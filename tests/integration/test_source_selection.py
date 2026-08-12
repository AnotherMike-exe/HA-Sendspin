"""The source dropdown: what M4 exists to deliver.

Streams appear and disappear as options on a durable speaker entity, so nothing
is ever created or destroyed in the entity registry when a stream starts or
stops. That is the whole reason entities are anchored to speakers.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiosendspin.models.types import GoodbyeReason
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import ATTR_ENTITY_ID, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sendspin.const import (
    CONF_LISTENER_URL,
    DOMAIN,
    SOURCE_NONE,
    SUBENTRY_TYPE_PLAYER,
)
from custom_components.sendspin.coordinator import _STRAND_CONFIRMATIONS
from custom_components.sendspin.mesh import parse_view
from tests.fakes.fake_sendspin import FakeSendspinServer

PLAYER_URL = "ws://192.168.7.151:8928/sendspin"
CLIENT_ID = "98:A3:16:D0:9E:E8"

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "mesh_view.json").read_text()
)


def live(*sources: tuple[str, str], payload: dict | None = None) -> dict:
    """A copy of the mesh view with the named `(unit_id, source_id)` being fed.

    The captured fixture is entirely idle, because a Plum unit publishes every
    *configured* input whether or not a sender is connected to it. Only a fed
    source is a stream, and only streams are offered — so any test about the
    dropdown has to say which of the six are real.
    """
    copy = json.loads(json.dumps(FIXTURE if payload is None else payload))
    wanted = set(sources)
    for unit in copy["units"]:
        for source in unit["sources"]:
            if (unit["unit_id"], source["source_id"]) in wanted:
                source["active"] = True
                source["streaming"] = True
    return copy


async def repoll(hass: HomeAssistant, entry: MockConfigEntry, payload: dict) -> None:
    """Feed the coordinator a fresh mesh view."""
    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(payload),
    ):
        await entry.runtime_data.coordinator.async_refresh_mesh()
    await flush(hass)


def no_real_links():
    """Keep controller links from dialling for real.

    Each `flush` advances virtual time, so a test that polls several times in a
    row reaches the links' reconnect backoff and they try to open a socket. Tests
    here drive link state by assigning snapshots, so starting them buys nothing.
    """
    return patch(
        "custom_components.sendspin.coordinator.LegacyControllerClient.async_start"
    )


async def flush(hass: HomeAssistant) -> None:
    """Let the coordinator's debounced publish land.

    Server events and mesh polls are coalesced with a short cooldown, so the
    trailing publish sits on a timer that test time does not reach on its own.
    """
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """A server holding one adopted speaker."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1", volume=40, muted=False)
    return server


async def setup_with_mesh(
    hass: HomeAssistant, fake_server: FakeSendspinServer, payload: dict
) -> tuple[MockConfigEntry, AsyncMock]:
    """Bring up a hub with one adopted speaker and a reachable mesh."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub",
        title="Home",
        data={CONF_NAME: "Home"},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_PLAYER,
                "title": "Satellite1",
                "data": {CONF_LISTENER_URL: PLAYER_URL},
                "unique_id": PLAYER_URL,
            }
        ],
    )
    entry.add_to_hass(hass)
    assign = AsyncMock()
    with (
        patch(
            "custom_components.sendspin.server_host.SendspinServer",
            return_value=fake_server,
        ),
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(payload),
        ),
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_assign", assign
        ) as _assign,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entry.runtime_data.coordinator.async_note_mesh_host("192.168.7.204")
        await entry.runtime_data.coordinator.async_refresh_mesh()
        await flush(hass)
    return entry, assign


def entity_id(hass: HomeAssistant) -> str:
    """The one media_player under test."""
    return hass.states.async_entity_ids("media_player")[0]


async def test_live_streams_appear_as_options_on_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A source with a sender on it is selectable; a bare configured input is not.

    The mesh publishes six sources here and two of them are being fed. Offering
    all six put an AirPlay endpoint nothing was connected to in the dropdown as
    though a speaker could usefully be routed to it.
    """
    await setup_with_mesh(
        hass,
        fake_server,
        live(("unit-7204", "airplay-1"), ("unit-7122", "airplay-1")),
    )

    state = hass.states.get(entity_id(hass))
    options = state.attributes["source_list"]

    assert options == [
        SOURCE_NONE,
        "Plum Amp100 / 204 AP",
        "Plum RackPi / VLAN7 AirPlay",
    ]
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )


