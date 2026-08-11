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

from aiosendspin.models.types import ConnectionReason
from aiosendspin.server.server import SendspinServer
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from aiosendspin.noise.keys import Identity
    from aiosendspin.noise.trust_store import ServerPairingStore

_LOGGER = logging.getLogger(__name__)

# How long to wait for a cancelled dial task to actually finish before giving
# up and logging. Cancellation is normally immediate; this only bounds the
# pathological case described in the module docstring.
_DIAL_TEARDOWN_TIMEOUT_S = 5.0


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

    @property
    def server_id(self) -> str:
        """The identity peers see. Stable across restarts by construction."""
        return self.identity.peer_id

    @property
    def adopted_urls(self) -> frozenset[str]:
        """Dial URLs this host is currently holding or trying to hold."""
        return frozenset(self._adopted)

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
        """
        await self._async_stop_dialing(dial_url)
        self._adopted.add(dial_url)
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
        _LOGGER.debug("Releasing Sendspin player at %s", dial_url)
        await self._async_stop_dialing(dial_url)

    async def async_reclaim(self, client_id: str, timeout_s: float = 30.0) -> bool:
        """Assert a playback claim on a player another server has taken.

        Synchronous upstream: it dials and arms a timeout, it does not wait for
        the player to land. Callers that need to know it arrived must poll.
        """
        return self.server.reclaim_client_for_playback(client_id, timeout_s=timeout_s)

    async def async_close(self) -> None:
        """Tear down every dialer, then the server.

        Dialers are stopped before `close()` so that a config entry reload does
        not leave a background task holding a websocket to a speaker that the
        reloaded entry then tries to dial again.
        """
        for dial_url in list(self._adopted):
            await self._async_stop_dialing(dial_url)
        self._adopted.clear()
        await self.server.close()

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
