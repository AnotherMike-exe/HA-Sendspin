"""The optional Plum-Audio mesh federation tier.

Where streams come from. A Sendspin server exposes no way for an outsider to
enumerate its groups or streams — that is the whole reason this integration
hosts its own server. Plum-Audio units additionally expose an HTTP mesh API
that *does* describe every unit, source and player, and that is the only place
a list of available streams can come from.

**Optional by construction.** With no Plum unit on the network the integration
still adopts speakers and controls their volume; there are simply no streams to
list. Nothing here is required for the integration to work.

Two things to know before changing this:

- **`source_id` is only unique within a unit.** Every unit happily calls its
  AirPlay endpoint `airplay-1`, so a source is always the pair
  `(unit_id, source_id)`, and a routing call must be POSTed to the unit that
  *owns* the source. Posting to the wrong one silently routes locally.
- **Assigning a speaker uses `adopt`, not `route`.** `route` resolves a player
  from the target unit's own view, and a speaker Home Assistant is holding
  appears in nobody's view — idle players are in no membership list. `adopt`
  takes the speaker's **URL**, which is the one identifier both Sendspin
  identity views share, and is what this project keys everything on.

There is no authentication on this API, and requests without an `Origin` header
bypass its CORS policy entirely, so anything on the LAN can reroute audio. That
is a property of the API, not a decision made here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from aiohttp import ClientError, ClientSession

_LOGGER = logging.getLogger(__name__)

DEFAULT_MESH_PORT = 5001
REQUEST_TIMEOUT_S = 4.0

# Plum aggregates peer state on a 2s loop, so polling faster than that buys
# nothing but load. A route is reflected back within roughly two cycles.
MESH_POLL_INTERVAL_S = 5.0


@dataclass(frozen=True, slots=True)
class MeshSource:
    """One source on one unit — a stream a speaker can be assigned to."""

    unit_id: str
    unit_name: str
    unit_host: str
    source_id: str
    name: str
    group_id: str | None = None
    active: bool = False
    """A sender is really feeding this source. Note Plum holds this true for up
    to SOURCE_IDLE_TIMEOUT_S (default 300s) after a sender walks away."""
    streaming: bool = False
    player_ids: tuple[str, ...] = ()
    source_volume: int | None = None
    source_muted: bool | None = None
    supports_source_volume: bool = False

    @property
    def key(self) -> str:
        """Globally unique id. `source_id` alone is only unique per unit."""
        return f"{self.unit_id}:{self.source_id}"

    @property
    def label(self) -> str:
        """What the user picks in the source dropdown."""
        return f"{self.unit_name} / {self.name}"


@dataclass(frozen=True, slots=True)
class MeshPlayer:
    """A speaker as a Plum-Audio unit currently sees it.

    Only speakers a unit is *holding* appear. An idle speaker is in no unit's
    list at all, which is why absence here says nothing about a speaker's
    health.
    """

    player_id: str
    unit_host: str
    name: str
    connected: bool = False
    volume: int | None = None
    muted: bool | None = None


@dataclass(frozen=True, slots=True)
class MeshView:
    """Everything the mesh currently reports."""

    sources: tuple[MeshSource, ...] = ()
    players: tuple[MeshPlayer, ...] = ()
    reachable: bool = False
    """False when no unit answered. Distinguishing this from "answered, and
    there is nothing" matters: a failed fetch must never be read as every
    stream having ended."""

    def source_by_label(self, label: str) -> MeshSource | None:
        """Look a source up by the label shown to the user."""
        return next((s for s in self.sources if s.label == label), None)

    def player_by_id(self, client_id: str | None) -> MeshPlayer | None:
        """A speaker as its holding unit reports it, if a unit holds it."""
        if client_id is None:
            return None
        return next((p for p in self.players if p.player_id == client_id), None)

    def source_for_player(self, client_id: str | None) -> MeshSource | None:
        """Which source, if any, a speaker is currently assigned to."""
        if client_id is None:
            return None
        return next((s for s in self.sources if client_id in s.player_ids), None)

    @property
    def sorted_sources(self) -> list[MeshSource]:
        """Active sources first, then alphabetically.

        All sources are listed, not just active ones. Assigning a speaker to an
        idle source then starting the music is a normal workflow, and on a real
        mesh every source reads inactive most of the time — filtering would
        leave the dropdown empty exactly when someone wants to use it.
        """
        return sorted(self.sources, key=lambda s: (not s.active, s.label.lower()))


def _as_bool(value: Any, default: bool = False) -> bool:
    """Read a flag, tolerating absence."""
    return default if value is None else bool(value)


def parse_view(payload: dict[str, Any]) -> MeshView:
    """Turn a `/api/mesh/view` body into sources.

    Deliberately forgiving: the API carries no version marker, and forward
    compatibility upstream is handled purely by defaults on missing keys. Do
    the same rather than assuming any field exists.
    """
    sources: list[MeshSource] = []
    players: list[MeshPlayer] = []
    for unit in payload.get("units") or []:
        unit_id = unit.get("unit_id")
        if not unit_id:
            continue
        unit_name = unit.get("name") or unit_id
        unit_host = unit.get("host") or ""
        for source in unit.get("sources") or []:
            source_id = source.get("source_id")
            if not source_id:
                continue
            sources.append(
                MeshSource(
                    unit_id=unit_id,
                    unit_name=unit_name,
                    unit_host=unit_host,
                    source_id=source_id,
                    # Upstream already falls back to the id, but never trust it.
                    name=source.get("name") or source_id,
                    group_id=source.get("group_id"),
                    active=_as_bool(source.get("active")),
                    streaming=_as_bool(source.get("streaming")),
                    player_ids=tuple(source.get("player_ids") or ()),
                    source_volume=source.get("source_volume"),
                    source_muted=source.get("source_muted"),
                    supports_source_volume=_as_bool(
                        source.get("supports_source_volume")
                    ),
                )
            )
        for player in unit.get("players") or []:
            player_id = player.get("player_id")
            if not player_id:
                continue
            players.append(
                MeshPlayer(
                    player_id=player_id,
                    unit_host=unit_host,
                    name=player.get("name") or player_id,
                    connected=_as_bool(player.get("connected"), True),
                    # What the speaker last *reported*, not what was commanded.
                    volume=player.get("volume"),
                    muted=player.get("muted"),
                )
            )
    return MeshView(sources=tuple(sources), players=tuple(players), reachable=True)


class MeshClient:
    """Talks to Plum-Audio units over HTTP.

    Every write is one small method, so the eventual upstream swap to the
    proposed `attach`/`detach` vocabulary — which retires `route`, `unroute`,
    `adopt` and `release` — is a change to this file alone.
    """

    def __init__(self, session: ClientSession, port: int = DEFAULT_MESH_PORT) -> None:
        """Initialise the client over Home Assistant's shared session."""
        self._session = session
        self._port = port

    def _url(self, host: str, path: str) -> str:
        return f"http://{host}:{self._port}/api/mesh/{path}"

    async def async_fetch_view(self, hosts: list[str]) -> MeshView:
        """Fetch the whole mesh from whichever host answers first.

        Any unit's view describes every unit, so one reachable host is enough.
        An unreachable mesh returns `reachable=False` rather than an empty
        view: a failed fetch must never be mistaken for every stream ending.
        """
        for host in hosts:
            try:
                async with self._session.get(
                    self._url(host, "view"), timeout=REQUEST_TIMEOUT_S
                ) as response:
                    if response.status != 200:
                        continue
                    payload = await response.json()
            except (ClientError, TimeoutError, ValueError) as err:
                _LOGGER.debug("Mesh view from %s failed: %s", host, err)
                continue
            if isinstance(payload, dict) and "units" in payload:
                return parse_view(payload)
        return MeshView(reachable=False)

    async def async_assign(self, source: MeshSource, speaker_url: str) -> None:
        """Put a speaker onto a source.

        Uses `adopt`, which is keyed on the speaker's URL. `route` is keyed on
        a player id resolved against the target unit's own view, and a speaker
        Home Assistant holds is in no unit's view at all.

        Posted to the unit that owns the source, because `source_id` is only
        unique within a unit.
        """
        await self._post(
            source.unit_host,
            "adopt",
            {"url": speaker_url, "source_id": source.source_id},
        )

    async def async_unassign(
        self, source: MeshSource, speaker_url: str, client_id: str | None
    ) -> None:
        """Take a speaker off a source and hand it back."""
        body: dict[str, Any] = {"source_id": source.source_id, "url": speaker_url}
        if client_id is not None:
            body["player_id"] = client_id
        await self._post(source.unit_host, "release", body)

    async def async_set_volume(
        self,
        unit_host: str,
        player_id: str,
        *,
        volume: int | None = None,
        muted: bool | None = None,
    ) -> None:
        """Command a speaker's volume through the unit currently holding it.

        Home Assistant can only command a speaker over its own connection, and
        handing a speaker to a unit means giving that connection up. Without
        this, routing a speaker would silently cost you the volume control.
        """
        body: dict[str, Any] = {"player_id": player_id}
        if volume is not None:
            body["volume"] = volume
        if muted is not None:
            body["muted"] = muted
        await self._post(unit_host, "volume", body)

    async def _post(self, host: str, path: str, body: dict[str, Any]) -> None:
        """POST to a unit, raising something a service can surface."""
        try:
            async with self._session.post(
                self._url(host, path), json=body, timeout=REQUEST_TIMEOUT_S
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200 or not (payload or {}).get("ok", True):
                    raise MeshError(
                        (payload or {}).get("error")
                        or f"{path} failed with HTTP {response.status}"
                    )
        except (ClientError, TimeoutError) as err:
            raise MeshError(f"Could not reach the Sendspin unit at {host}") from err


class MeshError(Exception):
    """A mesh call failed in a way worth telling the user about."""


@dataclass
class MeshState:
    """Mutable view plus the hosts worth asking."""

    view: MeshView = field(default_factory=MeshView)
    hosts: list[str] = field(default_factory=list)
