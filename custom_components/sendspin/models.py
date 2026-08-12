"""Shared data structures for the Sendspin integration.

Exists to break the import cycle that otherwise forms between the config entry
setup, the server host, the coordinator and the entity platforms.

Two URL kinds appear throughout and are never interchangeable:

- **frozen_url** — the endpoint's identity, as first seen. Entity unique ids are
  built from it and it is never recomputed.
- **dial_url** — where the speaker answers *now*. Moves with DHCP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import SendspinCoordinator
    from .mesh import MeshSource
    from .player_memo import PlayerMemo
    from .server_host import ServerHost


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    """One adopted Sendspin endpoint, as of the last server event."""

    frozen_url: str
    dial_url: str
    name: str

    client_id: str | None = None
    connected: bool = False

    yielded_reason: str | None = None
    """Set when another server holds this speaker, or it needs pairing. The
    adoption still stands; we have simply stopped fighting for it."""

    volume: int | None = None
    """0-100, or None when the player has never reported one.

    None must surface as *no volume attribute*, never as a fabricated level. A
    server can command a player's volume but cannot read it back unless the
    player echoes `client/state`, and many do not. Inventing a value here makes
    every level read 100% while the audio is demonstrably quieter, which looks
    exactly like a UI bug and is not one."""

    muted: bool | None = None

    source_label: str | None = None
    """The stream this endpoint is currently assigned to, or None when it is
    on nothing. Derived from the mesh view, which is the only place that
    knows — a Sendspin server tells outsiders nothing about its groups."""

    source_streaming: bool = False
    """Audio is actually flowing on the assigned stream. We can know this even
    when another server holds the speaker, because it comes from the mesh."""

    known_to_mesh: bool = False
    """The mesh can see this speaker, so it can be routed even though we are
    not holding it. Routing goes through the unit, not through us — which is
    the entire point: a speaker on Music Assistant can be moved to a Plum
    stream, and back again."""

    held_by_server: str | None = None
    """Whatever currently holds the speaker, for the user's benefit."""

    held_by_unit_host: str | None = None
    """The Plum unit currently holding this speaker, if any. Volume has to be
    commanded through whoever holds the connection."""

    routed_away: bool = False
    """The user deliberately handed this speaker to another unit. We must not
    dial it again on restart, or a reload would yank it back off the stream
    they put it on."""


@dataclass(frozen=True, slots=True)
class SendspinData:
    """Everything the entities render, rebuilt on each server event."""

    endpoints: dict[str, EndpointSnapshot] = field(default_factory=dict)
    """Keyed on frozen_url. Contains every **adopted** endpoint, whether or not
    it is currently connected — a speaker that drops off must go unavailable,
    not vanish."""

    sources: tuple[MeshSource, ...] = ()
    """Streams available right now, rebuilt on every mesh poll. Empty with no
    Plum mesh on the network, which is a legitimate state rather than an
    error — the integration still adopts and controls speakers."""


@dataclass(slots=True)
class SendspinRuntimeData:
    """Everything a Sendspin config entry owns while it is loaded."""

    host: ServerHost
    coordinator: SendspinCoordinator
    memo: PlayerMemo
