"""Tests for the adoption lifecycle and the upstream workarounds it carries.

Every assertion here corresponds to a failure Plum-Audio hit on real hardware.
They are pinned against a fake that reproduces the upstream defects, so a
regression fails in CI instead of as a speaker that goes silent for no logged
reason.
"""

from __future__ import annotations

from aiosendspin.models.types import ConnectionReason, GoodbyeReason
from aiosendspin.noise.keys import Identity
from aiosendspin.server.server import ClientConnectedEvent, ClientDisconnectedEvent
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


async def test_adoption_asserts_a_playback_claim(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Adopting must dial with PLAYBACK, because DISCOVERY takes nothing.

    This reverses the integration's original choice, and the measurement that
    forced it is recorded in docs/SPEC-UPGRADE-PLAN.md §4a: dialled with
    DISCOVERY, every speaker on a real network refuses — ESPHome speakers with
    `ANOTHER_SERVER`, upgraded Plum players with `CONCURRENT_ATTEMPT`. The same
    speakers accept a PLAYBACK dial and hand over their full role set.

    DISCOVERY was chosen as politeness, on the belief that PLAYBACK would rudely
    take a speaker DISCOVERY would leave alone. The opposite holds: DISCOVERY
    yields no gentler outcome, it yields no outcome. The protection that
    actually matters is untouched — nothing is ever auto-dialled, and
    ANOTHER_SERVER still ends the dial rather than starting a tug-of-war.
    """
    host, server = make_host(hass, identity)

    await host.async_adopt(PLAYER_URL)

    assert len(server.dial_calls) == 1
    call = server.dial_calls[0]
    assert call.connection_reason is ConnectionReason.PLAYBACK
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


async def test_reclaim_redials_the_speaker_for_playback(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Reclaim is the explicit, user-driven way to take a speaker.

    It goes through the same dialling path as adoption rather than upstream's
    `reclaim_client_for_playback`. That call resolves the URL from the library's
    own client registry, which we now evict on yield — so routing reclaim
    through it would make "take it anyway" fail precisely for the speakers that
    were given up, which is the only case it exists for.
    """
    host, server = make_host(hass, identity)
    server.client_ids_by_url[PLAYER_URL] = "98:A3:16:D0:9E:E8"

    assert await host.async_reclaim("98:A3:16:D0:9E:E8", timeout_s=5.0) is True

    assert server.dials_started == [PLAYER_URL]
    assert server.dial_calls[-1].connection_reason is ConnectionReason.PLAYBACK

    await host.async_close()


async def test_reclaim_of_an_unknown_speaker_reports_failure(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A client id we cannot resolve to a URL is not silently ignored."""
    host, server = make_host(hass, identity)

    assert await host.async_reclaim("never:seen:this:one", timeout_s=5.0) is False
    assert server.dials_started == []


async def test_reclaim_clears_a_previous_yield(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Reclaim after giving up must genuinely try again."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)
    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()
    assert host.yielded_urls

    assert await host.async_reclaim(CLIENT_ID) is True

    assert host.yielded_urls == {}
    assert server.live_dial_urls == {PLAYER_URL}

    await host.async_close()


async def test_diagnostics_expose_the_handshake_facts(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Roles and trust must be readable without attaching a debugger.

    Every one of these decided something during the fleet measurement in
    docs/SPEC-UPGRADE-PLAN.md and none of them were visible from Home Assistant
    at the time.
    """
    host, server = make_host(hass, identity)
    server.attach(PLAYER_URL, "98:A3:16:D0:9E:E8", "Satellite1")

    report = host.client_diagnostics()

    assert len(report) == 1
    assert report[0]["client_id"] == "98:A3:16:D0:9E:E8"
    assert report[0]["name"] == "Satellite1"
    assert report[0]["connected"] is True
    assert report[0]["active_roles"] == ["player@v1"]
    # Absent on a legacy connection rather than an error.
    assert report[0]["trust_level"] is None
    assert report[0]["last_goodbye"] is None


async def test_diagnostics_show_an_inert_client(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A connected speaker with no roles is the unpaired-encrypted signature.

    It is admitted rather than refused, so it reports available and looks
    adopted while doing nothing at all. This is the only place that shows it.
    """
    host, server = make_host(hass, identity)
    server.attach(PLAYER_URL, "98:A3:16:D0:9E:E8", "Plum", active_role_ids=[])

    report = host.client_diagnostics()

    assert report[0]["connected"] is True
    assert report[0]["active_roles"] == []


# --- Yielding to another server --------------------------------------------
#
# Observed on hardware (docs/OPEN-QUESTIONS.md §7): dialling the Satellite1
# completed a handshake, then got GoodbyeReason.ANOTHER_SERVER and never
# recovered across 30s of retries, because Music Assistant re-dials harder.
# Two servers retrying at each other degrades both.

CLIENT_ID = "98:A3:16:D0:9E:E8"


async def adopt_and_connect(
    host: ServerHost, server: FakeSendspinServer, url: str = PLAYER_URL
) -> None:
    """Adopt a URL and let the host see the client connect on it."""
    server.client_ids_by_url[url] = CLIENT_ID
    await host.async_adopt(url)
    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))


async def test_another_server_goodbye_stops_the_dial(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Losing a speaker to another server must end the tug-of-war."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()

    assert server.live_dial_urls == set()
    # Still adopted: the user asked for it, and the entity should say why it
    # is not held rather than silently disappearing.
    assert PLAYER_URL in host.adopted_urls
    assert host.yielded_urls == {PLAYER_URL: "another_server"}


async def test_concurrent_attempt_stops_the_dial_immediately(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A player that refuses because another server holds it has answered.

    Measured against an upgraded Plum player (docs/SPEC-UPGRADE-PLAN.md §3a):
    the dial is refused with CONCURRENT_ATTEMPT roughly 5ms after connecting,
    for any connection reason, and no retry can change the outcome — the
    incumbent wins on rank and we cannot raise ours without pairing.

    Upstream marks this reason retryable, which is defensible for a server
    polling for a speaker to free up and wrong for us: our retry is indefinite
    and our rank cannot improve. Without this the flap counter eventually stops
    it, but only after three rounds of hammering a speaker in use, and it
    reports "contested" where the player said something exact.
    """
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.CONCURRENT_ATTEMPT
        )
    )
    await hass.async_block_till_done()

    assert server.live_dial_urls == set()
    assert host.yielded_urls == {PLAYER_URL: "concurrent_attempt"}
    # Still adopted: the user asked for it, and the entity should say why.
    assert PLAYER_URL in host.adopted_urls


async def test_a_transient_goodbye_keeps_retrying(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A speaker rebooting must not be abandoned.

    This is the other half of the rule: giving up on ANOTHER_SERVER is only
    correct because we keep retrying everything else. A speaker that reboots
    or drops off Wi-Fi has to come back on its own.
    """
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.RESTART
        )
    )
    await hass.async_block_till_done()

    assert server.live_dial_urls == {PLAYER_URL}
    assert host.yielded_urls == {}

    await host.async_close()


async def test_an_unattributed_disconnect_keeps_retrying(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A dropped socket carries no goodbye reason at all — keep dialling."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await hass.async_block_till_done()

    assert server.live_dial_urls == {PLAYER_URL}

    await host.async_close()


async def test_re_adopting_a_yielded_player_tries_again(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Adopting a yielded URL is the user asking us to have another go."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)
    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()
    assert host.yielded_urls

    await host.async_adopt(PLAYER_URL)

    assert host.yielded_urls == {}
    assert server.live_dial_urls == {PLAYER_URL}

    await host.async_close()


async def test_reconnecting_clears_the_yield(
    hass: HomeAssistant, identity: Identity
) -> None:
    """If we end up holding it after all, stop claiming we yielded it."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)
    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()

    server.emit(ClientConnectedEvent(client_id=CLIENT_ID))

    assert host.yielded_urls == {}

    await host.async_close()


async def test_a_goodbye_for_an_unadopted_player_is_ignored(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Other servers' clients are none of our business."""
    host, server = make_host(hass, identity)
    server.client_ids_by_url[OTHER_URL] = "aa:bb:cc:dd:ee:ff"

    server.emit(
        ClientDisconnectedEvent(
            client_id="aa:bb:cc:dd:ee:ff", goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()

    assert host.yielded_urls == {}


async def test_yielding_forgets_the_client(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Giving up a speaker must release the client object with it.

    Upstream retains a client after ANOTHER_SERVER rather than cleaning it up,
    setting `_cleanup_on_mdns_removal` and waiting for its own mDNS browser to
    fire. This integration never starts that browser — it is deliberately silent
    on mDNS — so nothing ever reads the flag and retained clients accumulate for
    the life of the process.
    """
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(
        ClientDisconnectedEvent(
            client_id=CLIENT_ID, goodbye_reason=GoodbyeReason.ANOTHER_SERVER
        )
    )
    await hass.async_block_till_done()

    assert server.removed_clients == [CLIENT_ID]
    # The URL mapping survives, because reclaim needs it after the client is gone.
    assert host.client_id_for_url(PLAYER_URL) == CLIENT_ID


async def test_a_retryable_disconnect_keeps_the_client(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A speaker we are still dialling must not be forgotten mid-retry."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await hass.async_block_till_done()

    assert server.removed_clients == []

    await host.async_close()


async def test_close_unhooks_the_event_listener(
    hass: HomeAssistant, identity: Identity
) -> None:
    """A reload must not leave the previous host reacting to events."""
    host, server = make_host(hass, identity)
    assert server.listener_count == 1

    await host.async_close()

    assert server.listener_count == 0


# --- Flapping --------------------------------------------------------------
#
# Observed on the live network: Home Assistant and a Plum-Audio unit both held
# retrying dials against the same player. It dropped the socket with
# close_code=None and NO goodbye at all, so the reason-based rule never fired
# and the two servers traded the speaker back and forth indefinitely.


async def test_a_flapping_speaker_is_given_up_even_without_a_goodbye(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Churn is itself evidence that something else wants the speaker."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    for _ in range(3):
        server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
        await hass.async_block_till_done()

    assert host.yielded_urls == {PLAYER_URL: "contested"}
    assert server.live_dial_urls == set()
    # Still adopted — the user asked for it, and the entity should explain.
    assert PLAYER_URL in host.adopted_urls


async def test_an_occasional_drop_is_not_flapping(
    hass: HomeAssistant, identity: Identity
) -> None:
    """One or two drops is a flaky network, not a contest.

    Giving up too eagerly would abandon a speaker on a marginal Wi-Fi link,
    which is exactly the case retry_indefinitely exists for.
    """
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)

    for _ in range(2):
        server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
        await hass.async_block_till_done()

    assert host.yielded_urls == {}
    assert server.live_dial_urls == {PLAYER_URL}

    await host.async_close()


async def test_re_adopting_forgives_past_flapping(
    hass: HomeAssistant, identity: Identity
) -> None:
    """Asking again must start from a clean slate, not stay one drop from giving up."""
    host, server = make_host(hass, identity)
    await adopt_and_connect(host, server)
    for _ in range(3):
        server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
        await hass.async_block_till_done()
    assert host.yielded_urls

    await host.async_adopt(PLAYER_URL)
    server.emit(ClientDisconnectedEvent(client_id=CLIENT_ID, goodbye_reason=None))
    await hass.async_block_till_done()

    assert host.yielded_urls == {}
    assert server.live_dial_urls == {PLAYER_URL}

    await host.async_close()