async def test_an_idle_mesh_offers_only_none(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Six configured inputs and no senders means nothing to route to.

    A dropdown holding only 'None' is the honest answer, and 'None' has to stay
    offered regardless — taking a speaker off a stream must never stop being
    possible.
    """
    await setup_with_mesh(hass, fake_server, FIXTURE)

    state = hass.states.get(entity_id(hass))

    assert state.attributes["source_list"] == [SOURCE_NONE]
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )


async def test_a_stream_that_dies_while_selected_stays_pinned(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The selection must never be a value absent from its own options.

    A sender walking away drops the source from the live set, but the speaker is
    still on it until something moves it. Dropping the label outright left Home
    Assistant rendering a blank dropdown over a speaker that was still routed.
    """
    playing = live(("unit-7204", "airplay-1"))
    playing["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    entry, _assign = await setup_with_mesh(hass, fake_server, playing)

    assert hass.states.get(entity_id(hass)).attributes["source"] == (
        "Plum Amp100 / 204 AP"
    )

    # The sender leaves. The source stops being live; the speaker stays on it.
    ended = json.loads(json.dumps(playing))
    ended["units"][0]["sources"][0]["active"] = False
    ended["units"][0]["sources"][0]["streaming"] = False
    await repoll(hass, entry, ended)

    state = hass.states.get(entity_id(hass))
    assert state.attributes["source"] == "Plum Amp100 / 204 AP"
    assert "Plum Amp100 / 204 AP" in state.attributes["source_list"]


async def test_a_speaker_we_hold_reports_none_not_our_own_name(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Home Assistant is not a source.

    The mesh names us as the holder, and reporting that name put a value in the
    box that appeared in no dropdown. We originate no audio, so holding a
    speaker on no stream *is* the none state.
    """
    held_by_us = json.loads(json.dumps(FIXTURE))
    local = held_by_us["units"][0]["local_player"]
    local["player_id"] = CLIENT_ID
    local["url"] = PLAYER_URL
    local["server_name"] = "Home"
    local["server_id"] = "our-server-id"

    with patch(
        "custom_components.sendspin.server_host.ServerHost.server_id",
        new="our-server-id",
    ):
        entry, _assign = await setup_with_mesh(hass, fake_server, held_by_us)
        # Drop our own socket, so the only thing saying we hold it is the mesh's
        # `server_id` — the branch under test. While `connected` is true the
        # answer is right for a different reason.
        fake_server.clients_by_id[CLIENT_ID].is_connected = False
        fake_server.emit(
            ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None)
        )
        await flush(hass)
        state = hass.states.get(entity_id(hass))

    snapshot = entry.runtime_data.coordinator.data.endpoints[PLAYER_URL]
    assert snapshot.connected is False
    assert snapshot.held_by_us is True

    assert state.attributes["source"] == SOURCE_NONE
    assert "Home" not in state.attributes["source_list"]


async def test_no_mesh_means_no_dropdown_rather_than_an_empty_one(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """With no Plum unit there is nothing to select, and that is not an error.

    The integration still adopts and controls speakers; it simply has no
    streams to offer. An empty dropdown would look broken.
    """
    await setup_with_mesh(hass, fake_server, {"units": []})

    state = hass.states.get(entity_id(hass))

    assert state.attributes.get("source_list") is None
    assert not (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )


async def test_selecting_a_stream_hands_the_speaker_to_the_owning_unit(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The routing call must reach the unit that owns the source.

    Both units expose `airplay-1`, so posting to the wrong one would silently
    route the speaker locally instead of where the user asked. And we let go of
    the speaker first, because two servers dialling one player is a tug-of-war
    that neither wins.
    """
    await setup_with_mesh(hass, fake_server, FIXTURE)

    assign = AsyncMock()
    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(FIXTURE),
        ),
        patch("custom_components.sendspin.mesh.MeshClient.async_assign", assign),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {
                ATTR_ENTITY_ID: entity_id(hass),
                "source": "Plum RackPi / VLAN7 AirPlay",
            },
            blocking=True,
        )
        await flush(hass)

    assign.assert_awaited_once()
    target, speaker_url = assign.await_args.args
    assert target.unit_id == "unit-7122"
    assert target.source_id == "airplay-1"
    assert target.unit_host == "192.168.7.122"
    assert speaker_url == PLAYER_URL
    # We stopped holding it before asking the unit to take it.
    assert fake_server.live_dial_urls == set()


async def test_the_current_stream_is_reported(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker on a source shows that source, not 'None'."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]

    await setup_with_mesh(hass, fake_server, payload)

    assert (
        hass.states.get(entity_id(hass)).attributes["source"] == "Plum Amp100 / 204 AP"
    )


async def test_a_speaker_on_nothing_reports_none(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Unassigned is a real, displayable state."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    assert hass.states.get(entity_id(hass)).attributes["source"] == SOURCE_NONE


async def test_selecting_an_unknown_stream_fails_clearly(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A stream that vanished between render and click must not fail silently."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    with pytest.raises(Exception, match="No Sendspin stream"):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Gone / Vanished"},
            blocking=True,
        )


async def test_a_yielded_speaker_stays_visible_and_explains_itself(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Another server holding a speaker is not a fault.

    Home Assistant drops attributes on unavailable entities, so marking a
    yielded speaker unavailable would hide the very explanation the user needs
    — and would imply the speaker is broken when it is healthy and playing.
    """
    from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent

    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    payload["units"][0]["sources"][0]["active"] = True
    await setup_with_mesh(hass, fake_server, payload)

    fake_server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    for _ in range(3):
        fake_server.emit(
            ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None)
        )
        await hass.async_block_till_done()
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    assert state.state != "unavailable"
    assert state.attributes["yielded_to"] == "contested"
    # And it still reports which stream it is on, which is the useful part.
    assert state.attributes["source"] == "Plum Amp100 / 204 AP"


# --- The control must survive its own success -------------------------------
#
# Observed live: selecting a stream worked and audio flowed, but the dropdown
# then vanished. Handing the speaker to another unit is exactly what makes us
# stop holding it, and supported_features was gated on holding it.


async def test_the_dropdown_survives_handing_the_speaker_away(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Routing is an HTTP call keyed on the URL, not something we need to hold.

    Otherwise the control destroys itself the moment it works, and the speaker
    can never be moved again from Home Assistant.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    payload["units"][0]["sources"][0]["streaming"] = True
    payload["units"][0]["sources"][0]["active"] = True
    await setup_with_mesh(hass, fake_server, payload)

    # We are no longer holding it — the other unit is.
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    assert state.state != "unavailable"
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )
    assert SOURCE_NONE in state.attributes["source_list"]
    assert state.attributes["source"] == "Plum Amp100 / 204 AP"


async def test_a_speaker_on_a_live_stream_reports_playing(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Do not report idle over the top of music.

    We originate no audio, but the mesh tells us whether audio is flowing on
    the stream the speaker sits on.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    payload["units"][0]["sources"][0]["streaming"] = True
    await setup_with_mesh(hass, fake_server, payload)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    await flush(hass)

    assert hass.states.get(entity_id(hass)).state == "playing"


async def test_routing_a_speaker_away_survives_a_restart(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A reload must not yank a speaker back off the stream the user chose.

    Adoption is restored on every setup, so without remembering the hand-off
    the next restart re-dials the speaker and steals it back.
    """
    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)

    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(FIXTURE),
        ),
        patch("custom_components.sendspin.mesh.MeshClient.async_assign", AsyncMock()),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Plum RackPi / VLAN7 AirPlay"},
            blocking=True,
        )
        await flush(hass)

    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is True

    fake_server.dial_calls.clear()
    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert fake_server.dial_calls == []
    # The entity still exists; we simply are not competing for the speaker.
    assert hass.states.async_entity_ids("media_player")


