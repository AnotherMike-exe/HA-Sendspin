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
from dataclasses import dataclass

from aiosendspin.models.types import ConnectionReason


@dataclass(frozen=True, slots=True)
class DialCall:
    """One invocation of `connect_to_client`, no-op or not."""

    url: str
    connection_reason: ConnectionReason
    retry_initial_connection: bool
    retry_indefinitely: bool


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
        self.dial_calls: list[DialCall] = []
        self.dials_started: list[str] = []
        self.disconnect_calls: list[str] = []
        self.reclaim_calls: list[tuple[str, float]] = []
        self.client_ids_by_url: dict[str, str] = {}
        self.closed = False

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
