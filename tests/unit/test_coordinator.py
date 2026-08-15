"""Tests for the push coordinator's two load-bearing invariants.

1. The endpoint set is the ADOPTED set, never the connected set.
2. Unknown is not zero.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

from aiosendspin.models.types import GoodbyeReason
from aiosendspin.noise.keys import Identity
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.sendspin.const import DOMAIN
from custom_components.sendspin.coordinator import SendspinCoordinator
from custom_components.sendspin.legacy_client import ControllerSnapshot
from custom_components.sendspin.mesh import parse_view
from custom_components.sendspin.player_memo import PlayerMemo
from custom_components.sendspin.server_host import ServerHost
from tests.fakes.fake_sendspin import FakeSendspinServer

URL = "ws://192.168.7.151:8928/sendspin"
CLIENT_ID = "98:A3:16:D0:9E:E8"


async def flush(hass: HomeAssistant) -> None:
    """Let the coordinator's debounced publish actually land.

    Events are coalesced with a short cooldown, so the trailing publish is on a
    timer that test time does not reach on its own.
    """
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()


@pytest.fixture
async def wired(
    hass: HomeAssistant,
) -> tuple[SendspinCoordinator, FakeSendspinServer, ServerHost, PlayerMemo]:
    """A coordinator over a fake server, tracking one adopted endpoint."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="hub")
    entry.add_to_hass(hass)
    server = FakeSendspinServer()
    host = ServerHost(hass, server, Identity.generate())
    memo = PlayerMemo(hass)
    await memo.async_load()
    coordinator = SendspinCoordinator(hass, entry, host, memo)
    coordinator.async_start()
    yield coordinator, server, host, memo
    # The debouncer holds a timer; leaving it armed leaks into the next test
    # exactly as it would leak across a config entry reload.
    coordinator.async_stop()


async def test_starts_unavailable_before_anything_is_observed(wired) -> None:
    """With no poll interval nothing else would ever clear this flag.

    Leaving it True would have every entity claim to be available before a
    single event has arrived.
    """
    coordinator, _server, _host, _memo = wired

    assert coordinator.last_update_success is False


async def test_a_disconnect_never_removes_an_endpoint(
    hass: HomeAssistant, wired
) -> None:
    """A speaker that drops off goes unavailable; it must not disappear.

    Pruning on disconnect would make "the network hiccupped" indistinguishable
    from "the user removed this speaker", and would churn the entity registry.
    """
    coordinator, server, host, _memo = wired
    server.attach(URL, CLIENT_ID, "Satellite1", volume=40, muted=False)
    coordinator.async_track_endpoint(URL)
    await host.async_adopt(URL)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)

    assert coordinator.data.endpoints[URL].connected is True

    server.clients_by_id[CLIENT_ID].is_connected = False
    server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await flush(hass)

    assert URL in coordinator.data.endpoints
    assert coordinator.data.endpoints[URL].connected is False

    await host.async_close()


async def test_only_un_adoption_removes_an_endpoint(hass: HomeAssistant, wired) -> None:
    """The single path by which an endpoint leaves the data set."""
    coordinator, server, _host, _memo = wired
    server.attach(URL, CLIENT_ID, "Satellite1")
    coordinator.async_track_endpoint(URL)
    await flush(hass)
    assert URL in coordinator.data.endpoints

    coordinator.async_forget_endpoint(URL)
    await flush(hass)

    assert URL not in coordinator.data.endpoints


async def test_an_unreported_volume_stays_none(hass: HomeAssistant, wired) -> None:
    """Unknown is not zero, and it is certainly not 100.

    A server can command a player's volume but cannot read it back unless the
    player echoes `client/state`. Inventing a value makes every speaker read
    100% while the audio is quieter — which looks like a UI bug and is not one.
    """
    coordinator, server, _host, _memo = wired
    server.attach(URL, CLIENT_ID, "Satellite1", volume=None, muted=None)
    coordinator.async_track_endpoint(URL)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)

    snapshot = coordinator.data.endpoints[URL]
    assert snapshot.volume is None
    assert snapshot.muted is None


async def test_a_reported_volume_is_carried_through(hass: HomeAssistant, wired) -> None:
    """When the speaker does echo its level, use it."""
    coordinator, server, _host, _memo = wired
    server.attach(URL, CLIENT_ID, "Satellite1", volume=35, muted=True)
    coordinator.async_track_endpoint(URL)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)

    snapshot = coordinator.data.endpoints[URL]
    assert snapshot.volume == 35
    assert snapshot.muted is True


async def test_the_handshake_name_is_learned_while_attached(
    hass: HomeAssistant, wired
) -> None:
    """The only moment the good name is visible is while the speaker is on.

    Once it detaches, all that remains is its mDNS instance name, which for
    third-party devices reads like `home-assistant-voice-a1b2c3`.
    """
    coordinator, server, _host, memo = wired
    server.attach(URL, CLIENT_ID, "FutureProofHomes - Satellite1")
    coordinator.async_track_endpoint(URL)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    await flush(hass)

    assert memo.display_name(URL) == "FutureProofHomes - Satellite1"
    assert coordinator.data.endpoints[URL].name == "FutureProofHomes - Satellite1"


