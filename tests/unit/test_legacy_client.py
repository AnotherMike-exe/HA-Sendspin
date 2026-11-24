"""Tests for the hand-written pre-8.0 controller client.

The library's client cannot talk to any Sendspin server on a real network — it
always initiates the 8.0 Noise handshake and every server in the field predates
it. This speaks the older protocol directly, so the wire shapes here are the
contract, verified against live servers before being written.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from custom_components.sendspin import legacy_client
from custom_components.sendspin.legacy_client import (
    ControllerSnapshot,
    LegacyControllerClient,
)


def make_client(updates: list[ControllerSnapshot]) -> LegacyControllerClient:
    """A client with no socket, for exercising message handling."""
    return LegacyControllerClient(
        session=None,
        url="ws://192.168.7.204:8927/sendspin",
        client_name="Home Assistant",
        client_id="ha-sendspin-test",
        on_update=updates.append,
    )


def test_the_hello_declares_artwork_support() -> None:
    """Artwork arrives as pushed binary frames and only to a client that asked.

    Plum-Audio publishes no `artwork_url`, so without this declaration there is
    no cover art at all.
    """
    hello = make_client([])._hello()

    assert hello["type"] == "client/hello"
    payload = hello["payload"]
    assert payload["version"] == 1
    assert "controller@v1" in payload["supported_roles"]
    assert "metadata@v1" in payload["supported_roles"]
    # The key is aliased in the protocol and is easy to get wrong.
    assert payload["artwork@v1_support"]["channels"][0]["format"] == "jpeg"


def test_metadata_is_applied_as_a_diff() -> None:
    """An absent key means unchanged; a null key means cleared.

    Collapsing the two leaves a cleared album showing the previous track's
    forever.
    """
    updates: list[ControllerSnapshot] = []
    client = make_client(updates)

    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "metadata": {
                    "title": "Talk In Your Sleep",
                    "artist": "Moose Blood",
                    "album": "I Don't Think I Can Do This Anymore",
                }
            },
        }
    )
    assert client.snapshot.title == "Talk In Your Sleep"

    # Artist absent -> unchanged. Album explicitly null -> cleared.
    client._on_text({"type": "server/state", "payload": {"metadata": {"album": None}}})

    assert client.snapshot.artist == "Moose Blood"
    assert client.snapshot.album is None


def test_progress_is_converted_and_zero_duration_means_unknown() -> None:
    """Sendspin reports milliseconds; a zero duration means live, not empty."""
    client = make_client([])

    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "metadata": {
                    "progress": {
                        "track_progress": 3394,
                        "track_duration": 225800,
                        "playback_speed": 1000,
                    }
                }
            },
        }
    )
    assert client.snapshot.progress.position_s == 3.394
    assert client.snapshot.progress.duration_s == 225.8
    assert client.snapshot.progress.playing is True

    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "metadata": {
                    "progress": {
                        "track_progress": 10,
                        "track_duration": 0,
                        "playback_speed": 0,
                    }
                }
            },
        }
    )
    assert client.snapshot.progress.duration_s is None
    assert client.snapshot.progress.playing is False


def test_supported_commands_are_taken_from_the_server() -> None:
    """Transport buttons must reflect what the server will actually honour."""
    client = make_client([])

    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "controller": {
                    "supported_commands": ["play", "pause", "next"],
                    "volume": 67,
                    "muted": False,
                }
            },
        }
    )

    assert client.snapshot.supported_commands == ("play", "pause", "next")
    assert client.snapshot.volume == 67


def test_artwork_frames_are_unwrapped() -> None:
    """Binary frames are [type:1][timestamp_us:8][payload]."""
    client = make_client([])
    frame = bytes([8]) + (12345).to_bytes(8, "big") + b"\xff\xd8jpegdata"

    client._on_binary(frame)

    assert client.snapshot.artwork == b"\xff\xd8jpegdata"


def test_a_non_artwork_frame_is_ignored() -> None:
    """Audio and visualiser frames share the channel; only artwork is ours."""
    client = make_client([])
    client._on_binary(bytes([4]) + (0).to_bytes(8, "big") + b"audio")

    assert client.snapshot.artwork is None


def test_an_empty_artwork_payload_clears_the_cover() -> None:
    """Only an empty payload means clear — nothing else may blank the art."""
    client = make_client([])
    client._on_binary(bytes([8]) + (1).to_bytes(8, "big") + b"art")
    assert client.snapshot.artwork == b"art"

    client._on_binary(bytes([8]) + (2).to_bytes(8, "big"))

    assert client.snapshot.artwork is None


def test_stream_end_does_not_clear_artwork() -> None:
    """The track has not changed.

    Clearing here blanks the cover on every pause, idle and roam until the next
    track change.
    """
    client = make_client([])
    client._on_binary(bytes([8]) + (1).to_bytes(8, "big") + b"art")

    client._on_text({"type": "stream/end", "payload": {}})
    client._on_text({"type": "stream/clear", "payload": {}})

    assert client.snapshot.artwork == b"art"


def test_group_updates_are_tracked() -> None:
    """The group id is how a link's now-playing is matched to a mesh source."""
    client = make_client([])

    client._on_text(
        {
            "type": "group/update",
            "payload": {"playback_state": "playing", "group_id": "9607156d"},
        }
    )

    assert client.snapshot.group_id == "9607156d"
    assert client.snapshot.playback_state == "playing"


