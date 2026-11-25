"""The stream entities: one media player for each mesh source.

A stream entity shows what plays on a source, takes its transport, sets the
level on the sending device, and moves every adopted speaker on it at once.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.sendspin.const import DOMAIN, SOURCE_NONE
from custom_components.sendspin.legacy_client import ControllerSnapshot
from custom_components.sendspin.mesh import parse_view
from tests.fakes.fake_sendspin import FakeSendspinServer
from tests.integration.test_dashboard_controls import TRACK, hand_to_unit, held_by_unit
from tests.integration.test_source_selection import (
    CLIENT_ID,
    PLAYER_URL,
    flush,
    repoll,
)

AIRPLAY_KEY = "unit-7204:airplay-1"
SPOTIFY_KEY = "unit-7122:spotify-1"


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """A server holding one adopted speaker."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1", volume=40, muted=False)
    return server


def stream_id(hass: HomeAssistant, key: str) -> str:
    """The entity id of the stream entity for a source key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "media_player", DOMAIN, f"source:{key}"
    )
    assert entity_id is not None, key
    return entity_id


def endpoint_id(hass: HomeAssistant) -> str:
    """The entity id of the adopted speaker."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "media_player", DOMAIN, f"player:{PLAYER_URL}"
    )
    assert entity_id is not None
    return entity_id


def with_spotify(payload: dict, *, volume: int | None = 30) -> dict:
    """The RackPi's Spotify input live, with a source level."""
    copy = json.loads(json.dumps(payload))
    source = copy["units"][1]["sources"][1]
    source["active"] = True
    source["streaming"] = True
    source["source_volume"] = volume
    source["source_muted"] = False
    return copy


async def test_every_routable_source_gets_a_stream_entity(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Configured inputs are durable, so each one is an entity, live or not."""
    await hand_to_unit(hass, fake_server, held_by_unit())

    registry = er.async_get(hass)
    keys = {
        entry.unique_id.removeprefix("source:")
        for entry in registry.entities.values()
        if entry.platform == DOMAIN and entry.unique_id.startswith("source:")
    }

    assert keys == {
        "unit-7204:airplay-1",
        "unit-7204:bluetooth-1",
        "unit-7122:airplay-1",
        "unit-7122:spotify-1",
        "unit-7122:bluetooth-1",
        "unit-7122:spotify-2",
    }


async def test_a_source_with_no_sender_is_off(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Off, not unavailable: the source is there, nothing feeds it."""
    await hand_to_unit(hass, fake_server, held_by_unit())

    assert hass.states.get(stream_id(hass, SPOTIFY_KEY)).state == "off"


async def test_the_stream_lists_its_speakers(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """`endpoints` holds the entity ids of the adopted speakers on it."""
    await hand_to_unit(hass, fake_server, held_by_unit())

    attributes = hass.states.get(stream_id(hass, AIRPLAY_KEY)).attributes

    assert attributes["endpoints"] == [endpoint_id(hass)]
    assert attributes["speakers"] == ["Satellite1"]
    assert attributes["source_label"] == "Plum Amp100 / 204 AP"


async def test_the_stream_shows_what_plays_and_takes_transport(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Title, state and transport come from the link reading the source."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())
    coordinator = entry.runtime_data.coordinator
    link = coordinator.links[AIRPLAY_KEY]
    link.snapshot = ControllerSnapshot(
        connected=True, playback_state="playing", **TRACK
    )
    link.async_send_command = AsyncMock()
    coordinator.async_request_publish()
    await flush(hass)

    state = hass.states.get(stream_id(hass, AIRPLAY_KEY))
    assert state.state == "playing"
    assert state.attributes["media_title"] == TRACK["title"]

    await hass.services.async_call(
        "media_player",
        "media_pause",
        {ATTR_ENTITY_ID: stream_id(hass, AIRPLAY_KEY)},
        blocking=True,
    )
    link.async_send_command.assert_awaited_once_with("pause")


async def test_a_live_stream_is_read_with_no_speaker_of_ours_on_it(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A stream entity needs a link even when no adopted speaker listens."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await repoll(hass, entry, with_spotify(held_by_unit()))

    assert SPOTIFY_KEY in entry.runtime_data.coordinator.links


async def test_volume_is_the_source_level(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The stream's slider is the sending device's level, posted to its unit."""
    payload = with_spotify(held_by_unit(), volume=30)
    entry = await hand_to_unit(hass, fake_server, held_by_unit())
    await repoll(hass, entry, payload)

    state = hass.states.get(stream_id(hass, SPOTIFY_KEY))
    assert state.attributes["volume_level"] == 0.3
    assert (
        MediaPlayerEntityFeature(state.attributes["supported_features"])
        & MediaPlayerEntityFeature.VOLUME_SET
    )

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
            "volume_set",
            {ATTR_ENTITY_ID: stream_id(hass, SPOTIFY_KEY), "volume_level": 0.55},
            blocking=True,
        )

    post.assert_awaited_once_with(
        "192.168.7.122", "source-volume", {"source_id": "spotify-1", "volume": 55}
    )


async def test_a_source_that_takes_no_volume_offers_none(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The Amp100's AirPlay reports no source volume support."""
    await hand_to_unit(hass, fake_server, held_by_unit())

    features = MediaPlayerEntityFeature(
        hass.states.get(stream_id(hass, AIRPLAY_KEY)).attributes["supported_features"]
    )

    assert not features & MediaPlayerEntityFeature.VOLUME_SET


async def test_selecting_a_source_moves_every_speaker_on_the_stream(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A group move is each speaker's own `select_source`."""
    await hand_to_unit(hass, fake_server, held_by_unit())

    moved = AsyncMock()
    with patch(
        "custom_components.sendspin.media_player."
        "SendspinEndpointMediaPlayer.async_select_source",
        moved,
    ):
        await hass.services.async_call(
            "media_player",
            "select_source",
            {ATTR_ENTITY_ID: stream_id(hass, AIRPLAY_KEY), "source": SOURCE_NONE},
            blocking=True,
        )

    moved.assert_awaited_once_with(source=SOURCE_NONE)