async def test_yielding_is_reported_not_hidden(hass: HomeAssistant, wired) -> None:
    """An endpoint another server holds must say so, not look merely broken."""
    coordinator, server, host, _memo = wired
    server.attach(URL, CLIENT_ID, "Satellite1")
    coordinator.async_track_endpoint(URL)
    await host.async_adopt(URL)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))
    server.clients_by_id[CLIENT_ID].is_connected = False
    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await flush(hass)

    assert coordinator.data.endpoints[URL].yielded_reason == "another_server"

    await host.async_close()


# --- Controller links to Plum units ----------------------------------------
#
# Both cases below broke the same way when Plum-Audio moved to 9.1.x: a unit's
# `server_id` used to be a unit-scoped value and is now its X25519 public key.
# Anything that identified a unit by comparing those two stopped matching.

PLUM_KEY = "UDtWfFDLwBRGSZtv38GsSB1Rh9Dnc7aWJN0b53m781w"


def _stub_link(
    server_id: str | None,
    *,
    name: str | None = "Plum Amp100",
    connected: bool = True,
    title: str | None = None,
) -> MagicMock:
    """A controller link reporting a given identity, without a socket."""
    link = MagicMock()
    link.snapshot = ControllerSnapshot(
        server_name=name,
        server_id=server_id,
        connected=connected,
        title=title,
        playback_state="playing" if title else "stopped",
    )
    return link


async def test_a_source_link_is_not_a_duplicate_of_a_server_link(
    hass: HomeAssistant, wired
) -> None:
    """A link aimed at one source is not a redundant path to its unit.

    Observed on hardware: the dedupe closed `unit-7204:airplay-1` every poll
    because `server:[fd00:...]` reached the same `server_id`, the next sync
    rebuilt it, and a speaker's now-playing alternated between the routed track
    and nothing on a ten-second cycle — the rebuilt link reports no metadata
    until the server sends it, and it takes precedence while it exists.

    The two links share an identity but not a purpose: a `server:` link goes
    wherever the server puts it, a source link is deliberately placed in one
    group. Only `server:` links can be redundant with each other.
    """
    coordinator, *_ = wired
    coordinator._links["server:[fd00:1::dea6:32ff:fe2f:8080]"] = _stub_link(PLUM_KEY)
    coordinator._links["unit-7204:airplay-1"] = _stub_link(PLUM_KEY)

    coordinator._async_drop_duplicate_links()
    await hass.async_block_till_done()

    assert "unit-7204:airplay-1" in coordinator._links
    assert "server:[fd00:1::dea6:32ff:fe2f:8080]" in coordinator._links


async def test_a_second_address_for_one_server_is_still_dropped(
    hass: HomeAssistant, wired
) -> None:
    """The dedupe must still do its job for genuinely redundant server links."""
    coordinator, *_ = wired
    coordinator._links["server:192.168.7.204"] = _stub_link(PLUM_KEY)
    coordinator._links["server:[fd00:1::dea6:32ff:fe2f:8080]"] = _stub_link(PLUM_KEY)

    coordinator._async_drop_duplicate_links()
    await hass.async_block_till_done()

    assert list(coordinator._links) == ["server:192.168.7.204"]


async def test_a_unit_reached_by_a_second_address_is_not_a_foreign_server(
    hass: HomeAssistant, wired
) -> None:
    """A Plum unit is reachable by naming its streams, so it is not a destination.

    It is discovered over IPv6 as well as IPv4, so the host comparison misses it
    and identity has to catch it. Matching the link's `server_id` against the
    unit's `unit_id` used to work and no longer does, which put a bare "Plum
    Amp100" in the dropdown beside that unit's own "Plum Amp100 / 204 AP".
    """
    coordinator, *_ = wired
    coordinator._mesh_view = parse_view(
        {
            "units": [
                {
                    "unit_id": "unit-7204",
                    "name": "Plum Amp100",
                    "host": "192.168.7.204",
                    "server_id": PLUM_KEY,
                    "sources": [
                        {
                            "source_id": "airplay-1",
                            "name": "204 AP",
                            "active": True,
                            "group_id": "55e8f5b1",
                        }
                    ],
                }
            ]
        }
    )
    coordinator._links["server:[fd00:1::dea6:32ff:fe2f:8080]"] = _stub_link(PLUM_KEY)

    assert coordinator._foreign_servers() == ()


async def test_a_genuinely_foreign_server_is_still_offered(
    hass: HomeAssistant, wired
) -> None:
    """Music Assistant publishes no mesh API, and is the case the feature exists for."""
    coordinator, *_ = wired
    coordinator._mesh_view = parse_view(
        {
            "units": [
                {
                    "unit_id": "unit-7204",
                    "name": "Plum Amp100",
                    "host": "192.168.7.204",
                    "server_id": PLUM_KEY,
                    "sources": [{"source_id": "airplay-1", "name": "204 AP"}],
                }
            ]
        }
    )
    coordinator._links["server:192.168.7.226"] = _stub_link(
        "B3iRNtk3Bn-qzDVrf2-BhutRtpPQYyuVSamLSf4qZT4", name="Music Assistant"
    )

    assert coordinator._foreign_servers() == (
        ("B3iRNtk3Bn-qzDVrf2-BhutRtpPQYyuVSamLSf4qZT4", "Music Assistant"),
    )