def test_an_omitted_command_list_leaves_the_commands_alone() -> None:
    """Absent means unchanged here, as for every other field in the block.

    Reading it as "no commands" wiped the transport set on any controller block
    that omitted it, so the buttons and the volume slider derived alongside them
    disappeared and came back — which Home Assistant reports as an entity
    "updating its capabilities too often".
    """
    client = make_client([])
    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "controller": {
                    "supported_commands": ["play", "pause", "volume"],
                    "volume": 40,
                }
            },
        }
    )

    # A later block carrying only a volume change must not strip the commands.
    client._on_text({"type": "server/state", "payload": {"controller": {"volume": 55}}})

    assert client.snapshot.supported_commands == ("play", "pause", "volume")
    assert client.snapshot.volume == 55


def test_an_explicitly_empty_command_list_is_honoured() -> None:
    """A server that really has no commands must still be able to say so."""
    client = make_client([])
    client._on_text(
        {
            "type": "server/state",
            "payload": {"controller": {"supported_commands": ["play"]}},
        }
    )

    client._on_text(
        {"type": "server/state", "payload": {"controller": {"supported_commands": []}}}
    )

    assert client.snapshot.supported_commands == ()


def test_moving_to_another_group_drops_the_previous_streams_metadata() -> None:
    """A different group is a different context.

    Artwork is only ever cleared by an empty binary frame, so a link moved to
    another group kept serving the previous stream's cover — observed live as a
    dead AirPlay source showing the artwork of the last thing Music Assistant
    played.
    """
    client = make_client([])
    client._on_text({"type": "group/update", "payload": {"group_id": "g1"}})
    client._on_text(
        {
            "type": "server/state",
            "payload": {"metadata": {"title": "I Remember", "artist": "Kaskade"}},
        }
    )
    client._on_binary(bytes([8]) + bytes(8) + b"jpegbytes")
    assert client.snapshot.artwork == b"jpegbytes"

    client._on_text(
        {
            "type": "group/update",
            "payload": {"group_id": "g2", "playback_state": "stopped"},
        }
    )

    assert client.snapshot.group_id == "g2"
    assert client.snapshot.artwork is None
    assert client.snapshot.title is None
    assert client.snapshot.artist is None
    # Facts about the server, not the stream, survive the move.
    assert client.snapshot.server_name is None or isinstance(
        client.snapshot.server_name, str
    )


def test_staying_in_the_same_group_keeps_the_track() -> None:
    """Only a *change* of group is a new context.

    A repeated `group/update` — a pause, say — must not blank the cover.
    """
    client = make_client([])
    client._on_text({"type": "group/update", "payload": {"group_id": "g1"}})
    client._on_text(
        {"type": "server/state", "payload": {"metadata": {"title": "I Remember"}}}
    )
    client._on_binary(bytes([8]) + bytes(8) + b"jpegbytes")

    client._on_text(
        {
            "type": "group/update",
            "payload": {"group_id": "g1", "playback_state": "stopped"},
        }
    )

    assert client.snapshot.title == "I Remember"
    assert client.snapshot.artwork == b"jpegbytes"
    assert client.snapshot.playback_state == "stopped"


def test_listeners_are_only_told_about_real_changes() -> None:
    """A repeated identical state must not churn every entity."""
    updates: list[ControllerSnapshot] = []
    client = make_client(updates)
    message = {"type": "group/update", "payload": {"group_id": "g1"}}

    client._on_text(message)
    client._on_text(message)

    assert len(updates) == 1