# --- Volume must survive routing too ---------------------------------------


async def test_volume_works_while_a_unit_holds_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Handing a speaker to a unit must not cost the user their volume control.

    Home Assistant can only command a speaker over its own connection, and
    routing gives that connection up — so the command goes through whichever
    unit now holds it.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    payload["units"][0]["players"] = [
        {
            "player_id": CLIENT_ID,
            "name": "Satellite1",
            "url": PLAYER_URL,
            "connected": True,
            "volume": 35,
            "muted": False,
        }
    ]
    await setup_with_mesh(hass, fake_server, payload)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    # The level the holding unit reports is shown, not nothing.
    assert state.attributes["volume_level"] == 0.35
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.VOLUME_SET

    set_volume = AsyncMock()
    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(payload),
        ),
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_set_volume", set_volume
        ),
    ):
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {ATTR_ENTITY_ID: entity_id(hass), "volume_level": 0.5},
            blocking=True,
        )
        await flush(hass)

    set_volume.assert_awaited_once()
    assert set_volume.await_args.args == ("192.168.7.204", CLIENT_ID)
    assert set_volume.await_args.kwargs["volume"] == 50


async def test_a_speaker_whose_hand_off_did_not_stick_is_taken_back(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A routed speaker on no stream must not be stranded forever.

    Routing suppresses our dialling so a restart cannot yank the speaker back.
    But if the stream then ends, leaving it suppressed means the speaker is
    held by nobody, on nothing, and unavailable with no way back.
    """
    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    entry.runtime_data.memo.remember_handshake(
        PLAYER_URL, name="Satellite1", client_id=CLIENT_ID
    )
    entry.runtime_data.memo.set_routed_away(PLAYER_URL, True)
    fake_server.dial_calls.clear()

    # The mesh keeps reporting it on no source at all.
    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(FIXTURE),
    ):
        for _ in range(3):
            await entry.runtime_data.coordinator.async_refresh_mesh()
            await flush(hass)

    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is False
    assert PLAYER_URL in [c.url for c in fake_server.dial_calls]


async def test_a_speaker_still_on_its_stream_is_left_alone(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The rescue must not fire while the hand-off is working.

    Otherwise Home Assistant takes the speaker straight back off the stream the
    user just put it on.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    entry, _assign = await setup_with_mesh(hass, fake_server, payload)
    entry.runtime_data.memo.remember_handshake(
        PLAYER_URL, name="Satellite1", client_id=CLIENT_ID
    )
    entry.runtime_data.memo.set_routed_away(PLAYER_URL, True)
    fake_server.dial_calls.clear()

    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(payload),
    ):
        for _ in range(5):
            await entry.runtime_data.coordinator.async_refresh_mesh()
            await flush(hass)

    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is True
    assert fake_server.dial_calls == []


async def test_the_rescue_holds_off_right_after_a_routing_call(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A working hand-off looks exactly like a failed one for a few seconds.

    Plum aggregates peer state on a 2s loop and we poll on a 5s one, so without
    a grace period the rescue can take a speaker straight back off the stream
    the user just put it on.
    """
    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    entry.runtime_data.memo.remember_handshake(
        PLAYER_URL, name="Satellite1", client_id=CLIENT_ID
    )

    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(FIXTURE),
        ),
        patch("custom_components.sendspin.mesh.MeshClient.async_assign", AsyncMock()),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Plum RackPi / VLAN7 AirPlay"},
            blocking=True,
        )
        await flush(hass)
        fake_server.dial_calls.clear()

        # The mesh has not caught up: the speaker is on no source yet.
        for _ in range(5):
            await entry.runtime_data.coordinator.async_refresh_mesh()
            await flush(hass)

    # Still handed away, and we have not started competing for it again.
    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is True
    assert fake_server.dial_calls == []


