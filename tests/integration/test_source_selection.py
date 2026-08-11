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
from custom_components.sendspin.mesh import parse_view
from tests.fakes.fake_sendspin import FakeSendspinServer

PLAYER_URL = "ws://192.168.7.151:8928/sendspin"
CLIENT_ID = "98:A3:16:D0:9E:E8"

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "mesh_view.json").read_text()
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


async def test_streams_appear_as_options_on_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Every source on the mesh is selectable, plus 'None'."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    state = hass.states.get(entity_id(hass))
    options = state.attributes["source_list"]

    assert SOURCE_NONE in options
    assert "Plum Amp100 / 204 AP" in options
    assert "Plum RackPi / VLAN7 AirPlay" in options
    assert len(options) == 7  # six sources plus None
    assert (
        state.attributes["supported_features"] & MediaPlayerEntityFeature.SELECT_SOURCE
    )


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
