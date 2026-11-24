"""The controls a dashboard media card uses, one section for each control.

Volume, mute, transport, timeline, state and artwork on the endpoint entity.
The source dropdown itself is covered in `test_source_selection`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
import pytest

from custom_components.sendspin.const import SOURCE_NONE
from custom_components.sendspin.mesh import parse_view
from tests.fakes.fake_sendspin import FakeSendspinServer
from tests.integration.test_source_selection import (
    CLIENT_ID,
    FIXTURE,
    PLAYER_URL,
    entity_id,
    flush,
    setup_with_mesh,
)

UNIT_HOST = "192.168.7.204"


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """A server holding one adopted speaker."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1", volume=40, muted=False)
    return server


def held_by_unit(*, volume: int | None = 35, muted: bool | None = False) -> dict:
    """A mesh view in which the first unit holds our speaker on its AirPlay."""
    payload = json.loads(json.dumps(FIXTURE))
    source = payload["units"][0]["sources"][0]
    source["player_ids"] = [CLIENT_ID]
    source["active"] = True
    source["streaming"] = True
    player = {
        "player_id": CLIENT_ID,
        "name": "Satellite1",
        "url": PLAYER_URL,
        "connected": True,
    }
    if volume is not None:
        player["volume"] = volume
    if muted is not None:
        player["muted"] = muted
    payload["units"][0]["players"] = [player]
    return payload


async def hand_to_unit(
    hass: HomeAssistant, fake_server: FakeSendspinServer, payload: dict
):
    """Set up with the unit, not Home Assistant, holding the speaker."""
    entry, _assign = await setup_with_mesh(hass, fake_server, payload)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await flush(hass)
    return entry


async def call(hass: HomeAssistant, service: str, payload: dict, **data) -> AsyncMock:
    """Call a media_player service and return the mocked mesh POST."""
    post = AsyncMock()
    with (
        patch(
            "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
            return_value=parse_view(payload),
        ),
        patch("custom_components.sendspin.mesh.MeshClient._post", post),
    ):
        await hass.services.async_call(
            "media_player",
            service,
            {ATTR_ENTITY_ID: entity_id(hass), **data},
            blocking=True,
        )
        await flush(hass)
    return post


# --- Mute and volume through a unit -----------------------------------------