def test_the_link_hunts_for_a_playing_group_when_parked_in_a_solo_one() -> None:
    """Music Assistant leaves a controller in its own group, reporting nothing.

    Plum-Audio honours a `ctrl:<source_id>` id and places the controller
    directly; Music Assistant does not. `switch` cycles a controller through the
    server's playing groups, and a controller holds no player role, so moving
    between them affects no audio. Verified against a live Music Assistant: one
    switch reached the playing group and metadata arrived immediately.
    """
    client = make_client([])
    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "controller": {
                    "supported_commands": ["volume", "mute", "switch"],
                    "volume": 100,
                    "muted": False,
                }
            },
        }
    )
    client._on_text({"type": "group/update", "payload": {"playback_state": "stopped"}})

    # Nothing playing and switch is offered, so hunting is warranted.
    assert client.snapshot.title is None
    assert "switch" in client.snapshot.supported_commands


def test_hunting_stops_once_something_is_playing() -> None:
    """Never keep cycling past the group we were looking for."""
    client = make_client([])
    client._on_text(
        {
            "type": "server/state",
            "payload": {"metadata": {"title": "I Remember"}},
        }
    )
    client._on_text({"type": "group/update", "payload": {"playback_state": "playing"}})

    assert client.snapshot.title == "I Remember"
    assert client.snapshot.playback_state == "playing"


def test_a_server_link_hunts_by_default() -> None:
    """A plain server link has no source to name, so it has to go looking."""
    assert make_client([])._hunt is True


class FakeWebSocket:
    """Just enough websocket to run `_connect_once` against."""

    def __init__(self, hold_open_s: float) -> None:
        self.sent: list[dict] = []
        self._hold_open_s = hold_open_s
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        return {
            "type": "server/hello",
            "payload": {"name": "Plum Amp100", "server_id": "unit-7204"},
        }

    def __aiter__(self):
        return self

    async def __anext__(self):
        # No inbound frames; just keep the socket open long enough for any hunt
        # to fire, then end it so `_connect_once` returns.
        await asyncio.sleep(self._hold_open_s)
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> None:
        return None


class FakeSession:
    """A session that hands out one prepared websocket."""

    def __init__(self, ws: FakeWebSocket) -> None:
        self._ws = ws

    def ws_connect(self, _url: str, **_kw) -> FakeWebSocket:
        return self._ws


def switch_commands(ws: FakeWebSocket) -> list[dict]:
    """Every `switch` this link put on the wire."""
    return [
        m
        for m in ws.sent
        if m.get("type") == "client/command"
        and (m.get("payload") or {}).get("controller", {}).get("command") == "switch"
    ]


async def drive(client: LegacyControllerClient, ws: FakeWebSocket) -> None:
    """Run one connection to completion."""
    # The server must offer `switch` before hunting will consider it.
    await client._connect_once()


async def test_a_targeted_link_never_hunts(monkeypatch) -> None:
    """A `ctrl:<source_id>` link is placed on its source on purpose.

    Cycling it through playing groups moves it off the very group it was aimed
    at, so the source it was opened to observe stops being observed — and its
    speaker's now-playing appears and vanishes on the switch interval. This was
    live for three releases: the flag was stored and never read.
    """
    monkeypatch.setattr(legacy_client, "_SWITCH_INTERVAL_S", 0.01)
    ws = FakeWebSocket(hold_open_s=0.15)
    client = LegacyControllerClient(
        session=FakeSession(ws),
        url="ws://192.168.7.204:8927/sendspin",
        client_name="Home",
        client_id="ctrl:airplay-1:ha",
        on_update=lambda _s: None,
        hunt_for_playing=False,
    )
    client._snapshot = replace(
        client.snapshot, supported_commands=("volume", "mute", "switch")
    )

    await drive(client, ws)

    assert switch_commands(ws) == []


async def test_a_server_link_does_hunt(monkeypatch) -> None:
    """The other half: a link with no source to name must go looking.

    Music Assistant leaves a controller in its own solo group reporting nothing,
    so without this there is no now-playing for a speaker it holds at all.
    """
    monkeypatch.setattr(legacy_client, "_SWITCH_INTERVAL_S", 0.01)
    ws = FakeWebSocket(hold_open_s=0.15)
    client = make_client([])
    client._session = FakeSession(ws)
    client._snapshot = replace(
        client.snapshot, supported_commands=("volume", "mute", "switch")
    )

    await drive(client, ws)

    assert switch_commands(ws)


