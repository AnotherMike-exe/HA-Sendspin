"""A hand-written Sendspin controller client for pre-8.0 servers.

Why this exists rather than using the library
---------------------------------------------
`aiosendspin` 9.1.0's client always initiates the 8.0 Noise handshake and has no
way to fall back — `allow_unencrypted` exists only on the *server*. Every
Sendspin server on a real network today predates 8.0, so the library's client
cannot talk to any of them: Music Assistant, Plum-Audio units, all of them close
the socket on `client/init`.

The older protocol is plain JSON over a websocket, so this speaks it directly.
That also permanently decouples the controller role from the library version,
which is the trap that made this necessary in the first place.

Verified against live servers before being written: a hello of exactly this
shape is admitted by Music Assistant and by Plum-Audio units, which then send
`server/state` carrying real track metadata.

What a controller can and cannot see
------------------------------------
A controller observes **only the group it currently occupies**. There is no
message listing groups, listing players, or moving another player. That is why
this integration hosts a server for routing and uses this only for *reading*
what is playing, plus transport commands for the group it is in.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession, WSMsgType

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# Binary frames are [type:1][timestamp_us:8 big-endian][payload].
_BINARY_HEADER = 9
_ARTWORK_CHANNEL_0 = 8

# Reconnect backoff. A controller link is pure observation, so being slow to
# recover costs only staleness — never audio.
_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 60.0

# The spec expects a controller to keep the server's clock. Servers have been
# observed tolerating its absence, but a strict one may treat a controller that
# never sends `client/time` as not operational.
_TIME_SYNC_INTERVAL_S = 30.0

# Some servers place a controller into the group that is playing; Music
# Assistant does not, leaving it in a solo group that reports nothing. `switch`
# cycles a controller through the server's *playing* groups, which is the only
# way in. Bounded, because ordering is not stable and an empty server would
# otherwise be polled forever.
_SWITCH_ATTEMPTS = 6
_SWITCH_INTERVAL_S = 4.0
# Having cycled without finding anything, wait before going round again. A
# server that starts playing later must still be found — hunting only once per
# connection left a link parked in its solo group reporting idle forever.
_SWITCH_REST_S = 30.0


@dataclass(frozen=True, slots=True)
class Progress:
    """Where a track is, as of `updated_at`."""

    position_s: float
    duration_s: float | None
    playing: bool
    updated_at: float
    """`time.time()` when this was received. Home Assistant extrapolates from
    here, which avoids needing the server's clock just to draw a progress bar."""


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    """What one controller link can see of one group."""

    server_name: str | None = None
    server_id: str | None = None
    group_id: str | None = None
    playback_state: str | None = None

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_artist: str | None = None
    artwork_url: str | None = None
    progress: Progress | None = None

    supported_commands: tuple[str, ...] = ()
    volume: int | None = None
    muted: bool | None = None
    repeat: str | None = None
    shuffle: bool | None = None

    artwork: bytes | None = None
    """Raw image bytes from the artwork role. Plum-Audio sends no
    `artwork_url`, so binary frames are the only artwork that exists."""

    connected: bool = False