async def test_mute_through_a_unit_sends_the_current_volume(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """An old unit refuses a volume call that has no `volume`.

    It also sets the level it is given, so the level sent with a mute must be
    the level the speaker is at now.
    """
    payload = held_by_unit(volume=35, muted=False)
    await hand_to_unit(hass, fake_server, payload)

    post = await call(hass, "volume_mute", payload, is_volume_muted=True)

    post.assert_awaited_once_with(
        UNIT_HOST, "volume", {"player_id": CLIENT_ID, "volume": 35, "muted": True}
    )


async def test_unmute_through_a_unit_sends_the_current_volume(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Unmute is the same call with the flag cleared."""
    payload = held_by_unit(volume=35, muted=True)
    await hand_to_unit(hass, fake_server, payload)

    state = hass.states.get(entity_id(hass))
    assert state.attributes["is_volume_muted"] is True
    assert state.attributes["supported_features"] & MediaPlayerEntityFeature.VOLUME_MUTE

    post = await call(hass, "volume_mute", payload, is_volume_muted=False)

    post.assert_awaited_once_with(
        UNIT_HOST, "volume", {"player_id": CLIENT_ID, "volume": 35, "muted": False}
    )


async def test_volume_through_a_unit_keeps_the_mute(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """An old unit reads an absent `muted` as false, which would unmute."""
    payload = held_by_unit(volume=35, muted=True)
    await hand_to_unit(hass, fake_server, payload)

    post = await call(hass, "volume_set", payload, volume_level=0.5)

    post.assert_awaited_once_with(
        UNIT_HOST, "volume", {"player_id": CLIENT_ID, "volume": 50, "muted": True}
    )


async def test_volume_through_a_unit_does_not_invent_a_mute(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """With no mute state reported, the body carries only what is known."""
    body: dict = {}

    async def capture(_self, _host, _path, sent):
        body.update(sent)

    from custom_components.sendspin.mesh import MeshClient

    with patch.object(MeshClient, "_post", capture):
        await MeshClient(session=None).async_set_volume(
            UNIT_HOST, CLIENT_ID, volume=50, muted=None
        )

    assert body == {"player_id": CLIENT_ID, "volume": 50}


async def test_mute_on_a_speaker_we_hold_goes_over_our_own_connection(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker Home Assistant holds needs no unit."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    post = await call(hass, "volume_mute", FIXTURE, is_volume_muted=True)

    post.assert_not_awaited()
    assert fake_server.clients_by_id[CLIENT_ID].player.commanded_mutes == [True]


# --- Volume step --------------------------------------------------------------


async def test_volume_step_is_offered_with_the_slider(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker with a known level can be stepped."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    features = hass.states.get(entity_id(hass)).attributes["supported_features"]

    assert features & MediaPlayerEntityFeature.VOLUME_SET
    assert features & MediaPlayerEntityFeature.VOLUME_STEP


async def test_volume_step_is_absent_when_the_level_is_unknown(
    hass: HomeAssistant,
) -> None:
    """A step from an unknown level has no start point, so it is not offered."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1")
    await setup_with_mesh(hass, server, FIXTURE)

    features = hass.states.get(entity_id(hass)).attributes["supported_features"]

    assert not features & MediaPlayerEntityFeature.VOLUME_STEP


async def test_volume_up_and_down_move_the_endpoint_by_one_step(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Five points for each press, in whole percent."""
    await setup_with_mesh(hass, fake_server, FIXTURE)
    role = fake_server.clients_by_id[CLIENT_ID].player

    await call(hass, "volume_up", FIXTURE)
    await call(hass, "volume_down", FIXTURE)

    # The fake does not echo a command, so each press starts from 40.
    assert role.commanded_volumes == [45, 35]


async def test_volume_step_stops_at_the_ends(hass: HomeAssistant) -> None:
    """A press at the limit sends the limit, never a value out of range."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1", volume=98, muted=False)
    await setup_with_mesh(hass, server, FIXTURE)

    await call(hass, "volume_up", FIXTURE)

    assert server.clients_by_id[CLIENT_ID].player.commanded_volumes == [100]


async def test_volume_step_through_a_unit_keeps_the_mute(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A step is a volume command, so it follows the same pair rule."""
    payload = held_by_unit(volume=35, muted=True)
    await hand_to_unit(hass, fake_server, payload)

    post = await call(hass, "volume_up", payload)

    post.assert_awaited_once_with(
        UNIT_HOST, "volume", {"player_id": CLIENT_ID, "volume": 40, "muted": True}
    )


# --- Source: "None" and what must never show ----------------------------------


async def test_selecting_none_releases_the_speaker_and_holds_it(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """None takes the speaker off its stream, by id and by URL."""
    payload = held_by_unit()
    entry = await hand_to_unit(hass, fake_server, payload)
    entry.runtime_data.memo.set_routed_away(PLAYER_URL, True)
    fake_server.dial_calls.clear()

    post = await call(hass, "select_source", FIXTURE, source=SOURCE_NONE)

    post.assert_awaited_once_with(
        UNIT_HOST,
        "release",
        {"source_id": "airplay-1", "url": PLAYER_URL, "player_id": CLIENT_ID},
    )
    # Home Assistant holds it again, on no stream.
    assert [dial.url for dial in fake_server.dial_calls] == [PLAYER_URL]
    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is False

    # The dial lands.
    fake_server.clients_by_id[CLIENT_ID].is_connected = True
    fake_server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)
    assert hass.states.get(entity_id(hass)).attributes["source"] == SOURCE_NONE


async def test_a_calibration_source_never_shows(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker playing a calibration tone is on no stream the user can name."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"].append(
        {
            "source_id": f"cal:{CLIENT_ID}",
            "group_id": "g-cal",
            "name": f"cal:{CLIENT_ID}",
            "player_ids": [CLIENT_ID],
            "active": True,
            "streaming": True,
        }
    )
    payload["units"][0]["players"] = [
        {"player_id": CLIENT_ID, "name": "Satellite1", "url": PLAYER_URL}
    ]
    entry = await hand_to_unit(hass, fake_server, payload)

    state = hass.states.get(entity_id(hass))

    assert state.attributes["source_list"] == [SOURCE_NONE]
    assert state.attributes["source"] == SOURCE_NONE
    assert state.state == "idle"
    # And no controller link reads it.
    assert not [k for k in entry.runtime_data.coordinator.links if "cal:" in k]


async def test_a_speaker_a_unit_holds_on_no_stream_is_at_none(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A unit detaches its speakers when a source dies, and keeps the socket.

    The unit is no stream, so its name must not show as the source. It stays in
    the `held_by` attribute, and the speaker stays available.
    """
    payload = held_by_unit()
    source = payload["units"][0]["sources"][0]
    source["player_ids"] = []
    source["active"] = source["streaming"] = False
    await hand_to_unit(hass, fake_server, payload)

    state = hass.states.get(entity_id(hass))

    assert state.state == "idle"
    assert state.attributes["source"] == SOURCE_NONE
    assert state.attributes["source_list"] == [SOURCE_NONE]
    assert state.attributes["held_by"] == "Plum Amp100"