async def test_a_speaker_we_have_never_held_is_still_usable(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The bug that made two of three real speakers permanently unavailable.

    We only ever learned a client id by holding a speaker, so a speaker another
    server had was unidentifiable: it matched no source, showed no stream, and
    read as unavailable forever. The mesh knows the address it dials each
    speaker on, which closes the loop.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    payload["units"][0]["sources"][0]["streaming"] = True
    payload["units"][0]["players"] = [
        {
            "player_id": CLIENT_ID,
            "name": "Satellite1",
            "url": PLAYER_URL,
            "connected": True,
            "volume": 100,
        }
    ]

    # Nothing has ever connected to us, so our own registry knows nothing.
    fake_server.client_ids_by_url.clear()
    fake_server.clients_by_id.clear()
    entry, _assign = await setup_with_mesh(hass, fake_server, payload)

    state = hass.states.get(entity_id(hass))
    assert state.state == "playing"
    assert state.attributes["source"] == "Plum Amp100 / 204 AP"
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )
    # And the id is remembered, so it survives the mesh going away.
    assert entry.runtime_data.memo.client_id(PLAYER_URL) == CLIENT_ID


async def test_a_speaker_on_another_server_can_still_be_routed(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The core promise: move a speaker between servers.

    A speaker Music Assistant holds is on no Plum source and is not held by us,
    but it is still routable — the unit does the dialling, and a player always
    yields to the newest dialer. Marking it unavailable took away the one
    control that actually works on it.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["players"] = []
    payload["units"][0]["local_player"] = {
        "player_id": "player-7204",
        "name": "Plum Amp100",
        "url": PLAYER_URL,
        "attached": True,
        "server_name": "Music Assistant",
    }
    fake_server.client_ids_by_url.clear()
    fake_server.clients_by_id.clear()
    await setup_with_mesh(hass, fake_server, payload)

    state = hass.states.get(entity_id(hass))
    assert state.state != "unavailable"
    # And it says who has it, so "on no stream" is explicable.
    assert state.attributes["held_by"] == "Music Assistant"
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )

    assign = AsyncMock()
    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(payload),
        ),
        patch("custom_components.sendspin.mesh.MeshClient.async_assign", assign),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Plum Amp100 / 204 AP"},
            blocking=True,
        )
        await flush(hass)

    assign.assert_awaited_once()
    target, url = assign.await_args.args
    assert target.unit_id == "unit-7204"
    assert url == PLAYER_URL