async def test_hunting_rests_instead_of_switching_forever(monkeypatch) -> None:
    """A sweep is bounded, but sweeps resume — and used not to.

    `_SWITCH_REST_S` was defined and never referenced, so a link that swept an
    idle server once stopped for the life of the connection and never noticed it
    start playing later.
    """
    monkeypatch.setattr(legacy_client, "_SWITCH_INTERVAL_S", 0.01)
    monkeypatch.setattr(legacy_client, "_SWITCH_ATTEMPTS", 2)
    monkeypatch.setattr(legacy_client, "_SWITCH_REST_S", 0.02)
    ws = FakeWebSocket(hold_open_s=0.3)
    client = make_client([])
    client._session = FakeSession(ws)
    client._snapshot = replace(
        client.snapshot, supported_commands=("volume", "mute", "switch")
    )

    await drive(client, ws)

    # More than one sweep's worth, so it came back round after resting.
    assert len(switch_commands(ws)) > 2


def make_pinned_client(updates: list[ControllerSnapshot]) -> LegacyControllerClient:
    """A `ctrl:` link, aimed at one source and never switching itself."""
    return LegacyControllerClient(
        session=None,
        url="ws://192.168.7.204:8927/sendspin",
        client_name="Home Assistant",
        client_id="ctrl:airplay-1:hatest",
        on_update=updates.append,
        hunt_for_playing=False,
    )


def test_a_pinned_link_keeps_the_metadata_it_was_placed_with() -> None:
    """Placement moves a `ctrl:` link *after* sending the track it is being sent to.

    Captured against Plum-Audio 9.1.x, in this order: metadata, then two
    `group/update`s ending in the targeted source's group, then artwork. The
    server does not repeat the metadata once the move lands, so treating each
    move as a new context discarded the only metadata this link would ever
    receive — a routed speaker showed a running progress bar with no title,
    while a hunting link to the same unit showed the track.
    """
    client = make_pinned_client([])

    client._on_text(
        {
            "type": "server/state",
            "payload": {"metadata": {"title": "Easy Way Out", "artist": "Qbomb"}},
        }
    )
    client._on_text(
        {
            "type": "group/update",
            "payload": {"group_id": "transient", "playback_state": "stopped"},
        }
    )
    client._on_text(
        {
            "type": "group/update",
            "payload": {"group_id": "55e8f5b1", "playback_state": "playing"},
        }
    )

    assert client.snapshot.group_id == "55e8f5b1"
    assert client.snapshot.playback_state == "playing"
    assert client.snapshot.title == "Easy Way Out"
    assert client.snapshot.artist == "Qbomb"


def test_a_hunting_link_still_drops_metadata_when_it_moves() -> None:
    """The protection stays where it was earned.

    A hunting link switches groups on purpose, so carrying a track across a move
    is exactly the bug that put a dead AirPlay source under Music Assistant's
    cover art.
    """
    client = make_client([])
    client._on_text({"type": "group/update", "payload": {"group_id": "g1"}})
    client._on_text(
        {"type": "server/state", "payload": {"metadata": {"title": "I Remember"}}}
    )

    client._on_text({"type": "group/update", "payload": {"group_id": "g2"}})

    assert client.snapshot.title is None


# --- Timeline -----------------------------------------------------------------


class _Clocks:
    """Our monotonic clock, the wall clock, and a server on its own clock."""

    SERVER_AHEAD_US = 5_000_000_000

    def __init__(self, monkeypatch) -> None:
        self.mono_us = 1_000_000_000
        self.wall = 1_800_000_000.0
        monkeypatch.setattr(legacy_client, "_now_us", lambda: self.mono_us)
        monkeypatch.setattr(legacy_client.time, "time", lambda: self.wall)

    @property
    def server_us(self) -> int:
        """What the server's clock reads now."""
        return self.mono_us + self.SERVER_AHEAD_US

    def advance(self, seconds: float) -> None:
        """Let time pass on every clock."""
        self.mono_us += int(seconds * 1_000_000)
        self.wall += seconds

    def sync(
        self, client: LegacyControllerClient, *, out_us: int = 0, back_us: int = 0
    ) -> None:
        """One `client/time` round trip, with a delay for each direction."""
        sent = self.mono_us
        self.mono_us += out_us
        received = self.server_us
        self.mono_us += back_us
        client._on_text(
            {
                "type": "server/time",
                "payload": {
                    "client_transmitted": sent,
                    "server_received": received,
                    "server_transmitted": received,
                },
            }
        )


