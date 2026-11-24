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
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.sendspin.const import SOURCE_NONE
from custom_components.sendspin.legacy_client import ControllerSnapshot, Progress
from custom_components.sendspin.mesh import parse_view
from tests.fakes.fake_sendspin import FakeSendspinServer
from tests.integration.test_source_selection import (
    CLIENT_ID,
    FIXTURE,
    PLAYER_URL,
    entity_id,
    flush,
    repoll,
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


# --- Transport ----------------------------------------------------------------

AIRPLAY_KEY = "unit-7204:airplay-1"
PLUM_COMMANDS = ("play", "pause", "next", "previous")
SPOTIFY_COMMANDS = (
    *PLUM_COMMANDS,
    "repeat_off",
    "repeat_one",
    "repeat_all",
    "shuffle",
    "unshuffle",
)
TRANSPORT = (
    MediaPlayerEntityFeature.PLAY
    | MediaPlayerEntityFeature.PAUSE
    | MediaPlayerEntityFeature.STOP
    | MediaPlayerEntityFeature.NEXT_TRACK
    | MediaPlayerEntityFeature.PREVIOUS_TRACK
    | MediaPlayerEntityFeature.REPEAT_SET
    | MediaPlayerEntityFeature.SHUFFLE_SET
)


async def observe_stream(hass: HomeAssistant, entry, **fields) -> AsyncMock:
    """Give the link that reads the speaker's stream a state.

    Returns the mock that receives the commands the entity sends.
    """
    coordinator = entry.runtime_data.coordinator
    link = coordinator.links[AIRPLAY_KEY]
    link.snapshot = ControllerSnapshot(connected=True, **fields)
    link.async_send_command = AsyncMock()
    coordinator.async_request_publish()
    await flush(hass)
    return link.async_send_command


def features(hass: HomeAssistant) -> MediaPlayerEntityFeature:
    """The entity's current feature set."""
    return MediaPlayerEntityFeature(
        hass.states.get(entity_id(hass)).attributes["supported_features"]
    )


async def test_transport_follows_what_the_stream_advertises(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """An AirPlay stream has four commands, so the entity has four buttons."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(
        hass, entry, playback_state="playing", supported_commands=PLUM_COMMANDS
    )

    assert features(hass) & TRANSPORT == (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
    )


async def test_stop_is_offered_only_when_the_server_advertises_it(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Plum-Audio has no stop. Another server can have one."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(
        hass, entry, playback_state="playing", supported_commands=("play", "stop")
    )

    assert features(hass) & TRANSPORT == (
        MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.STOP
    )


async def test_repeat_and_shuffle_show_for_a_stream_that_has_them(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A Spotify stream advertises repeat and shuffle, with their state."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    sent = await observe_stream(
        hass,
        entry,
        playback_state="playing",
        supported_commands=SPOTIFY_COMMANDS,
        repeat="all",
        shuffle=True,
    )

    state = hass.states.get(entity_id(hass))
    assert features(hass) & MediaPlayerEntityFeature.REPEAT_SET
    assert features(hass) & MediaPlayerEntityFeature.SHUFFLE_SET
    assert state.attributes["repeat"] == "all"
    assert state.attributes["shuffle"] is True

    await call(hass, "repeat_set", held_by_unit(), repeat="one")
    await call(hass, "shuffle_set", held_by_unit(), shuffle=False)
    await call(hass, "media_next_track", held_by_unit())

    assert [c.args[0] for c in sent.await_args_list] == [
        "repeat_one",
        "unshuffle",
        "next",
    ]


async def test_repeat_and_shuffle_are_absent_for_airplay(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """No command, so no control and no attribute."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(
        hass, entry, playback_state="playing", supported_commands=PLUM_COMMANDS
    )

    state = hass.states.get(entity_id(hass))
    assert not features(hass) & (
        MediaPlayerEntityFeature.REPEAT_SET | MediaPlayerEntityFeature.SHUFFLE_SET
    )
    assert "repeat" not in state.attributes
    assert "shuffle" not in state.attributes


async def test_transport_goes_when_the_speaker_leaves_the_stream(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """With no stream selected there is nothing for a button to act on."""
    payload = held_by_unit()
    entry = await hand_to_unit(hass, fake_server, payload)
    await observe_stream(
        hass, entry, playback_state="playing", supported_commands=SPOTIFY_COMMANDS
    )
    assert features(hass) & TRANSPORT

    gone = json.loads(json.dumps(payload))
    gone["units"][0]["sources"][0]["player_ids"] = []
    await repoll(hass, entry, gone)

    assert hass.states.get(entity_id(hass)).attributes["source"] == SOURCE_NONE
    assert not features(hass) & TRANSPORT


async def test_a_unit_held_speaker_does_not_borrow_another_servers_stream(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """One playing server is the only candidate only for a speaker nobody names.

    A speaker a Plum unit holds on no stream is accounted for. It must not take
    the track, or the buttons, of a server that happens to play alone.
    """
    payload = held_by_unit()
    payload["units"][0]["sources"][0]["player_ids"] = []
    entry = await hand_to_unit(hass, fake_server, payload)

    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(payload),
    ):
        coordinator = entry.runtime_data.coordinator
        coordinator.async_note_mesh_host("192.168.7.226")
        await coordinator.async_refresh_mesh()
    coordinator.links["server:192.168.7.226"].snapshot = ControllerSnapshot(
        connected=True,
        server_id="ma",
        server_name="Music Assistant",
        playback_state="playing",
        title="I Remember",
        supported_commands=PLUM_COMMANDS,
    )
    coordinator.async_request_publish()
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    assert state.attributes["source"] == SOURCE_NONE
    assert state.attributes.get("media_title") is None
    assert not features(hass) & TRANSPORT


async def test_a_command_the_stream_does_not_have_fails_clearly(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A stale card can still call a service the stream no longer has."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())
    sent = await observe_stream(
        hass, entry, playback_state="playing", supported_commands=("play",)
    )
    player = hass.data["media_player"].get_entity(entity_id(hass))

    with pytest.raises(HomeAssistantError):
        await player.async_media_next_track()

    sent.assert_not_awaited()


# --- Timeline -----------------------------------------------------------------


async def test_the_position_carries_the_time_it_was_true(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Home Assistant extrapolates from `media_position_updated_at`."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())
    taken_at = 1_800_000_000.0

    await observe_stream(
        hass,
        entry,
        playback_state="playing",
        title="Easy Way Out",
        progress=Progress(
            position_s=10.0, duration_s=240.0, playing=True, updated_at=taken_at
        ),
    )

    state = hass.states.get(entity_id(hass))
    assert state.attributes["media_position"] == 10
    assert state.attributes["media_duration"] == 240
    assert state.attributes["media_position_updated_at"].timestamp() == taken_at
    assert not features(hass) & MediaPlayerEntityFeature.SEEK


async def test_a_sender_with_no_progress_shows_no_position(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Not a bar at zero. No bar."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(
        hass,
        entry,
        playback_state="playing",
        title="Easy Way Out",
        progress=Progress(
            position_s=None, duration_s=None, playing=True, updated_at=1.0
        ),
    )

    state = hass.states.get(entity_id(hass))
    assert state.state == "playing"
    assert "media_position" not in state.attributes
    assert "media_duration" not in state.attributes
    assert "media_position_updated_at" not in state.attributes


# --- State and what a card shows ----------------------------------------------

MEDIA_ATTRIBUTES = (
    "media_title",
    "media_artist",
    "media_album_name",
    "media_content_type",
    "media_position",
    "media_duration",
    "entity_picture",
)

TRACK = {
    "title": "Easy Way Out",
    "artist": "Gotye",
    "album": "Making Mirrors",
    "artwork": b"\xff\xd8\xff\xe0cover",
    "supported_commands": PLUM_COMMANDS,
}


def stream_stopped(payload: dict) -> dict:
    """The same view with the sender gone, and the speaker still in the group.

    A unit before the true-none rule leaves its speakers attached.
    """
    ended = json.loads(json.dumps(payload))
    ended["units"][0]["sources"][0]["active"] = False
    ended["units"][0]["sources"][0]["streaming"] = False
    return ended


async def test_a_playing_stream_shows_its_track(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The base case the other state tests change one thing in."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(hass, entry, playback_state="playing", **TRACK)

    state = hass.states.get(entity_id(hass))
    assert state.state == "playing"
    assert state.attributes["media_title"] == "Easy Way Out"
    assert state.attributes["media_artist"] == "Gotye"
    assert state.attributes["media_album_name"] == "Making Mirrors"
    assert state.attributes["media_content_type"] == "music"
    assert state.attributes["entity_picture"]


async def test_a_stream_that_ended_is_idle_and_shows_no_track(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A server keeps the last title after it stops.

    The title held the state at paused for as long as the link stayed open,
    with the cover of a track that was over.
    """
    payload = held_by_unit()
    entry = await hand_to_unit(hass, fake_server, payload)
    await observe_stream(hass, entry, playback_state="playing", **TRACK)

    await repoll(hass, entry, stream_stopped(payload))
    await observe_stream(hass, entry, playback_state="stopped", **TRACK)

    state = hass.states.get(entity_id(hass))
    assert state.state == "idle"
    for attribute in MEDIA_ATTRIBUTES:
        assert attribute not in state.attributes, attribute
    player = hass.data["media_player"].get_entity(entity_id(hass))
    assert await player.async_get_media_image() == (None, None)


@pytest.mark.parametrize(
    "paused",
    [
        # Plum-Audio: the group stays `playing` and the speed goes to zero.
        {
            "playback_state": "playing",
            "progress": Progress(
                position_s=42.0, duration_s=240.0, playing=False, updated_at=1.0
            ),
        },
        # A server that says so.
        {"playback_state": "paused"},
    ],
)
async def test_a_paused_stream_is_paused_and_keeps_its_track(
    hass: HomeAssistant, fake_server: FakeSendspinServer, paused: dict
) -> None:
    """Paused is not ended. The track and the cover stay."""
    payload = held_by_unit()
    payload["units"][0]["sources"][0]["streaming"] = False
    entry = await hand_to_unit(hass, fake_server, payload)

    await observe_stream(hass, entry, **paused, **TRACK)

    state = hass.states.get(entity_id(hass))
    assert state.state == "paused"
    assert state.attributes["media_title"] == "Easy Way Out"
    assert state.attributes["entity_picture"]


async def test_a_pause_shows_before_the_mesh_notices(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The link is pushed and the mesh is polled, so the link says it first."""
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(hass, entry, playback_state="paused", **TRACK)

    assert hass.states.get(entity_id(hass)).state == "paused"


async def test_audio_on_the_stream_beats_a_link_that_says_stopped(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Never report idle over music.

    A link that was not placed in the group reads `stopped` for ever. The mesh
    still says audio flows on the stream the speaker is on.
    """
    entry = await hand_to_unit(hass, fake_server, held_by_unit())

    await observe_stream(hass, entry, playback_state="stopped")

    assert hass.states.get(entity_id(hass)).state == "playing"


async def test_a_speaker_moved_to_none_is_idle_and_shows_no_track(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """None clears the card."""
    payload = held_by_unit()
    entry = await hand_to_unit(hass, fake_server, payload)
    await observe_stream(hass, entry, playback_state="playing", **TRACK)

    gone = json.loads(json.dumps(payload))
    gone["units"][0]["sources"][0]["player_ids"] = []
    await repoll(hass, entry, gone)

    state = hass.states.get(entity_id(hass))
    assert state.state == "idle"
    assert state.attributes["source"] == SOURCE_NONE
    for attribute in MEDIA_ATTRIBUTES:
        assert attribute not in state.attributes, attribute


async def test_a_speaker_nothing_can_see_is_unavailable(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Not held by us, in no mesh view, and no server plays to it."""
    await setup_with_mesh(hass, fake_server, FIXTURE)

    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await flush(hass)

    assert hass.states.get(entity_id(hass)).state == "unavailable"


async def test_a_speaker_another_server_plays_to_stays_available(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker handed to a server before a restart never yielded to us.

    The link that reads the server is then the one thing that says the speaker
    is there. It must stay available, with the server in `held_by`.
    """
    entry, _assign = await setup_with_mesh(hass, fake_server, FIXTURE)
    fake_server.clients_by_id[CLIENT_ID].is_connected = False
    fake_server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await flush(hass)

    with patch(
        "custom_components.sendspin.mesh.MeshClient.async_fetch_view",
        return_value=parse_view(FIXTURE),
    ):
        coordinator = entry.runtime_data.coordinator
        coordinator.async_note_mesh_host("192.168.7.226")
        await coordinator.async_refresh_mesh()
    coordinator.links["server:192.168.7.226"].snapshot = ControllerSnapshot(
        connected=True,
        server_id="ma",
        server_name="Music Assistant",
        playback_state="playing",
        title="I Remember",
    )
    coordinator.async_request_publish()
    await flush(hass)

    state = hass.states.get(entity_id(hass))
    assert state.state == "playing"
    assert state.attributes["held_by"] == "Music Assistant"
    assert state.attributes["source"] == "Music Assistant"
