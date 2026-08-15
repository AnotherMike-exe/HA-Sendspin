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
# is a tug-of-war that degrades both. See docs/OPEN-QUESTIONS.md §7.
#
# CONCURRENT_ATTEMPT is the upgraded-fleet equivalent, measured against a Plum
# player on 9.1.x (docs/SPEC-UPGRADE-PLAN.md §3a): the player refuses roughly
# 5ms after connecting because another server already holds it, and it does so
# for *any* connection reason. Upstream marks this reason retryable
# (server/connection.py:341-356, "may retry later"), which is reasonable for a
# server polling for a speaker to free up and wrong here — our retry is
# indefinite, and our rank cannot improve without a pairing record. Left out of
# this set the flap counter below eventually catches it, but only after three
# rounds of hammering a speaker somebody is using, and it reports the vague
# "contested" where the player told us something exact.
#
# The others are terminal without user action, so retrying only generates noise.
#
# Everything else — SHUTDOWN, RESTART, USER_REQUEST, or no reason at all — is
# transient. A speaker rebooting or a flaky network must keep being retried,
# which is the whole point of retry_indefinitely.
_NON_RETRYABLE_GOODBYES: frozenset[GoodbyeReason] = frozenset(
    {
        GoodbyeReason.ANOTHER_SERVER,
        GoodbyeReason.CONCURRENT_ATTEMPT,
        GoodbyeReason.UNAUTHORIZED,
        GoodbyeReason.PAIRING_REQUIRED,
        GoodbyeReason.UNPAIRED,
    }
)

# A contested speaker does not always say goodbye. Observed on a live network:
# Home Assistant and a Plum-Audio unit both held retrying dials against the same
# player, which dropped the socket with close_code=None and no goodbye at all,
# so the reason-based rule above never fired and the two servers traded the
# speaker back and forth indefinitely.
#
# So also give up when a speaker churns: this many disconnects inside this
# window means something else wants it, whatever it does or does not tell us.
_FLAP_THRESHOLD = 3
_FLAP_WINDOW_S = 120.0

YIELD_CONTESTED = "contested"
"""Reason recorded when a speaker flaps rather than saying goodbye."""