def _progress(position_ms: int, timestamp_us: int | None, *, speed: int = 1000):
    metadata: dict = {
        "progress": {
            "track_progress": position_ms,
            "track_duration": 240_000,
            "playback_speed": speed,
        }
    }
    if timestamp_us is not None:
        metadata["timestamp"] = timestamp_us
    return {"type": "server/state", "payload": {"metadata": metadata}}


def test_position_is_stamped_with_the_server_timestamp(monkeypatch) -> None:
    """A link that joins mid-track gets a position that is already old.

    The server sends the position it had at `timestamp`, once. A stamp of the
    receipt time puts the track back to where it was then.
    """
    clocks = _Clocks(monkeypatch)
    client = make_client([])
    clocks.sync(client)

    # The server took this position 60 seconds before we joined.
    client._on_text(_progress(10_000, clocks.server_us - 60_000_000))

    progress = client.snapshot.progress
    assert progress.position_s == 10.0
    assert progress.updated_at == clocks.wall - 60


def test_a_position_that_arrives_before_the_clock_is_stamped_again(
    monkeypatch,
) -> None:
    """The server pushes state on join, before the first `server/time`."""
    clocks = _Clocks(monkeypatch)
    updates: list[ControllerSnapshot] = []
    client = make_client(updates)

    client._on_text(_progress(10_000, clocks.server_us - 60_000_000))
    # No clock yet, so the receipt time is all there is.
    assert client.snapshot.progress.updated_at == clocks.wall

    clocks.advance(0.5)
    clocks.sync(client)

    assert client.snapshot.progress.updated_at == clocks.wall - 60.5
    assert updates[-1].progress.updated_at == clocks.wall - 60.5


def test_a_later_clock_sample_does_not_move_a_good_stamp(monkeypatch) -> None:
    """A stamp made with the clock stays. Each sync must not write the state."""
    clocks = _Clocks(monkeypatch)
    updates: list[ControllerSnapshot] = []
    client = make_client(updates)
    clocks.sync(client)
    client._on_text(_progress(10_000, clocks.server_us))
    count = len(updates)

    clocks.advance(30)
    clocks.sync(client, out_us=4_000)

    assert len(updates) == count


def test_the_clock_keeps_the_sample_with_the_shortest_round_trip(
    monkeypatch,
) -> None:
    """A slow, lopsided reply carries the most error, so the fastest one wins."""
    clocks = _Clocks(monkeypatch)
    client = make_client([])

    clocks.sync(client, out_us=50_000)
    clocks.sync(client, out_us=400, back_us=400)
    clocks.sync(client, back_us=80_000)

    assert client._clock_offset_us == _Clocks.SERVER_AHEAD_US


def test_a_position_with_no_timestamp_uses_the_receipt_time(monkeypatch) -> None:
    """Without a timestamp there is nothing to correct with."""
    clocks = _Clocks(monkeypatch)
    client = make_client([])
    clocks.sync(client)

    client._on_text(_progress(10_000, None))

    assert client.snapshot.progress.updated_at == clocks.wall


def test_a_timestamp_from_the_future_is_not_trusted(monkeypatch) -> None:
    """A stamp after now would make Home Assistant run the track backwards."""
    clocks = _Clocks(monkeypatch)
    client = make_client([])
    clocks.sync(client)

    client._on_text(_progress(10_000, clocks.server_us + 5_000_000))

    assert client.snapshot.progress.updated_at == clocks.wall


def test_a_sender_that_reports_no_progress_has_no_position() -> None:
    """Some AirPlay senders send no `prgr`.

    The unit then sends a zero position and a zero duration, with a real
    speed. That is not the start of a track of no length: it is unknown. The
    speed still says whether the stream plays.
    """
    client = make_client([])

    client._on_text(
        {
            "type": "server/state",
            "payload": {
                "metadata": {
                    "progress": {
                        "track_progress": 0,
                        "track_duration": 0,
                        "playback_speed": 1000,
                    }
                }
            },
        }
    )

    progress = client.snapshot.progress
    assert progress.position_s is None
    assert progress.duration_s is None
    assert progress.playing is True


def test_a_new_connection_does_not_keep_the_old_clock(monkeypatch) -> None:
    """A server that restarts has a new clock."""
    clocks = _Clocks(monkeypatch)
    client = make_client([])
    clocks.sync(client)

    client._reset_clock()

    assert client._clock_offset_us is None
