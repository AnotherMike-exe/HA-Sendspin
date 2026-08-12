"""Tests for the Plum-Audio mesh tier.

`mesh_view.json` is a real capture from a live two-unit mesh, not a
hand-written approximation — including the detail that every source reads
inactive while nothing is playing. A unit publishes every *configured* input
regardless, so that capture is six sources and zero live streams, which is why
the dropdown filters rather than listing what the API returns.
"""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.sendspin.mesh import MeshView, parse_view

FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "mesh_view.json").read_text()
)


def test_parses_every_source_across_every_unit() -> None:
    """Both units and all six of their sources survive the round trip."""
    view = parse_view(FIXTURE)

    assert view.reachable is True
    assert len(view.sources) == 6
    assert {s.unit_id for s in view.sources} == {"unit-7204", "unit-7122"}


def test_a_source_is_identified_by_unit_and_id_together() -> None:
    """`source_id` is only unique WITHIN a unit.

    Both units call their AirPlay endpoint `airplay-1`. Keying on the bare id
    would collide, and a routing call posted against the wrong unit silently
    routes locally instead of where the user asked.
    """
    view = parse_view(FIXTURE)

    airplay = sorted(s.key for s in view.sources if s.source_id == "airplay-1")
    assert airplay == ["unit-7122:airplay-1", "unit-7204:airplay-1"]


def test_labels_name_the_unit_so_they_are_distinguishable() -> None:
    """Two sources called the same thing must still be tellable apart."""
    view = parse_view(FIXTURE)

    labels = {s.label for s in view.sources}
    assert "Plum Amp100 / 204 AP" in labels
    assert "Plum RackPi / VLAN7 AirPlay" in labels
    assert len(labels) == 6


def test_an_idle_mesh_offers_no_live_sources() -> None:
    """Every configured input is published; none of them is a stream yet.

    The captured mesh has six sources and nothing feeding any of them. Offering
    all six put an AirPlay endpoint with no sender in the dropdown as though a
    speaker could usefully be put on it.
    """
    view = parse_view(FIXTURE)

    assert len(view.sources) == 6
    assert view.live_sources == []


def test_only_a_fed_source_is_live() -> None:
    """A sender arriving is what turns a configured input into a stream."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][1]["sources"][0]["active"] = True
    payload["units"][1]["sources"][0]["streaming"] = True
    expected = payload["units"][1]["sources"][0]["name"]

    view = parse_view(payload)

    assert [s.name for s in view.live_sources] == [expected]


def test_a_paused_sender_stays_live() -> None:
    """`streaming` drops on a pause; `active` is what keeps the source usable.

    Filtering on `streaming` alone would make a paused stream vanish from the
    dropdown mid-track, taking the user's selection with it.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["active"] = True
    payload["units"][0]["sources"][0]["streaming"] = False

    assert [s.source_id for s in parse_view(payload).live_sources] == ["airplay-1"]


def test_streaming_without_active_is_still_live() -> None:
    """Either flag suffices; they are not always set together."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][1]["streaming"] = True

    assert [s.source_id for s in parse_view(payload).live_sources] == ["bluetooth-1"]


def test_live_sources_are_ordered_by_label() -> None:
    """Stable, predictable dropdown order once nothing is idle to sort around."""
    payload = json.loads(json.dumps(FIXTURE))
    for unit in payload["units"]:
        for source in unit["sources"]:
            source["active"] = True

    labels = [s.label for s in parse_view(payload).live_sources]

    assert labels == sorted(labels, key=str.lower)


def test_a_speaker_is_matched_to_the_source_holding_it() -> None:
    """How an endpoint knows which stream it is on."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = ["98:A3:16:D0:9E:E8"]

    view = parse_view(payload)

    assert view.source_for_player("98:A3:16:D0:9E:E8").source_id == "airplay-1"
    assert view.source_for_player("not-a-player") is None
    assert view.source_for_player(None) is None


def test_a_speaker_is_matched_by_its_group_when_player_ids_are_empty() -> None:
    """The membership signal that actually exists on live hardware.

    Every unit on the real mesh reports `player_ids: []` and `players: []`, so
    the direct membership list never matches and a routed speaker read as being
    on no stream. Its `local_player.group_id` is the join key: it equals the
    `group_id` of whichever source it is on, and it moves whenever *anything*
    re-routes the speaker — which is how a change made from another server's UI
    becomes visible here.
    """
    payload = json.loads(json.dumps(FIXTURE))
    source = payload["units"][0]["sources"][0]
    assert source["player_ids"] == []
    payload["units"][0]["local_player"]["group_id"] = source["group_id"]

    view = parse_view(payload)

    assert view.source_for_player("player-7204").source_id == "airplay-1"