def _enum_value(value: object) -> object:
    """Unwrap an enum for the diagnostics dump, passing None and plain values."""
    return getattr(value, "value", value)


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
        self._yielded: dict[str, str] = {}
        self._disconnects: dict[str, list[float]] = {}
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
    def yielded_urls(self) -> dict[str, str]:
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
        """Start holding a player.

        `ConnectionReason.PLAYBACK` is deliberate, and reverses this
        integration's original choice. `DISCOVERY` was chosen as politeness, on
        the belief that `PLAYBACK` would rudely take a speaker that `DISCOVERY`
        would leave alone. Measured against the whole fleet, the opposite is
        true: `DISCOVERY` is refused by every speaker on the network — ESPHome
        endpoints answer `ANOTHER_SERVER`, upgraded Plum players answer
        `CONCURRENT_ATTEMPT` — while the same speakers accept a `PLAYBACK` dial
        and hand over their full role set. Politeness bought no gentler
        outcome, it bought no outcome. See docs/SPEC-UPGRADE-PLAN.md §4a.

        What actually protects against taking a speaker somebody is using is
        unchanged: nothing is ever auto-dialled, adoption is an explicit and
        warned user action, and `ANOTHER_SERVER` still ends the dial rather
        than starting a tug-of-war.

        Stops any existing dialer first, because `connect_to_client` is a
        silent no-op while one exists.

        Calling this on a URL we previously yielded is the user asking us to
        try again, so the yield is cleared.
        """
        await self._async_stop_dialing(dial_url)
        self._adopted.add(dial_url)
        self._yielded.pop(dial_url, None)
        self._disconnects.pop(dial_url, None)
        _LOGGER.debug("Adopting Sendspin player at %s", dial_url)
        self.server.connect_to_client(
            dial_url,
            connection_reason=ConnectionReason.PLAYBACK,
            retry_initial_connection=True,
            retry_indefinitely=True,
        )

    async def async_release(self, dial_url: str) -> None:
        """Stop holding a player so another server can have it."""
        self._adopted.discard(dial_url)
        self._yielded.pop(dial_url, None)
        self._disconnects.pop(dial_url, None)
        _LOGGER.debug("Releasing Sendspin player at %s", dial_url)
        await self._async_stop_dialing(dial_url)

    async def async_reclaim(
        self,
        client_id: str,
        timeout_s: float = 30.0,
    ) -> bool:
        """Take a player back from whatever currently holds it.

        This is the escalation from a yielded adoption: the user has been told
        another server holds the speaker and has asked for it anyway.

        Deliberately re-dials through `async_adopt` rather than upstream's
        `reclaim_client_for_playback`. Both now assert the same playback claim,
        so the only thing upstream's version adds is resolving the URL from the
        library's own client registry — which `_on_server_event` evicts on
        yield, so reclaiming would fail for exactly the speakers that were given
        up. That is the only case the service exists for.

        Returns False when the client id resolves to no URL we could dial,
        which the service surfaces rather than failing silently.

        Does not wait for the player to land: dialling is asynchronous and the
        speaker may be slow to answer, so callers that need to know it arrived
        must watch the entity. `timeout_s` is retained for the service schema.
        """
        url = self._url_for_client_id(client_id)
        if url is None:
            _LOGGER.debug("Cannot reclaim %s: no known listener URL", client_id)
            return False
        await self.async_adopt(url)
        return True

    def client_diagnostics(self) -> list[dict[str, object]]:
        """What the server knows about each client, for the diagnostics dump.

        Every field here decided something during the 2026-08-15 fleet
        measurement and none of them were visible without attaching a debugger
        (docs/SPEC-UPGRADE-PLAN.md §0). The two that matter most:

        - **`active_roles` empty on a connected client** is the signature of an
          unpaired encrypted dial. It is admitted rather than refused, so the
          speaker looks adopted and available while being completely inert.
        - **`unpaired_access` and `trust_level`** together decide whether a
          speaker can ever be driven without a pairing record.

        Read defensively: a legacy cleartext connection has no PSK category and
        therefore no `connection_security` at all, and a client that never
        completed a hello has no `info`.
        """
        diagnostics: list[dict[str, object]] = []
        for client in self.server.clients:
            info = getattr(client, "info_or_none", None)
            security = getattr(client, "connection_security", None)
            connection = getattr(client, "connection", None)
            unpaired = getattr(info, "unpaired_access", None)
            goodbye = getattr(connection, "goodbye_reason", None)
            diagnostics.append(
                {
                    "client_id": client.client_id,
                    "name": getattr(client, "name", None),
                    "connected": getattr(client, "is_connected", None),
                    # Empty while connected means adopted but inert.
                    "active_roles": list(getattr(client, "active_role_ids", []) or []),
                    "negotiated_roles": list(
                        getattr(client, "negotiated_role_ids", []) or []
                    ),
                    "is_paired": getattr(client, "is_paired", None),
                    "is_encrypted": getattr(connection, "is_encrypted", None),
                    "trust_level": _enum_value(getattr(security, "trust_level", None)),
                    "psk_category": _enum_value(
                        getattr(security, "psk_category", None)
                    ),
                    "unpaired_access": getattr(unpaired, "enabled", None),
                    "last_goodbye": _enum_value(goodbye),
                    "software_version": getattr(
                        getattr(info, "device_info", None), "software_version", None
                    ),
                }
            )
        return diagnostics

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
        self._disconnects.clear()
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
        if (url := self._url_for_client_id(event.client_id)) is None:
            return
        if url not in self._adopted:
            return

        reason = self._yield_reason(url, event.goodbye_reason)
        if reason is None:
            return  # transient — let retry_indefinitely do its job

        self._yielded[url] = reason
        _LOGGER.info(
            "Giving up the dial to Sendspin player %s (%s) rather than "
            "fighting for it. Use the reclaim service to take it anyway",
            url,
            reason,
        )
        self.hass.async_create_task(
            self._async_yield(url, event.client_id), f"sendspin-yield-{url}"
        )

    async def _async_yield(self, dial_url: str, client_id: str) -> None:
        """Stop dialling a speaker we have given up, and release its client.

        Upstream retains a client after a goodbye and sets
        `_cleanup_on_mdns_removal`, waiting for its own mDNS browser to fire —
        but this integration never starts that browser (it is deliberately
        silent on mDNS), so nothing reads the flag and yielded clients
        accumulate for the life of the process.

        `remove_client` is a coroutine, so this has to be a task rather than a
        call from the synchronous event callback. Fired and forgotten it does
        nothing at all beyond logging "coroutine was never awaited", which is
        how it first reached hardware.

        Removal also drops upstream's own `client_id -> url` mapping, which is
        why `async_reclaim` resolves against `_url_by_client_id` instead.
        """
        await self._async_stop_dialing(dial_url)
        await self.server.remove_client(client_id)

    def _yield_reason(self, url: str, goodbye: GoodbyeReason | None) -> str | None:
        """Decide whether this disconnect means we should stop dialling.

        Two independent triggers, because a contested speaker does not reliably
        announce itself: an explicit non-retryable goodbye, or simply churning.
        """
        if goodbye in _NON_RETRYABLE_GOODBYES:
            return goodbye.value

        now = self.hass.loop.time()
        recent = [t for t in self._disconnects.get(url, ()) if now - t < _FLAP_WINDOW_S]
        recent.append(now)
        self._disconnects[url] = recent
        if len(recent) >= _FLAP_THRESHOLD:
            return YIELD_CONTESTED
        return None

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