class _StubLink:
    """A link that is already connected, for asserting on filtering alone."""

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.url = "ws://stub:8927/sendspin"

    async def async_stop(self) -> None:
        """Nothing to tear down."""


async def observe_server(hass, entry, host: str, snapshot) -> None:
    """Let the coordinator open a link to a server, then give it a state.

    Built the way production does — noting the host, letting a poll create the
    link — because links are pruned to exactly the set that is wanted, so one
    injected by hand would vanish on the next poll.

    The host must not be a Plum unit: no `server:` link is opened to one, since
    a unit is observed through the `ctrl:` links its streams already need.
    """
    coordinator = entry.runtime_data.coordinator
    coordinator.async_note_mesh_host(host)
    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(FIXTURE),
    ):
        await coordinator.async_refresh_mesh()
    assert f"server:{host}" in coordinator.links, (
        f"no server link to {host}; it is a Plum unit host, or discovery of it "
        f"did not land. Open links: {sorted(coordinator.links)}"
    )
    coordinator._links[f"server:{host}"].snapshot = snapshot
    coordinator.async_request_publish()
    await flush(hass)


async def test_a_speaker_on_an_unseen_server_still_shows_what_is_playing(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A third-party speaker on a non-Plum server is in no mesh view at all.

    Music Assistant exposes no mesh API and Plum only describes its own
    speakers, so nothing links the two. When exactly one observed server is
    playing it is the only candidate, which is enough to show the user what
    their speaker is actually playing.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    # Music Assistant holds it, so we do not — and it said so on the way out.
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )

    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True,
            playback_state="playing",
            title="I Remember",
            artist="Kaskade & deadmau5",
            supported_commands=("play", "pause"),
        ),
    )

    state = hass.states.get(entity_id(hass))
    assert state.state == "playing"
    assert state.attributes["media_title"] == "I Remember"
    assert state.attributes["media_artist"] == "Kaskade & deadmau5"


