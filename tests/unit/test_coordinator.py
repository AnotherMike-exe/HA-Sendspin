"""Tests for the push coordinator's two load-bearing invariants.

1. The endpoint set is the ADOPTED set, never the connected set.
2. Unknown is not zero.
"""

from __future__ import annotations

from datetime import timedelta

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
