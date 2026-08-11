"""A stand-in for `aiosendspin.server.SendspinServer`.

This fake deliberately reproduces upstream's **defects**, not an idealised
version of the library. That is the entire point of it: the integration exists
largely to work around those defects, so a fake that behaves correctly would
let every workaround rot silently and the failure would only ever show up on
real hardware, where Plum-Audio originally found them.

Reproduced here, all verified against aiosendspin 9.1.0:

- `connect_to_client` returns early — a **silent no-op** — when a dial task for
  that URL already exists.
- `disconnect_from_client` pops the dial task and calls `.cancel()` without
  awaiting it, so the task may still be alive when the call returns.
- A dial task can decline to die on the first cancellation (`stubborn_cancels`),
  which is what produced several live dialers fighting over one socket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from aiosendspin.models.types import ConnectionReason


@dataclass(frozen=True, slots=True)
class DialCall:
    """One invocation of `connect_to_client`, no-op or not."""

    url: str
    connection_reason: ConnectionReason
    retry_initial_connection: bool
    retry_indefinitely: bool


class FakePlayerRole:
    """A player role, which is where volume and mute actually live."""

    def __init__(self, volume: int | None = None, muted: bool | None = None) -> None:
        """Volume defaults to None: unknown, not zero, and not 100."""
        self._volume = volume
        self._muted = muted
        self.commanded_volumes: list[int] = []
        self.commanded_mutes: list[bool] = []

    def get_player_volume(self) -> int | None:
        """None when the player has never echoed client/state."""
        return self._volume

    def get_player_muted(self) -> bool | None:
        """None when the player has never echoed client/state."""
        return self._muted

    def set_player_volume(self, volume: int) -> None:
        """Command volume. A server can set but not necessarily read it back."""
        self.commanded_volumes.append(volume)

    def set_player_mute(self, muted: bool) -> None:
        """Command mute."""
        self.commanded_mutes.append(muted)


class FakeSendspinClient:
    """A connected speaker as the server sees it."""

    def __init__(
        self,
        client_id: str,
        name: str,
        *,
        is_connected: bool = True,
        role: FakePlayerRole | None = None,
    ) -> None:
        """Create the fake client."""
        self.client_id = client_id
        self.name = name
        self.is_connected = is_connected
        self.player = role if role is not None else FakePlayerRole()

    def roles_by_family(self, family: str) -> list[FakePlayerRole]:
        """Only the player family is modelled."""
        return [self.player] if family == "player" else []


class FakeSendspinServer:
    """Duck-typed `SendspinServer` covering the surface `ServerHost` uses."""

    def __init__(self, *, stubborn_cancels: int = 0) -> None:
        """Create the fake.

        `stubborn_cancels` makes each dial task swallow that many cancellations
        before it actually exits, modelling the message loop that catches
        CancelledError and reconnects.
        """
        self._connection_tasks: dict[str, asyncio.Task] = {}
        self._stubborn_cancels = stubborn_cancels
        self._listeners: list[Callable[[object, object], None]] = []
        self.dial_calls: list[DialCall] = []
        self.dials_started: list[str] = []
        self.disconnect_calls: list[str] = []
        self.reclaim_calls: list[tuple[str, float]] = []
        self.client_ids_by_url: dict[str, str] = {}
        self.clients_by_id: dict[str, FakeSendspinClient] = {}
        self.closed = False

    def attach(
        self,
        url: str,
        client_id: str,
        name: str = "Speaker",
        *,
        volume: int | None = None,
        muted: bool | None = None,
        is_connected: bool = True,
    ) -> FakeSendspinClient:
        """Register a client answering on a URL, as if it had connected."""
        client = FakeSendspinClient(
            client_id,
            name,
            is_connected=is_connected,
            role=FakePlayerRole(volume=volume, muted=muted),
        )
        self.client_ids_by_url[url] = client_id
        self.clients_by_id[client_id] = client
        return client

    def get_client(self, client_id: str) -> FakeSendspinClient | None:
        """Look a client up by id."""
        return self.clients_by_id.get(client_id)

    def add_event_listener(
        self, callback: Callable[[object, object], None]
    ) -> Callable[[], None]:
        """Register an event listener, returning an unsubscribe callable."""
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsubscribe

    def emit(self, event: object) -> None:
        """Fire an event at every listener, as the real server does."""
        for callback in list(self._listeners):
            callback(self, event)

    @property
    def listener_count(self) -> int:
        """How many listeners are attached — used to prove teardown unhooks."""
        return len(self._listeners)

    # --- the surface ServerHost depends on --------------------------------

    def connect_to_client(
        self,
        url: str,
        *,
        connection_reason: ConnectionReason = ConnectionReason.DISCOVERY,
        retry_initial_connection: bool = False,
        retry_indefinitely: bool = False,
        pairing_attempt: object | None = None,
    ) -> None:
        """Start a dial, unless one is already in flight for this URL."""
        self.dial_calls.append(
            DialCall(
                url,
                connection_reason,
                retry_initial_connection,
                retry_indefinitely,
            )
        )
        if url in self._connection_tasks:
            return  # upstream's silent no-op
        self.dials_started.append(url)
        self._connection_tasks[url] = asyncio.get_running_loop().create_task(
            self._dial_forever()
        )

    def disconnect_from_client(self, url: str) -> None:
        """Pop the dial task and cancel it without awaiting it."""
        self.disconnect_calls.append(url)
        if (task := self._connection_tasks.pop(url, None)) is not None:
            task.cancel()

    def reclaim_client_for_playback(
        self, client_id: str, timeout_s: float = 30.0
    ) -> bool:
        """Record a playback claim."""
        self.reclaim_calls.append((client_id, timeout_s))
        return True

    def get_client_id_for_url(self, url: str) -> str | None:
        """Resolve a dial URL to the client answering on it."""
        return self.client_ids_by_url.get(url)

    def get_client_url(self, client_id: str) -> str | None:
        """Reverse of `get_client_id_for_url`."""
        for url, cid in self.client_ids_by_url.items():
            if cid == client_id:
                return url
        return None

    async def close(self) -> None:
        """Shut the server down."""
        self.closed = True

    async def start_server(self, *args: object, **kwargs: object) -> None:
        """Fail loudly. Home Assistant must never call this.

        `start_server()` constructs an AsyncZeroconf, binds UDP 5353 and
        advertises this server on the LAN. This integration browses; it does
        not advertise, and it does not accept inbound connections — Sendspin
        servers dial players, not the other way round.
        """
        raise AssertionError(
            "start_server() must never be called: it advertises over mDNS"
        )

    # --- helpers for assertions -------------------------------------------

    @property
    def live_dial_urls(self) -> set[str]:
        """URLs with a dial task that has not finished."""
        return {url for url, t in self._connection_tasks.items() if not t.done()}

    async def _dial_forever(self) -> None:
        """Model a dial task, optionally ignoring the first N cancellations."""
        remaining = self._stubborn_cancels
        while True:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                if remaining <= 0:
                    raise
                remaining -= 1