async def test_two_servers_playing_means_we_decline_to_guess(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Better no track than the wrong one on the wrong speaker."""
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )

    for host, title in (("192.168.7.226", "One"), ("192.168.7.230", "Two")):
        await observe_server(
            hass,
            entry,
            host,
            ControllerSnapshot(connected=True, playback_state="playing", title=title),
        )

    assert hass.states.get(entity_id(hass)).attributes.get("media_title") is None


async def test_an_idle_servers_stale_title_does_not_block_attribution(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A server keeps the last track's metadata after it stops.

    Counting titles made an idle server look like a second candidate, so the
    rule declined and the speaker's now-playing flickered in and out as that
    stale title came and went.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )

    # An idle server still holding the last track it played.
    await observe_server(
        hass,
        entry,
        "192.168.7.240",
        ControllerSnapshot(
            connected=True, playback_state="stopped", title="Talk In Your Sleep"
        ),
    )
    # And the one actually playing.
    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True, playback_state="playing", title="I Remember"
        ),
    )

    assert hass.states.get(entity_id(hass)).attributes["media_title"] == "I Remember"


async def test_binary_artwork_reaches_home_assistant(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Home Assistant fetches an image only when the entity offers a hash.

    It derives that hash from `media_image_url`, which binary artwork does not
    have — so without an override the art is read correctly off the wire and
    then silently dropped.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True,
            playback_state="playing",
            title="I Remember",
            artwork=b"\xff\xd8jpegbytes",
        ),
    )

    state = hass.states.get(entity_id(hass))
    assert state.attributes.get("entity_picture") is not None

    player = hass.data["media_player"].get_entity(entity_id(hass))
    assert await player.async_get_media_image() == (b"\xff\xd8jpegbytes", "image/jpeg")


async def test_one_server_on_two_addresses_is_not_two_candidates(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A server advertising over IPv4 and IPv6 is discovered as two hosts.

    Both links report the same track, which made a single playing server look
    like two — so the rule declined and now-playing flickered out.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )

    same = dict(
        connected=True,
        playback_state="playing",
        title="I Remember",
        server_id="ma",
    )
    await observe_server(hass, entry, "192.168.7.226", ControllerSnapshot(**same))
    await observe_server(
        hass, entry, "fd00:1::dea6:32ff:fe2f:8080", ControllerSnapshot(**same)
    )

    assert hass.states.get(entity_id(hass)).attributes["media_title"] == "I Remember"


async def test_two_servers_playing_the_same_track_is_not_ambiguous(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """One server feeding another puts the same audio on both.

    Music Assistant into another server's input means both report the same
    track. Treating that as two competing answers made now-playing blink out
    whenever their states drifted in and out of step.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )

    for host, server_id in (("192.168.7.226", "ma"), ("192.168.7.240", "other")):
        await observe_server(
            hass,
            entry,
            host,
            ControllerSnapshot(
                connected=True,
                playback_state="playing",
                title="There Might Be Coffee",
                artist="Bell Orchestre",
                server_id=server_id,
                server_name=server_id,
            ),
        )

    assert (
        hass.states.get(entity_id(hass)).attributes["media_title"]
        == "There Might Be Coffee"
    )


async def test_another_server_is_offered_as_a_source(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Handing a speaker back to Music Assistant is a routing choice too.

    Previously the dropdown listed only this mesh's streams, so there was no
    way to express it at all — and a speaker Music Assistant held reported
    "None", which read as "not routed anywhere".
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True,
            playback_state="stopped",
            server_id="ma",
            server_name="Music Assistant",
        ),
    )

    options = hass.states.get(entity_id(hass)).attributes["source_list"]
    assert "Music Assistant" in options

    # The Plum units run Sendspin servers too, and links to them are open — but
    # they are already in the dropdown as their own streams. Offering the bare
    # unit name beside `Plum Amp100 / 204 AP` was meaningless: selecting it only
    # released the speaker and hoped the unit re-dialled.
    assert "Plum Amp100" not in options
    assert "Plum RackPi" not in options

    # Choosing it stops us holding the speaker, so that server can take it.
    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(FIXTURE),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Music Assistant"},
            blocking=True,
        )
        await flush(hass)

    assert fake_server.live_dial_urls == set()
    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is True


