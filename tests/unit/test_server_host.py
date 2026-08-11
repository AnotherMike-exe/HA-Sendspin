"""Tests for the adoption lifecycle and the upstream workarounds it carries.

Every assertion here corresponds to a failure Plum-Audio hit on real hardware.
They are pinned against a fake that reproduces the upstream defects, so a
regression fails in CI instead of as a speaker that goes silent for no logged
reason.
"""

from __future__ import annotations

from aiosendspin.models.types import ConnectionReason
from aiosendspin.noise.keys import Identity
from homeassistant.core import HomeAssistant
import pytest

from custom_components.sendspin.server_host import ServerHost
from tests.fakes.fake_sendspin import FakeSendspinServer

PLAYER_URL = "ws://192.168.7.211:8928/sendspin"
OTHER_URL = "ws://192.168.7.212:8928/sendspin"


@pytest.fixture
def identity() -> Identity:
    """A throwaway server identity."""
    return Identity.generate()


def make_host(
    hass: HomeAssistant, identity: Identity, **kwargs
) -> tuple[ServerHost, FakeSendspinServer]:
    """Build a ServerHost over a fake server."""
    server = FakeSendspinServer(**kwargs)
    return ServerHost(hass, server, identity), server


async def test_adoption_is_polite_by_default(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Adopting must dial with DISCOVERY, never PLAYBACK.

    A PLAYBACK dial asserts a claim a player is expected to honour over
    whatever currently holds it. Home Assistant has no audio to justify that,
    so adopting with PLAYBACK would silently take speakers away from Music
    Assistant. Taking a speaker is `async_reclaim`, and the user has to ask.
    """
    host, server = make_host(hass, identity)

    await host.async_adopt(PLAYER_URL)

    assert len(server.dial_calls) == 1
    call = server.dial_calls[0]
    assert call.connection_reason is ConnectionReason.DISCOVERY
    # A speaker that is briefly unreachable must still be picked up later.
    assert call.retry_initial_connection is True
    assert call.retry_indefinitely is True

    await host.async_close()


async def test_re_adopting_actually_re_dials(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Adopting an already-adopted URL must start a genuinely new dial.

    `connect_to_client` is a silent no-op while a dial task for that URL
    exists, so adopting without stopping the previous dialer first does
    nothing and then times out claiming the device never connected.
    """
    host, server = make_host(hass, identity)

    await host.async_adopt(PLAYER_URL)
    await host.async_adopt(PLAYER_URL)

    assert server.dials_started == [PLAYER_URL, PLAYER_URL]

    await host.async_close()


async def test_release_leaves_no_live_dialer(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Releasing must actually stop dialling, so another server can take it."""
    host, server = make_host(hass, identity)
    await host.async_adopt(PLAYER_URL)

    await host.async_release(PLAYER_URL)

    assert server.live_dial_urls == set()
    assert PLAYER_URL not in host.adopted_urls


async def test_a_dialer_that_ignores_cancellation_is_cancelled_again(
    hass: HomeAssistant, identity: Identity
) -> None:
    """`disconnect_from_client` cancels without awaiting, so verify and retry.

    Upstream pops the task and fires `.cancel()` at it. A message loop that
    swallows the cancellation reconnects, and its cleanup can evict the
    *replacement* task's registry entry — which is how several live dialers end
    up fighting over one socket.
    """
    host, server = make_host(hass, identity, stubborn_cancels=3)
    await host.async_adopt(PLAYER_URL)

    await host.async_release(PLAYER_URL)

    assert server.live_dial_urls == set()


async def test_close_stops_every_dialer_before_closing(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A reload must not leave dialers racing the new entry for the same speakers."""
    host, server = make_host(hass, identity)
    await host.async_adopt(PLAYER_URL)
    await host.async_adopt(OTHER_URL)

    await host.async_close()

    assert server.live_dial_urls == set()
    assert server.closed is True
    assert host.adopted_urls == frozenset()


async def test_devices_are_identified_by_url_not_by_set_difference(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Resolving a dial URL to a client id must keep working on re-adoption.

    Diffing `server.clients` before and after a dial works exactly once: an
    adopted speaker stays in the registry, so the second adoption finds no new
    id and reports a failure about a device that is plainly connected.
    """
    host, server = make_host(hass, identity)
    server.client_ids_by_url[PLAYER_URL] = "98:A3:16:D0:9E:E8"

    await host.async_adopt(PLAYER_URL)
    assert host.client_id_for_url(PLAYER_URL) == "98:A3:16:D0:9E:E8"

    await host.async_release(PLAYER_URL)
    await host.async_adopt(PLAYER_URL)
    assert host.client_id_for_url(PLAYER_URL) == "98:A3:16:D0:9E:E8"

    await host.async_close()


async def test_reclaim_asserts_a_playback_claim(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Reclaim is the explicit, user-driven way to take a speaker."""
    host, server = make_host(hass, identity)

    assert await host.async_reclaim("98:A3:16:D0:9E:E8", timeout_s=5.0) is True
    assert server.reclaim_calls == [("98:A3:16:D0:9E:E8", 5.0)]
