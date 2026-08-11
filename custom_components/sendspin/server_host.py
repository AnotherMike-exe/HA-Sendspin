"""Home Assistant's in-process Sendspin server.

Why Home Assistant hosts a server at all
----------------------------------------
A Sendspin *controller* client can send transport commands and observe the one
group it currently occupies. That is the entire wire surface: there is no
message that lists groups, lists players, or moves a player between groups.
Adopting and routing speakers only exists as in-process Python on a
`SendspinServer`. So to control generic Sendspin endpoints without requiring
any additional hardware on the network, Home Assistant has to be a server.

What this server deliberately is not
------------------------------------
- **Source-less.** `start_stream()` is never called, so no `PushStream` is ever
  created and no audio is encoded. `av` is consequently not a dependency.
- **Silent on mDNS.** `start_server()` is never called, so no `AsyncZeroconf`
  is constructed, nothing binds UDP 5353, and nothing is advertised. Discovery
  is Home Assistant core's job, via the `zeroconf` key in manifest.json.
- **Never listening.** Sendspin's direction of travel is server to player: a
  speaker runs a listener and the server dials it. We are always the dialer, so
  no inbound HTTP route is registered. (`SendspinServer.on_client_connect` is a
  public aiohttp handler if that ever changes.)

Upstream bugs worked around here
--------------------------------
Verified against aiosendspin 9.1.0, and documented in Plum-Audio's
`docs/HARD-WON-LESSONS.md` where they were found on hardware:

- `connect_to_client(url)` returns early when a dial task for that URL already
  exists. Re-dialling without stopping first is therefore a silent no-op that
  later times out claiming the device never connected.
- `disconnect_from_client(url)` pops the dial task and calls `.cancel()`
  without awaiting it. The message loop can swallow the cancellation and
  reconnect, and the doomed task's cleanup can pop the *replacement* task's
  registry entry, leaving several live dialers fighting over one socket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiosendspin.models.types import ConnectionReason, GoodbyeReason
from aiosendspin.server.server import (
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    SendspinEvent,
    SendspinServer,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiosendspin.noise.keys import Identity
    from aiosendspin.noise.trust_store import ServerPairingStore

_LOGGER = logging.getLogger(__name__)

# How long to wait for a cancelled dial task to actually finish before giving
# up and logging. Cancellation is normally immediate; this only bounds the
# pathological case described in the module docstring.
_DIAL_TEARDOWN_TIMEOUT_S = 5.0

# Goodbye reasons where continuing to dial is actively harmful.
#
# ANOTHER_SERVER is the one observed on hardware: dialling the Satellite1
# handshook, then got ANOTHER_SERVER and never recovered across 30s of retries,
# because Music Assistant re-dials harder. Two servers retrying at each other
# is a tug-of-war that degrades both. Politeness does not help — this happened
# with ConnectionReason.DISCOVERY, not PLAYBACK. See docs/OPEN-QUESTIONS.md §7.
#
# The others are terminal without user action, so retrying only generates noise.
#
# Everything else — SHUTDOWN, RESTART, USER_REQUEST, CONCURRENT_ATTEMPT, or no
# reason at all — is transient. A speaker rebooting or a flaky network must
# keep being retried, which is the whole point of retry_indefinitely.
_NON_RETRYABLE_GOODBYES: frozenset[GoodbyeReason] = frozenset(
    {
        GoodbyeReason.ANOTHER_SERVER,
        GoodbyeReason.UNAUTHORIZED,
        GoodbyeReason.PAIRING_REQUIRED,
        GoodbyeReason.UNPAIRED,
    }
)


def player_role(client: object) -> object | None:
    """Return a client's player role, or None if it has none.

    Volume and mute live on the role, not on the client. Looked up by *family*
    rather than by the exact `player@v1` id so a future `player@v2` still
    resolves.

    Note `get_player_volume()` returns `int | None`: a server can command a
    player's volume but cannot read it back unless the player echoes
    `client/state`, and many do not.
    """
    roles = client.roles_by_family("player")
    return roles[0] if roles else None


async def async_create_server_host(
    hass: HomeAssistant,
    *,
    identity: Identity,
    pairing_store: ServerPairingStore,
    server_name: str,
) -> ServerHost:
    """Construct the in-process Sendspin server.

    `allow_unencrypted=True` is required, not optional: every Sendspin client
    on a real network today predates the 8.0 Noise handshake — Plum-Audio's own
    player, sendspin-cpp 0.7.0 on ESP32 speakers, and Home Assistant Voice PE
    are all cleartext. Upstream marks this branch as transitional and intends
    to remove it eventually; see docs/OPEN-QUESTIONS.md.
    """
    server = SendspinServer(
        hass.loop,
        identity,
        server_name,
        async_get_clientsession(hass),
        pairing_store=pairing_store,
        allow_unencrypted=True,
    )
    return ServerHost(hass, server, identity)


class ServerHost:
    """Owns the Sendspin server object and the adoption lifecycle."""

    def __init__(
        self, hass: HomeAssistant, server: SendspinServer, identity: Identity
    ) -> None:
        """Initialise the host. Use `async_create_server_host` instead."""
        self.hass = hass
        self.server = server
        self.identity = identity
        self._adopted: set[str] = set()
        self._yielded: dict[str, GoodbyeReason] = {}
        self._url_by_client_id: dict[str, str] = {}
        self._unsubscribe: Callable[[], None] | None = server.add_event_listener(
            self._on_server_event
        )

    @property
    def server_id(self) -> str:
        """The identity peers see. Stable across restarts by construction."""
        return self.identity.peer_id

    @property
    def adopted_urls(self) -> frozenset[str]:
        """Dial URLs this host is currently holding or trying to hold."""
        return frozenset(self._adopted)

    @property
    def yielded_urls(self) -> dict[str, GoodbyeReason]:
        """Adopted URLs we have stopped dialling, and why.

        These are still adopted — the user asked for them — but another server
        holds them, or they need pairing. Surfacing this is the honest
        alternative to retrying forever and losing quietly.
        """
        return dict(self._yielded)

    def client_id_for_url(self, dial_url: str) -> str | None:
        """Resolve a dial URL to the client id currently answering on it.

        Always identify a dialled device this way. The obvious alternative —
        diffing `server.clients` before and after — works exactly once: an
        adopted speaker stays in the registry afterwards, so the second
        adoption of the same device finds no new id and reports failure.
        """
        return self.server.get_client_id_for_url(dial_url)

    async def async_adopt(self, dial_url: str) -> None:
        """Start holding a player, politely.

        `ConnectionReason.DISCOVERY` is deliberate. A `PLAYBACK` dial asserts a
        claim that a player is expected to honour over whatever currently holds
        it, and Home Assistant has no audio to justify that: adopting with
        PLAYBACK would silently take speakers away from Music Assistant. The
        explicit "take it anyway" path is `async_reclaim`, which the user has
        to ask for. See docs/OPEN-QUESTIONS.md §1.

        Stops any existing dialer first, because `connect_to_client` is a
        silent no-op while one exists.

        Calling this on a URL we previously yielded is the user asking us to
        try again, so the yield is cleared.
        """
        await self._async_stop_dialing(dial_url)
        self._adopted.add(dial_url)
        self._yielded.pop(dial_url, None)
        _LOGGER.debug("Adopting Sendspin player at %s", dial_url)
        self.server.connect_to_client(
            dial_url,
            connection_reason=ConnectionReason.DISCOVERY,
            retry_initial_connection=True,
            retry_indefinitely=True,
        )

    async def async_release(self, dial_url: str) -> None:
        """Stop holding a player so another server can have it."""
        self._adopted.discard(dial_url)
        self._yielded.pop(dial_url, None)
        _LOGGER.debug("Releasing Sendspin player at %s", dial_url)
        await self._async_stop_dialing(dial_url)

    async def async_reclaim(self, client_id: str, timeout_s: float = 30.0) -> bool:
        """Assert a playback claim on a player another server has taken.

        This is the escalation from a yielded adoption: the user has been told
        another server holds the speaker and has asked for it anyway.

        Synchronous upstream: it dials and arms a timeout, it does not wait for
        the player to land. Callers that need to know it arrived must poll.
        """
        if (url := self._url_by_client_id.get(client_id)) is not None:
            self._yielded.pop(url, None)
        return self.server.reclaim_client_for_playback(client_id, timeout_s=timeout_s)

    async def async_close(self) -> None:
        """Tear down every dialer, then the server.

        Dialers are stopped before `close()` so that a config entry reload does
        not leave a background task holding a websocket to a speaker that the
        reloaded entry then tries to dial again.
        """
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for dial_url in list(self._adopted):
            await self._async_stop_dialing(dial_url)
        self._adopted.clear()
        self._yielded.clear()
        self._url_by_client_id.clear()
        await self.server.close()

    def _on_server_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        """React to server events. Called synchronously on the event loop."""
        if isinstance(event, ClientConnectedEvent):
            if (url := self._url_for_client_id(event.client_id)) is not None:
                self._url_by_client_id[event.client_id] = url
                # Whatever made us yield, we are clearly holding it now.
                self._yielded.pop(url, None)
            return

        if not isinstance(event, ClientDisconnectedEvent):
            return
        if event.goodbye_reason not in _NON_RETRYABLE_GOODBYES:
            return  # transient — let retry_indefinitely do its job
        if (url := self._url_for_client_id(event.client_id)) is None:
            return
        if url not in self._adopted:
            return

        self._yielded[url] = event.goodbye_reason
        _LOGGER.info(
            "Sendspin player %s said goodbye (%s); giving up the dial rather "
            "than fighting for it. Use the reclaim service to take it anyway",
            url,
            event.goodbye_reason.value,
        )
        self.hass.async_create_task(
            self._async_stop_dialing(url), f"sendspin-stop-dial-{url}"
        )

    def _url_for_client_id(self, client_id: str) -> str | None:
        """Map a client id back to the URL we dial it on.

        Checked in order of reliability: what we recorded while connected, then
        upstream's registry, then a scan of our own adoptions. The last two can
        both come up empty once a client has fully disconnected, which is
        precisely when we need the answer.
        """
        if (url := self._url_by_client_id.get(client_id)) is not None:
            return url
        if (url := self.server.get_client_url(client_id)) is not None:
            return url
        for candidate in self._adopted:
            if self.server.get_client_id_for_url(candidate) == client_id:
                return candidate
        return None

    async def _async_stop_dialing(self, dial_url: str) -> None:
        """Stop a dial task and confirm it actually stopped.

        `disconnect_from_client` pops the task and calls `.cancel()` without
        awaiting it, so on return the task may still be running and may still
        reconnect. Taking our own reference first lets us await the cancellation
        and re-issue it until the task is genuinely done.

        Reading `_connection_tasks` reaches into aiosendspin's internals. There
        is no public accessor for "is a dial in flight for this URL", and doing
        this blind is what produced multiple live dialers fighting over one
        socket on real hardware.
        """
        task: asyncio.Task | None = self.server._connection_tasks.get(dial_url)
        self.server.disconnect_from_client(dial_url)
        if task is None:
            return

        # asyncio.wait rather than `await task`: awaiting a cancelled task
        # raises CancelledError, and suppressing that here would also swallow
        # cancellation of *this* coroutine during an entry unload.
        deadline = self.hass.loop.time() + _DIAL_TEARDOWN_TIMEOUT_S
        while not task.done() and self.hass.loop.time() < deadline:
            task.cancel()
            await asyncio.wait({task}, timeout=0.1)

        if not task.done():
            _LOGGER.warning(
                "Sendspin dial task for %s did not stop within %.0fs; it may "
                "reconnect on its own",
                dial_url,
                _DIAL_TEARDOWN_TIMEOUT_S,
            )
