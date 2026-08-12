"""Tests for the Plum-Audio mesh tier.

`mesh_view.json` is a real capture from a live two-unit mesh, not a
hand-written approximation — including the detail that every source reads
inactive, which is the normal resting state and the case a naive
"only show active sources" filter gets wrong.
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


def test_all_sources_are_listed_even_though_none_is_active() -> None:
    """The captured mesh is entirely idle, which is the normal resting state.

    Filtering the dropdown to active sources would leave it empty exactly when
    someone wants to assign a speaker and then start the music.
    """
    view = parse_view(FIXTURE)

    assert all(not s.active for s in view.sources)
    assert len(view.sorted_sources) == 6


def test_active_sources_sort_first() -> None:
    """Idle sources stay selectable, but what is playing is easiest to reach."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][1]["sources"][0]["active"] = True
    expected = payload["units"][1]["sources"][0]["name"]

    view = parse_view(payload)

    assert view.sorted_sources[0].name == expected
    assert view.sorted_sources[0].active is True


def test_a_speaker_is_matched_to_the_source_holding_it() -> None:
    """How an endpoint knows which stream it is on."""
    payload = json.loads(json.dumps(FIXTURE))
    payload["units"][0]["sources"][0]["player_ids"] = ["98:A3:16:D0:9E:E8"]

    view = parse_view(payload)

    assert view.source_for_player("98:A3:16:D0:9E:E8").source_id == "airplay-1"
    assert view.source_for_player("not-a-player") is None
    assert view.source_for_player(None) is None


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