def test_a_speaker_in_a_group_belonging_to_no_source_is_on_no_stream() -> None:
    """A solo group, or a group on a foreign server, is the none state.

    This is what makes a return to none detectable rather than sticky.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["local_player"]["group_id"] = "a-group-no-source-owns"

    assert parse_view(payload).source_for_player("player-7204") is None


def test_the_group_of_a_source_is_parsed() -> None:
    """The other half of the join, and a direct lookup for it."""
    view = parse_view(FIXTURE)
    source = view.source_for_group("9607156d-57d5-488f-803f-8539638f535e")

    assert source is not None
    assert source.key == "unit-7204:airplay-1"
    assert view.source_for_group("nothing-owns-this") is None
    assert view.source_for_group(None) is None


def test_missing_fields_do_not_break_parsing() -> None:
    """The API carries no version marker and no schema guarantee.

    Upstream handles forward compatibility purely with defaults on absent keys;
    anything stricter here would break on the next unit that ships.
    """
    view = parse_view({"units": [{"unit_id": "u1", "sources": [{"source_id": "s1"}]}]})

    source = view.sources[0]
    assert source.name == "s1"  # falls back to the id rather than being blank
    assert source.unit_name == "u1"
    assert source.active is False
    assert source.player_ids == ()
    assert source.supports_source_volume is False


def test_junk_entries_are_skipped_rather_than_crashing() -> None:
    """A malformed unit must not take the whole view down."""
    view = parse_view(
        {
            "units": [
                {"name": "no id"},
                {"unit_id": "u1", "sources": [{"name": "no id"}]},
            ]
        }
    )

    assert view.sources == ()
    assert view.reachable is True


def test_an_unreachable_mesh_is_not_an_empty_mesh() -> None:
    """A failed fetch must never read as every stream having ended.

    This is the distinction that stops a dropped poll from wiping the source
    list and yanking every user's selection.
    """
    unreachable = MeshView(reachable=False)

    assert unreachable.reachable is False
    assert unreachable.sources == ()


def test_a_speaker_can_be_identified_without_ever_holding_it() -> None:
    """The mesh reports the address it dials each speaker on.

    This is the only way to identify a speaker another server holds: its client
    id is a MAC or an opaque id visible only during a handshake with *us*, and
    a speaker someone else holds never handshakes with us. Without this it
    matches no source, so it reports no stream, offers no controls, and reads
    as unavailable while being present and probably playing.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["players"] = [
        {
            "player_id": "08:B6:1F:B7:AF:5C",
            "name": "esparagus-hifi-1",
            "url": "ws://192.168.7.201:8928/sendspin",
            "connected": True,
            "volume": 100,
        }
    ]

    view = parse_view(payload)
    found = view.player_by_url("ws://192.168.7.201:8928/sendspin")

    assert found.player_id == "08:B6:1F:B7:AF:5C"
    assert view.player_by_url("ws://192.168.7.99:8928/sendspin") is None
    assert view.player_by_url(None) is None


def test_a_units_own_speaker_is_identifiable_even_on_a_foreign_server() -> None:
    """`local_player` is the only record of a unit's own speaker when another
    server has taken it — and that is exactly when we need its identity.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["players"] = []
    payload["units"][0]["local_player"] = {
        "player_id": "player-7204",
        "name": "Plum Amp100",
        "url": "ws://192.168.7.204:8928/sendspin",
        "attached": True,
        "server_name": "Music Assistant",
    }

    view = parse_view(payload)
    found = view.player_by_url("ws://192.168.7.204:8928/sendspin")

    assert found is not None
    assert found.player_id == "player-7204"


def test_a_units_own_speaker_is_not_listed_twice() -> None:
    """It appears in both lists when the unit itself holds it."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["players"] = [
        {"player_id": "player-7204", "url": "ws://192.168.7.204:8928/sendspin"}
    ]
    payload["units"][0]["local_player"] = {
        "player_id": "player-7204",
        "url": "ws://192.168.7.204:8928/sendspin",
        "attached": True,
    }

    view = parse_view(payload)

    assert len([p for p in view.players if p.player_id == "player-7204"]) == 1


def test_the_holding_server_is_recorded_for_attribution() -> None:
    """`local_player.server_id` and `server/hello`'s server_id are the same value.

    That equality is what attaches a server's now-playing to the speakers it is
    holding — including a server that is not a Plum unit at all.
    """
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["local_player"] = {
        "player_id": "player-7204",
        "url": "ws://192.168.7.204:8928/sendspin",
        "attached": True,
        "server_name": "Music Assistant",
        "server_id": "1d95425e51ef4db8b578d1b010c33414",
    }

    found = parse_view(payload).player_by_url("ws://192.168.7.204:8928/sendspin")

    assert found.held_by == "Music Assistant"
    assert found.held_by_server_id == "1d95425e51ef4db8b578d1b010c33414"
