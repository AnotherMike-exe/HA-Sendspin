"""Tests for the hand-written pre-8.0 controller client.

The library's client cannot talk to any Sendspin server on a real network — it
always initiates the 8.0 Noise handshake and every server in the field predates
it. This speaks the older protocol directly, so the wire shapes here are the
contract, verified against live servers before being written.
"""

from __future__ import annotations

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
