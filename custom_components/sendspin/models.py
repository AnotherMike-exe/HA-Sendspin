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

    source_kind: str | None = None
    """`airplay`, `bluetooth` or `spotify` for a stream on a Plum-Audio unit,
    when the unit's source id says which. None for no stream."""

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

    held_by_us: bool = False
    """Home Assistant is the holder.

    Distinct from `connected`, which is only true while our own socket is up:
    the mesh can report us as the holder from a unit's point of view. Naming
    ourselves as this speaker's *source* is wrong either way — we originate no
    audio, so holding a speaker on no stream is the none state."""

    held_by_plum_unit: bool = False
    """A Plum-Audio unit is the holder.

    A unit is never a source. Its streams are, and they are named one by one,
    so a speaker a unit holds on no stream is at none."""

    held_by_unit_host: str | None = None
    """The Plum unit currently holding this speaker, if any. Volume has to be
    commanded through whoever holds the connection."""

    media_title: str | None = None
    media_artist: str | None = None
    media_album: str | None = None
    media_album_artist: str | None = None
    media_position: float | None = None
    media_position_updated_at: float | None = None
    media_duration: float | None = None
    media_playing: bool = False
    media_state: str | None = None
    """`playing`, `paused` or `stopped`, as the link that reads the stream
    reports it. None when no link reads it, or the link reports no state.

    Kept apart from the title on purpose. A server keeps the last title after a
    stream stops, so a title says nothing about what the stream does now."""
    media_commands: tuple[str, ...] = ()
    media_repeat: str | None = None
    """`off`, `one` or `all`, when the stream reports a repeat mode."""
    media_shuffle: bool | None = None
    media_link: str | None = None
    """Which controller link supplies this endpoint's now-playing, and takes
    its transport commands. None when nothing is observing the stream."""

    routed_away: bool = False
    """The user deliberately handed this speaker to another unit. We must not
    dial it again on restart, or a reload would yank it back off the stream
    they put it on."""


@dataclass(frozen=True, slots=True)
class StreamSnapshot:
    """One mesh source — a stream — as of the last publish.

    Keyed on `MeshSource.key`, which is built from the unit's *configured*
    inputs. A unit publishes every input whether or not a sender is connected,
    so the set of streams is as durable as the set of speakers: a stream that
    starts or stops changes state, never the entity registry.
    """

    key: str
    unit_id: str
    unit_name: str
    unit_host: str
    source_id: str
    name: str
    """The source's own name, with any suffix that keeps its label unique."""
    label: str
    """What an endpoint's source dropdown calls this stream."""
    kind: str | None = None
    live: bool = False
    """A sender is feeding it (`active or streaming`), the same test that puts
    it in an endpoint's dropdown."""
    streaming: bool = False

    members: tuple[str, ...] = ()
    """Frozen URLs of the *adopted* endpoints on this stream, in name order."""
    speaker_names: tuple[str, ...] = ()
    """Every speaker the mesh shows on this stream, adopted or not."""

    source_volume: int | None = None
    """The level on the sending device, 0-100. None when unknown."""
    source_muted: bool | None = None
    supports_source_volume: bool = False

    media_title: str | None = None
    media_artist: str | None = None
    media_album: str | None = None
    media_album_artist: str | None = None
    media_position: float | None = None
    media_position_updated_at: float | None = None
    media_duration: float | None = None
    media_state: str | None = None
    media_commands: tuple[str, ...] = ()
    media_repeat: str | None = None
    media_shuffle: bool | None = None
    media_link: str | None = None


@dataclass(frozen=True, slots=True)
class SendspinData:
    """Everything the entities render, rebuilt on each server event."""

    endpoints: dict[str, EndpointSnapshot] = field(default_factory=dict)
    """Keyed on frozen_url. Contains every **adopted** endpoint, whether or not
    it is currently connected — a speaker that drops off must go unavailable,
    not vanish."""

    servers: tuple[tuple[str, str], ...] = ()
    """(server_id, name) of other Sendspin servers we can see. A speaker can be
    handed to one of these, which is how it gets back to Music Assistant."""

    sources: tuple[MeshSource, ...] = ()
    """*Every* source the mesh knows, live or not, rebuilt on every mesh poll.

    Not what the dropdown shows — see `live_sources`. This is the set a label is
    resolved against, so a stream that dies while selected can still be named.
    Empty with no Plum mesh on the network, which is a legitimate state rather
    than an error: the integration still adopts and controls speakers."""

    streams: dict[str, StreamSnapshot] = field(default_factory=dict)
    """Every routable mesh source, live or not, keyed on `MeshSource.key`.

    Internal sources (calibration tones) are never here."""

    live_sources: tuple[MeshSource, ...] = ()
    """The subset something is actually feeding, in dropdown order.

    A unit publishes every configured input whether or not a sender is
    connected, so this is the only honest answer to "what can this speaker be
    put on right now"."""


@dataclass(slots=True)
class SendspinRuntimeData:
    """Everything a Sendspin config entry owns while it is loaded."""

    host: ServerHost
    coordinator: SendspinCoordinator
    memo: PlayerMemo