class LegacyControllerClient:
    """One observing connection to one pre-8.0 Sendspin server."""

    def __init__(
        self,
        session: ClientSession,
        url: str,
        *,
        client_name: str,
        client_id: str,
        on_update: Callable[[ControllerSnapshot], None],
        artwork_size: int = 512,
        hunt_for_playing: bool = True,
    ) -> None:
        """Prepare a link. Call `async_start` to open it.

        `hunt_for_playing` cycles the controller through the server's playing
        groups. Turn it off for a link whose client id already targets a
        specific source — hunting would move it off the group it was placed in
        on purpose.
        """
        self._session = session
        self._url = url
        self._client_name = client_name
        self._client_id = client_id
        self._on_update = on_update
        self._artwork_size = artwork_size
        self._hunt = hunt_for_playing

        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._closing = False
        self._snapshot = ControllerSnapshot()
        self._switches = 0
        # Server clock minus ours, in microseconds.
        self._clock_offset_us = 0

    @property
    def snapshot(self) -> ControllerSnapshot:
        """The latest state observed on this link."""
        return self._snapshot

    @property
    def url(self) -> str:
        """The server this link observes."""
        return self._url

    def async_start(self) -> None:
        """Open the link and keep it open."""
        if self._task is None:
            self._closing = False
            self._task = asyncio.create_task(self._run())

    async def async_stop(self) -> None:
        """Close the link for good."""
        self._closing = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=5)
            self._task = None
        self._publish(replace(ControllerSnapshot(), connected=False))

    async def async_send_command(
        self,
        command: str,
        *,
        volume: int | None = None,
        mute: bool | None = None,
    ) -> None:
        """Send a transport command for the group this link is in.

        Silently does nothing when the server has not advertised the command:
        it would be dropped server-side anyway, and pretending otherwise makes
        a button look broken rather than absent.
        """
        if self._ws is None or self._ws.closed:
            raise ConnectionError(f"Not connected to {self._url}")
        payload: dict[str, Any] = {"command": command}
        if volume is not None:
            payload["volume"] = volume
        if mute is not None:
            payload["mute"] = mute
        await self._ws.send_json(
            {"type": "client/command", "payload": {"controller": payload}}
        )

    # --- connection ---------------------------------------------------------

    async def _run(self) -> None:
        """Keep the link up, backing off between attempts."""
        backoff = _BACKOFF_START_S
        while not self._closing:
            try:
                await self._connect_once()
                backoff = _BACKOFF_START_S
            except asyncio.CancelledError:
                raise
            except (ClientError, ConnectionError, TimeoutError, OSError) as err:
                _LOGGER.debug("Sendspin controller link to %s: %s", self._url, err)
            except Exception:
                _LOGGER.exception("Sendspin controller link to %s failed", self._url)

            if self._closing:
                return
            self._publish(replace(self._snapshot, connected=False))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _connect_once(self) -> None:
        """Handshake, then read until the socket closes."""
        async with self._session.ws_connect(self._url, heartbeat=30) as ws:
            self._ws = ws
            await ws.send_json(self._hello())

            # Nothing may go on the wire between client/hello and server/hello.
            first = await asyncio.wait_for(ws.receive_json(), timeout=15)
            if first.get("type") != "server/hello":
                raise ConnectionError(f"Expected server/hello, got {first.get('type')}")
            self._on_server_hello(first.get("payload") or {})

            # Only now may anything else go on the wire. The spec expects a
            # client to declare itself; a controller is always synchronized,
            # having no audio to be out of sync with.
            await ws.send_json(
                {"type": "client/state", "payload": {"state": "synchronized"}}
            )

            self._switches = 0
            sync = asyncio.create_task(self._time_sync_loop(ws))
            hunt = asyncio.create_task(self._find_playing_group(ws))
            try:
                async for msg in ws:
                    if msg.type is WSMsgType.TEXT:
                        self._on_text(msg.json())
                    elif msg.type is WSMsgType.BINARY:
                        self._on_binary(msg.data)
                    elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSED):
                        break
            finally:
                sync.cancel()
                hunt.cancel()
                self._ws = None

    def _hello(self) -> dict[str, Any]:
        """The handshake, in the shape both server families were seen to accept.

        Artwork support is declared because Plum-Audio publishes no
        `artwork_url` — pushed binary frames are the only cover art available,
        and a server sends them only to a client that asked.
        """
        return {
            "type": "client/hello",
            "payload": {
                "name": self._client_name,
                "supported_roles": ["controller@v1", "metadata@v1", "artwork@v1"],
                "client_id": self._client_id,
                "version": PROTOCOL_VERSION,
                "artwork@v1_support": {
                    "channels": [
                        {
                            "source": "album",
                            "format": "jpeg",
                            "media_width": self._artwork_size,
                            "media_height": self._artwork_size,
                        }
                    ]
                },
            },
        }

    # --- inbound ------------------------------------------------------------

    def _on_server_hello(self, payload: dict[str, Any]) -> None:
        self._publish(
            replace(
                self._snapshot,
                server_name=payload.get("name"),
                server_id=payload.get("server_id"),
                connected=True,
            )
        )

    def _on_text(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        payload = message.get("payload") or {}
        if kind == "server/state":
            self._on_server_state(payload)
        elif kind == "group/update":
            self._publish(
                replace(
                    self._snapshot,
                    group_id=payload.get("group_id", self._snapshot.group_id),
                    playback_state=payload.get(
                        "playback_state", self._snapshot.playback_state
                    ),
                )
            )
        elif kind == "server/time":
            self._on_server_time(payload)
        elif kind in ("stream/end", "stream/clear"):
            # Deliberately does NOT clear artwork. The track has not changed,
            # and clearing here blanks the cover on every pause, idle and roam
            # until the next track change.
            pass

    def _on_server_state(self, payload: dict[str, Any]) -> None:
        snapshot = self._snapshot
        if (controller := payload.get("controller")) is not None:
            snapshot = replace(
                snapshot,
                supported_commands=tuple(controller.get("supported_commands") or ()),
                volume=controller.get("volume", snapshot.volume),
                muted=controller.get("muted", snapshot.muted),
                repeat=controller.get("repeat", snapshot.repeat),
                shuffle=controller.get("shuffle", snapshot.shuffle),
            )
        if (metadata := payload.get("metadata")) is not None:
            snapshot = self._apply_metadata(snapshot, metadata)
        self._publish(snapshot)

    def _apply_metadata(
        self, snapshot: ControllerSnapshot, metadata: dict[str, Any]
    ) -> ControllerSnapshot:
        """Apply a metadata diff.

        Metadata is a **diff**: a key that is absent means unchanged, while a
        key present as null means cleared. Collapsing the two would leave a
        cleared album showing the previous track's forever.
        """

        def pick(key: str, current: Any) -> Any:
            # An absent key means unchanged; a key present as null means
            # cleared. `.get` with a default gives exactly that, since a
            # present-but-null key returns the null rather than the default.
            return metadata.get(key, current)

        progress = snapshot.progress
        if "progress" in metadata:
            raw = metadata["progress"]
            if raw is None:
                progress = None
            else:
                duration_ms = raw.get("track_duration") or 0
                progress = Progress(
                    position_s=(raw.get("track_progress") or 0) / 1000,
                    # 0 means live or unknown, not "zero length".
                    duration_s=(duration_ms / 1000) if duration_ms else None,
                    playing=bool(raw.get("playback_speed")),
                    updated_at=time.time(),
                )

        return replace(
            snapshot,
            title=pick("title", snapshot.title),
            artist=pick("artist", snapshot.artist),
            album=pick("album", snapshot.album),
            album_artist=pick("album_artist", snapshot.album_artist),
            artwork_url=pick("artwork_url", snapshot.artwork_url),
            repeat=pick("repeat", snapshot.repeat),
            shuffle=pick("shuffle", snapshot.shuffle),
            progress=progress,
        )

    def _on_binary(self, data: bytes) -> None:
        """Handle a binary frame — artwork is the only kind we asked for."""
        if len(data) < _BINARY_HEADER:
            return
        if data[0] != _ARTWORK_CHANNEL_0:
            return
        image = data[_BINARY_HEADER:]
        # An empty payload is the only signal that means "clear".
        self._publish(replace(self._snapshot, artwork=image or None))

    # --- clock --------------------------------------------------------------

    async def _find_playing_group(self, ws: Any) -> None:
        """Cycle into a playing group, for servers that do not put us in one.

        Plum-Audio honours a `ctrl:<source_id>` client id and places the
        controller directly. Music Assistant does not: it leaves a controller
        in its own solo group, which reports nothing at all. `switch` moves a
        controller through the server's playing groups, and a controller has no
        player role, so moving between them affects no audio.

        Stops as soon as something is playing, and is bounded — the cycle
        ordering is not stable, and a server with nothing playing would
        otherwise be asked forever.
        """
        while self._switches < _SWITCH_ATTEMPTS:
            await asyncio.sleep(_SWITCH_INTERVAL_S)
            snapshot = self._snapshot
            if snapshot.title is not None or snapshot.playback_state == "playing":
                return
            if "switch" not in snapshot.supported_commands:
                return
            self._switches += 1
            try:
                await ws.send_json(
                    {
                        "type": "client/command",
                        "payload": {"controller": {"command": "switch"}},
                    }
                )
            except ClientError, ConnectionError:
                return

    async def _time_sync_loop(self, ws: Any) -> None:
        """Keep the server's clock, as the spec expects of a controller."""
        while True:
            try:
                await ws.send_json(
                    {
                        "type": "client/time",
                        "payload": {"client_transmitted": _now_us()},
                    }
                )
            except ClientError, ConnectionError:
                return
            await asyncio.sleep(_TIME_SYNC_INTERVAL_S)

    def _on_server_time(self, payload: dict[str, Any]) -> None:
        """Estimate the server's clock with a simple round-trip midpoint."""
        try:
            sent = int(payload["client_transmitted"])
            received = int(payload["server_received"])
            replied = int(payload["server_transmitted"])
        except KeyError, TypeError, ValueError:
            return
        now = _now_us()
        self._clock_offset_us = ((received - sent) + (replied - now)) // 2

    def _publish(self, snapshot: ControllerSnapshot) -> None:
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self._on_update(snapshot)


def _now_us() -> int:
    """Monotonic-ish microseconds for time sync."""
    return time.monotonic_ns() // 1000