async def test_an_unconfirmed_mesh_stops_asserting_what_is_live(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A frozen view keeps identities but must not keep claiming a stream is fed.

    An unreachable mesh deliberately does not clear the view — a dropped poll
    must never read as every stream having ended. But "something is feeding this
    source" is a statement about *now*, and a sender that left during an outage
    would otherwise stay selectable forever.
    """
    from custom_components.sendspin.mesh import MeshView

    entry, _assign = await setup_with_mesh(
        hass, fake_server, live(("unit-7204", "airplay-1"))
    )
    coordinator = entry.runtime_data.coordinator
    assert (
        "Plum Amp100 / 204 AP"
        in hass.states.get(entity_id(hass)).attributes["source_list"]
    )

    # The mesh stops answering, and stays unanswered past the liveness TTL.
    coordinator._mesh_fetched_at = hass.loop.time() - 3600
    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=MeshView(reachable=False),
    ):
        await coordinator.async_refresh_mesh()
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    assert state.attributes["source_list"] == [SOURCE_NONE]
    # The view itself survives, so labels still resolve and routing still works.
    assert len(coordinator.mesh_view.sources) == 6

    # And the stream comes back when the mesh does — even though the source set
    # is identical across the outage, which is the common case.
    await repoll(hass, entry, live(("unit-7204", "airplay-1")))

    assert (
        "Plum Amp100 / 204 AP"
        in hass.states.get(entity_id(hass)).attributes["source_list"]
    )


async def test_a_plum_unit_is_never_offered_as_a_server(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A unit reached by a connected link still must not become an entry.

    Each Plum unit runs a Sendspin server, so a link to one reports a perfectly
    valid `server/hello` name — which is how a bare `Plum Amp100` ended up in
    the dropdown beside that same unit's `Plum Amp100 / 204 AP`. The unit is
    already reachable by naming its streams; the bare entry only released the
    speaker.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    coordinator = entry.runtime_data.coordinator

    # The unit's host is a known Sendspin server and gets noted as such, so this
    # is the exact path that used to produce the bare entry.
    coordinator.async_note_mesh_host("192.168.7.204")
    await repoll(hass, entry, FIXTURE)

    # No `server:` link is opened to it at all — its streams and their `ctrl:`
    # links are the whole of what we need from a unit.
    assert "server:192.168.7.204" not in coordinator.links

    # And were one open anyway — opened before the mesh view identified the host
    # as a unit — it still must not become an entry.
    coordinator._links["server:192.168.7.204"] = _StubLink(
        ControllerSnapshot(
            connected=True,
            playback_state="stopped",
            server_id="unit-7204",
            server_name="Plum Amp100",
        )
    )
    coordinator.async_request_publish()
    await flush(hass)

    assert coordinator.data.servers == ()
    assert (
        "Plum Amp100" not in hass.states.get(entity_id(hass)).attributes["source_list"]
    )


async def test_a_foreign_server_stays_offered_while_the_speaker_is_routed(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Handing a speaker back must not stop being possible once it is routed.

    `server:` links were opened per *unrouted* endpoint, so the moment a speaker
    went onto a Plum stream every one of them closed and Music Assistant vanished
    from the dropdown — you had to select None first to get the option back. It
    could not be offered at all after a restart with everything already routed.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    playing = live(("unit-7204", "airplay-1"))
    playing["units"][0]["sources"][0]["player_ids"] = [CLIENT_ID]
    entry, _assign = await setup_with_mesh(hass, fake_server, playing)
    coordinator = entry.runtime_data.coordinator

    # Let the handshake land, so the memo knows the client id. Production always
    # reaches this state, and without it the speaker matches no source and the
    # coordinator falls back to observing every candidate server — which would
    # make this test pass for the wrong reason.
    fake_server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)
    assert entry.runtime_data.memo.client_id(PLAYER_URL) == CLIENT_ID

    coordinator.async_note_mesh_host("192.168.7.226")
    await repoll(hass, entry, playing)
    coordinator._links["server:192.168.7.226"].snapshot = ControllerSnapshot(
        connected=True,
        playback_state="stopped",
        server_id="ma",
        server_name="Music Assistant",
    )
    coordinator.async_request_publish()
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    # Routed onto a stream, and the hand-back option is still there.
    assert state.attributes["source"] == "Plum Amp100 / 204 AP"
    assert state.attributes["source_list"] == [
        SOURCE_NONE,
        "Music Assistant",
        "Plum Amp100 / 204 AP",
    ]


async def test_a_speaker_handed_to_a_server_is_not_taken_back(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The stranded rescue must not undo a deliberate hand-off to a server.

    A speaker on a server with no mesh API is on no mesh source permanently and
    by design. The rescue read that as a failed hand-off, so ~15s after handing a
    speaker to Music Assistant it re-adopted it, yanking it straight back and
    re-arming the tug-of-war. Only the user can undo this now.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    coordinator = entry.runtime_data.coordinator
    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True,
            playback_state="stopped",
            server_id="ma",
            server_name="Music Assistant",
        ),
    )

    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(FIXTURE),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Music Assistant"},
            blocking=True,
        )
        await flush(hass)

    memo = entry.runtime_data.memo
    assert memo.routed_away(PLAYER_URL) is True
    assert memo.handed_to_server(PLAYER_URL) == "Music Assistant"
    assert fake_server.live_dial_urls == set()

    # Well past the routing grace, and more polls than the strand threshold.
    coordinator._routed_at.clear()
    with no_real_links():
        for _ in range(_STRAND_CONFIRMATIONS + 2):
            await repoll(hass, entry, FIXTURE)

    # Still handed away: we have not re-dialled it.
    assert fake_server.live_dial_urls == set()
    assert memo.routed_away(PLAYER_URL) is True


async def test_a_failed_stream_handoff_is_still_rescued(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The rescue still does its job for the case it exists for.

    A *stream* hand-off is verifiable — the mesh either shows the speaker on that
    source or it does not — so a speaker left on no stream and held by nothing is
    genuinely stranded and must be taken back.
    """
    entry, _assign = await setup_with_mesh(
        hass, fake_server, live(("unit-7204", "airplay-1"))
    )
    coordinator = entry.runtime_data.coordinator

    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(live(("unit-7204", "airplay-1"))),
        ),
        patch("custom_components.sendspin.mesh.MeshClient.async_assign", AsyncMock()),
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: entity_id(hass), "source": "Plum Amp100 / 204 AP"},
            blocking=True,
        )
        await flush(hass)

    memo = entry.runtime_data.memo
    assert memo.routed_away(PLAYER_URL) is True
    assert memo.handed_to_server(PLAYER_URL) is None

    # The mesh never shows it on the source, and nothing else claims it.
    coordinator._routed_at.clear()
    with no_real_links():
        for _ in range(_STRAND_CONFIRMATIONS + 1):
            await repoll(hass, entry, FIXTURE)

    assert memo.routed_away(PLAYER_URL) is False
    assert PLAYER_URL in fake_server.live_dial_urls


async def test_the_server_we_read_from_is_named_as_the_source(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Showing a server's track while reporting "None" was contradictory.

    A third-party speaker appears in no mesh view, so nothing said who held it
    — but if we are reading its now-playing from a server, that server has it.
    """
    from custom_components.sendspin.legacy_client import ControllerSnapshot

    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await observe_server(
        hass,
        entry,
        "192.168.7.226",
        ControllerSnapshot(
            connected=True,
            playback_state="playing",
            title="I Remember",
            server_id="ma",
            server_name="Music Assistant",
        ),
    )

    state = hass.states.get(entity_id(hass))
    assert state.attributes["media_title"] == "I Remember"
    assert state.attributes["source"] == "Music Assistant"
    assert state.attributes["held_by"] == "Music Assistant"
