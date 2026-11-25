"""Media player platform for Sendspin.

One durable entity per **physical endpoint** — a speaker — not one per stream.
Streams come and go constantly; anchoring entities to them would churn the
entity registry and discard the user's renames, areas and icons on every
reconnect. Instead the stream an endpoint is assigned to becomes state *on* the
endpoint, and the streams that are live become its `source_list`.

The source dropdown is the one routing control. To select a source puts the
speaker on that stream, and that is grouping: no `GROUPING` feature, no
`group_members`, no join. `None` is a true none.

`supported_features` is read from the endpoint and its current stream on each
state write, so the entity offers only the controls that work now. The volume
is the endpoint level, never the group level or the source level.

docs/DASHBOARD.md has the feature table for each kind of source.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
from weakref import WeakKeyDictionary

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SendspinConfigEntry
from .const import CONF_LISTENER_URL, DOMAIN, SOURCE_NONE, SUBENTRY_TYPE_PLAYER
from .coordinator import SendspinCoordinator
from .entity import SendspinEndpointEntity
from .mesh import MeshError
from .models import StreamSnapshot
from .server_host import player_role

# One press of volume up or down, in percent of the endpoint level.
VOLUME_STEP_PERCENT = 5

# Sendspin command name -> the Home Assistant feature it satisfies.
_TRANSPORT_FEATURES = {
    "play": MediaPlayerEntityFeature.PLAY,
    "pause": MediaPlayerEntityFeature.PAUSE,
    "stop": MediaPlayerEntityFeature.STOP,
    "next": MediaPlayerEntityFeature.NEXT_TRACK,
    "previous": MediaPlayerEntityFeature.PREVIOUS_TRACK,
    "repeat_off": MediaPlayerEntityFeature.REPEAT_SET,
    "repeat_one": MediaPlayerEntityFeature.REPEAT_SET,
    "repeat_all": MediaPlayerEntityFeature.REPEAT_SET,
    "shuffle": MediaPlayerEntityFeature.SHUFFLE_SET,
    "unshuffle": MediaPlayerEntityFeature.SHUFFLE_SET,
}

# Source kind -> the name a card shows for it.
_KIND_NAMES = {"airplay": "AirPlay", "bluetooth": "Bluetooth", "spotify": "Spotify"}

# Source kind -> the icon of its stream entity.
_KIND_ICONS = {
    "airplay": "mdi:cast-audio-variant",
    "bluetooth": "mdi:bluetooth-audio",
    "spotify": "mdi:spotify",
}

# The `source_kind` of a speaker that another Sendspin server plays to.
KIND_SERVER = "server"

# Leading bytes -> image type. A server can send any of these, whatever format
# the hello asked for.
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)
# The hello asks for JPEG, so that is the guess for bytes nothing recognises.
_DEFAULT_IMAGE_TYPE = "image/jpeg"


def image_content_type(image: bytes) -> str:
    """The MIME type of a cover, from the bytes of the cover."""
    for signature, content_type in _IMAGE_SIGNATURES:
        if image.startswith(signature):
            return content_type
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return "image/webp"
    return _DEFAULT_IMAGE_TYPE


# Link -> its cover as (bytes, hash, content type). Shared by every entity
# that shows a link's cover, so one cover is hashed once, not once per entity.
_COVERS: WeakKeyDictionary[object, tuple[bytes, str, str]] = WeakKeyDictionary()


def cover_for(link: object | None) -> tuple[bytes, str, str] | None:
    """A link's current cover with its hash and type, worked out once a cover.

    Home Assistant reads the hash on each state write, and a cover is up to
    half a megabyte. The link keeps one bytes object until the server sends
    another, so identity says it has not changed. The comparison is for a link
    that was replaced and holds an equal cover.
    """
    artwork = link.snapshot.artwork if link is not None else None
    if not artwork:
        return None
    cached = _COVERS.get(link)
    if cached is None or (cached[0] is not artwork and cached[0] != artwork):
        cached = _COVERS[link] = (
            artwork,
            hashlib.sha256(artwork).hexdigest()[:16],
            image_content_type(artwork),
        )
    return cached


_REPEAT_COMMANDS = {
    RepeatMode.OFF: "repeat_off",
    RepeatMode.ONE: "repeat_one",
    RepeatMode.ALL: "repeat_all",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SendspinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sendspin endpoints, including ones adopted later.

    The closure keeps its own `known` set and stays subscribed for the life of
    the entry, so a speaker adopted an hour from now appears without a reload.
    This is Home Assistant's `dynamic-devices` pattern.

    Entities are never removed here. An endpoint leaves only when the user
    deletes its subentry, which Home Assistant handles by removing the device.
    """
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()
    known_streams: set[str] = set()

    @callback
    def _add_new_streams() -> None:
        """One entity per configured mesh source, added as sources appear.

        Never removed: a source that leaves the view goes unavailable, so its
        renames and dashboard cards survive a unit restarting.
        """
        new = [key for key in coordinator.data.streams if key not in known_streams]
        if not new:
            return
        known_streams.update(new)
        async_add_entities([SendspinStreamMediaPlayer(coordinator, key) for key in new])

    @callback
    def _add_new_endpoints() -> None:
        for subentry_id, subentry in entry.subentries.items():
            if subentry.subentry_type != SUBENTRY_TYPE_PLAYER:
                continue
            frozen_url = subentry.data[CONF_LISTENER_URL]
            if frozen_url in known:
                continue
            known.add(frozen_url)
            async_add_entities(
                [SendspinEndpointMediaPlayer(coordinator, frozen_url)],
                config_subentry_id=subentry_id,
            )

    _add_new_endpoints()
    _add_new_streams()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_endpoints))
    entry.async_on_unload(coordinator.async_add_listener(_add_new_streams))


class SendspinEndpointMediaPlayer(SendspinEndpointEntity, MediaPlayerEntity):
    """A Sendspin speaker exposed as a media player."""

    _attr_name = None  # the device name is the entity name
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_volume_step = VOLUME_STEP_PERCENT / 100

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Only claim what this endpoint can actually honour.

        Volume is offered solely when the speaker has reported one. A server
        can command a player's volume but cannot read it back unless the player
        echoes `client/state`, and many never do — offering a slider that
        cannot show the current level is worse than offering nothing.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return MediaPlayerEntityFeature(0)

        features = MediaPlayerEntityFeature(0)

        # Volume goes over whichever connection exists: ours when we hold the
        # speaker, otherwise the unit that does, via the mesh. Handing a speaker
        # to a unit must not cost the user their volume control.
        if endpoint.connected or endpoint.held_by_unit_host is not None:
            if endpoint.volume is not None:
                features |= (
                    MediaPlayerEntityFeature.VOLUME_SET
                    | MediaPlayerEntityFeature.VOLUME_STEP
                )
            if endpoint.muted is not None:
                features |= MediaPlayerEntityFeature.VOLUME_MUTE

        # Source selection does NOT. It is an HTTP call to the owning unit,
        # keyed on the speaker's URL, so it works whether or not we hold the
        # speaker — and it has to, because handing a speaker to another unit is
        # precisely what makes us stop holding it. Gating this on `connected`
        # made the control destroy itself the moment it succeeded.
        #
        # Gated on exactly what `source_list` publishes, or a populated dropdown
        # could appear with no capability to act on it.
        if self.source_list is not None:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE

        # Transport reflects what the server observing this stream actually
        # advertises. Offering a button that will be silently dropped is worse
        # than not offering it. It is read again on each state write, so the
        # buttons follow the stream: an AirPlay stream has four, a Spotify
        # stream adds repeat and shuffle, and no stream has none.
        for command in self._stream_commands():
            features |= _TRANSPORT_FEATURES.get(command, MediaPlayerEntityFeature(0))
        return features

    def _stream_commands(self) -> tuple[str, ...]:
        """What the selected stream accepts. Empty when no stream is selected.

        A transport command acts on a stream. A speaker at none has no stream,
        so it has no transport, whatever a link somewhere reports.
        """
        endpoint = self.endpoint
        if endpoint is None or endpoint.media_link is None:
            return ()
        if self.source in (None, SOURCE_NONE):
            return ()
        return endpoint.media_commands

    @property
    def source_list(self) -> list[str] | None:
        """Streams this speaker can be assigned to, right now.

        Rebuilt on every mesh poll, so a source appearing or disappearing shows
        up without any entity being created or destroyed. That is the whole
        reason entities are anchored to speakers rather than to streams.

        **Only live sources.** A Plum unit publishes every configured input
        whether or not a sender is connected to it, so listing them all offered
        an AirPlay endpoint nothing was feeding as if it were a stream. What is
        selectable is now what is actually available.

        `None` — no dropdown at all — is reserved for having nothing to choose
        between. Once there is a mesh, `SOURCE_NONE` is always offered, because
        taking a speaker off a stream must never stop being possible.
        """
        data = self.coordinator.data
        if not data.sources and not data.servers:
            return None

        live = [source.label for source in data.live_sources]
        # Other Sendspin servers are offered alongside streams, because handing
        # a speaker back to Music Assistant is exactly as much a routing choice
        # as putting it on a unit's input — and there was previously no way to
        # express it at all.
        options = [
            SOURCE_NONE,
            *(name for _server_id, name in data.servers),
            *live,
        ]

        # Pin whatever is selected, even once it stops being live. A stream that
        # ends while a speaker is on it would otherwise leave the entity
        # reporting a source absent from its own options, which Home Assistant
        # renders as a blank dropdown.
        current = self.source
        if current is not None and current not in options:
            options.append(current)
        return options

    @property
    def source(self) -> str | None:
        """The stream this speaker is on, or None for nothing."""
        endpoint = self.endpoint
        data = self.coordinator.data
        if endpoint is None or (not data.sources and not data.servers):
            # Nothing to select between, so reporting a selection would show it
            # against a dropdown that does not exist.
            return None
        if endpoint.source_label is not None:
            return endpoint.source_label
        if endpoint.held_by_us or endpoint.held_by_plum_unit:
            # We are the holder. Naming ourselves as the source was confusing
            # and put a value in the box that is in no dropdown — Home Assistant
            # holding a speaker on no stream *is* the none state.
            #
            # The same is true of a Plum unit. A unit detaches every speaker
            # when a source dies and keeps the socket, so this is the usual
            # state after a stream ends. Its name is no stream and no option.
            return SOURCE_NONE
        # Held by another server rather than on one of our streams. Saying so
        # is the honest answer; "None" read as "not routed anywhere".
        return endpoint.held_by_server or SOURCE_NONE

    async def async_select_source(self, source: str) -> None:
        """Assign this speaker to a stream, or take it off one.

        This is the routing verb. Several speakers selecting the same source
        *is* a group — that is Sendspin's own semantics, which is why no
        separate grouping feature is advertised.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return

        current = self.coordinator.mesh_view.source_for_player(endpoint.client_id)

        memo = self.coordinator.memo

        if source == SOURCE_NONE:
            if current is not None:
                await self._call_mesh(
                    self.coordinator.mesh.async_unassign(
                        current, endpoint.dial_url, endpoint.client_id
                    )
                )
            # Taking it off a stream means we want it back, so start holding it
            # again and stop suppressing adoption on restart.
            memo.set_routed_away(self._frozen_url, False)
            memo.set_handed_to_server(self._frozen_url, None)
            memo.async_schedule_save()
            await self.coordinator.host.async_adopt(endpoint.dial_url)
        elif source in {name for _server_id, name in self.coordinator.data.servers}:
            # Hand it to another server. There is no protocol verb for "give
            # this speaker to that server", so we stop holding it and let that
            # server's own dialling take it — which is how it got there before.
            if current is not None:
                await self._call_mesh(
                    self.coordinator.mesh.async_unassign(
                        current, endpoint.dial_url, endpoint.client_id
                    )
                )
            memo.set_routed_away(self._frozen_url, True)
            # Remember it was a *server*, not a stream. A server hand-off cannot
            # be verified through the mesh — the speaker is on no source by
            # design — so the stranded rescue must not treat that as a failure
            # and take the speaker straight back.
            memo.set_handed_to_server(self._frozen_url, source)
            memo.async_schedule_save()
            self.coordinator.async_note_routing(self._frozen_url)
            await self.coordinator.host.async_release(endpoint.dial_url)
        else:
            target = self.coordinator.mesh_view.source_by_label(source)
            if target is None:
                raise HomeAssistantError(f"No Sendspin stream named {source!r}")
            if current is not None and current.key == target.key:
                return
            # Stop holding it ourselves first, so we are not competing with the
            # unit we are asking to take it. A player always yields to the
            # newest dialer, so two servers dialling is a tug-of-war.
            await self.coordinator.host.async_release(endpoint.dial_url)
            # Remember this was deliberate. Otherwise the next restart re-dials
            # the speaker and takes it straight back off the stream.
            memo.set_routed_away(self._frozen_url, True)
            # A stream hand-off *is* verifiable through the mesh, so the rescue
            # stays armed for it — this is the case it exists for.
            memo.set_handed_to_server(self._frozen_url, None)
            memo.async_schedule_save()
            self.coordinator.async_note_routing(self._frozen_url)
            await self._call_mesh(
                self.coordinator.mesh.async_assign(target, endpoint.dial_url)
            )

        # Plum aggregates on a 2s loop, so without this the dropdown would lag
        # the action by up to a full poll interval.
        await self.coordinator.async_refresh_mesh()

    async def _call_mesh(self, awaitable) -> None:
        """Run a mesh call, surfacing failures to the user."""
        try:
            await awaitable
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def state(self) -> MediaPlayerState | None:
        """What this speaker is doing, as far as anything can tell us.

        | State | When |
        |---|---|
        | playing | the stream it is on plays |
        | paused | the stream it is on is paused |
        | idle | it is on no stream, or the stream stopped |
        | unavailable | nothing can see the speaker (see `available`) |

        Home Assistant originates no audio, so a speaker we hold is idle by
        definition. But a speaker sitting on someone else's stream may well be
        playing, and the mesh tells us whether audio is flowing on it — so say
        so rather than reporting idle over the top of music.

        The state comes from the transport state of the stream, never from the
        title. A server keeps the last title after a stream stops, and reading
        the title held this at paused for as long as the link stayed open.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return None
        if self.source in (None, SOURCE_NONE) and endpoint.media_link is None:
            return MediaPlayerState.IDLE
        if endpoint.media_state == "paused":
            # The link is pushed and the mesh is polled, so the link knows of a
            # pause first. Do not let a stale `streaming` flag play over it.
            return MediaPlayerState.PAUSED
        if endpoint.media_state == "playing" or (
            endpoint.source_label is not None and endpoint.source_streaming
        ):
            # The mesh half matters when no link reads the stream, and when a
            # link reads `stopped` because it was not placed in the group.
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def _shows_media(self) -> bool:
        """Whether there is a current track to show.

        False when the stream ended or the source is None. Each media property
        reads this, so the title, the artist, the album and the cover all go at
        the same moment and a card does not keep a track that is over.
        """
        return self.state in (MediaPlayerState.PLAYING, MediaPlayerState.PAUSED)

    @property
    def media_title(self) -> str | None:
        """What is playing on this speaker's stream."""
        return self.endpoint.media_title if self._shows_media else None

    @property
    def media_artist(self) -> str | None:
        """Artist of the current track."""
        return self.endpoint.media_artist if self._shows_media else None

    @property
    def media_album_name(self) -> str | None:
        """Album of the current track."""
        return self.endpoint.media_album if self._shows_media else None

    @property
    def media_album_artist(self) -> str | None:
        """Album artist of the current track."""
        return self.endpoint.media_album_artist if self._shows_media else None

    @property
    def source_kind(self) -> str | None:
        """What kind of thing feeds this speaker, or None on no stream.

        `airplay`, `bluetooth` or `spotify` for a stream on a Plum-Audio unit,
        and `server` for another Sendspin server. None also for a unit stream
        whose source id names no kind we know.
        """
        endpoint = self.endpoint
        if endpoint is None or self.source in (None, SOURCE_NONE):
            return None
        if endpoint.source_label is not None:
            return endpoint.source_kind
        return KIND_SERVER

    @property
    def app_name(self) -> str | None:
        """The name of what feeds this speaker: AirPlay, or a server's name."""
        kind = self.source_kind
        if kind == KIND_SERVER:
            return self.source
        return _KIND_NAMES.get(kind) if kind is not None else None

    @property
    def media_content_type(self) -> MediaType | None:
        """Sendspin carries audio and says nothing finer."""
        return MediaType.MUSIC if self._shows_media else None

    @property
    def media_duration(self) -> int | None:
        """Track length in seconds, or None for a live stream."""
        endpoint = self.endpoint
        if not self._shows_media or endpoint.media_duration is None:
            return None
        return int(endpoint.media_duration)

    @property
    def media_position(self) -> int | None:
        """Position in seconds, as of `media_position_updated_at`.

        Reported rather than extrapolated: position is never pushed
        periodically, and Home Assistant already advances it from the timestamp
        while the entity is playing.

        Read-only. The protocol has no seek command, so `SEEK` is never offered.
        None when the sender reports no progress, as some AirPlay senders do.
        """
        endpoint = self.endpoint
        if not self._shows_media or endpoint.media_position is None:
            return None
        return int(endpoint.media_position)

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was true, on the server's clock moved to ours.

        None with no position. A time with nothing to extrapolate from is noise.
        """
        endpoint = self.endpoint
        if (
            not self._shows_media
            or endpoint.media_position is None
            or endpoint.media_position_updated_at is None
        ):
            return None
        return dt_util.utc_from_timestamp(endpoint.media_position_updated_at)

    @property
    def media_image_hash(self) -> str | None:
        """Identify the current cover art.

        Home Assistant derives this from `media_image_url` and, finding none,
        publishes no picture and never asks for the image — so binary artwork
        would be read correctly and then thrown away. Hashing the bytes gives
        it something to key the proxy URL on, and changes when the art does.
        """
        cover = self._cover_info()
        return cover[1] if cover is not None else None

    def _cover_info(self) -> tuple[bytes, str, str] | None:
        """The current cover of this speaker's stream, or None with no track.

        A link keeps the last cover until the server sends an empty frame,
        which a stream that ends does not do.
        """
        if not self._shows_media:
            return None
        return cover_for(
            self.coordinator.link_for(
                self.endpoint.media_link if self.endpoint else None
            )
        )

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Cover art, pushed as binary frames by the artwork role.

        Plum-Audio publishes no `artwork_url`, so these frames are the only
        cover art that exists.
        """
        cover = self._cover_info()
        if cover is None:
            return None, None
        return cover[0], cover[2]

    async def async_media_play(self) -> None:
        """Resume the stream this speaker is on."""
        await self._async_transport("play")

    async def async_media_pause(self) -> None:
        """Pause the stream this speaker is on."""
        await self._async_transport("pause")

    async def async_media_stop(self) -> None:
        """Stop the stream this speaker is on."""
        await self._async_transport("stop")

    async def async_media_next_track(self) -> None:
        """Skip forward on the stream this speaker is on."""
        await self._async_transport("next")

    async def async_media_previous_track(self) -> None:
        """Skip back on the stream this speaker is on."""
        await self._async_transport("previous")

    @property
    def repeat(self) -> RepeatMode | None:
        """The repeat mode of the stream, when the stream has one."""
        endpoint = self.endpoint
        if endpoint is None or not (
            self.supported_features & MediaPlayerEntityFeature.REPEAT_SET
        ):
            return None
        try:
            return RepeatMode(endpoint.media_repeat)
        except ValueError:
            return None

    @property
    def shuffle(self) -> bool | None:
        """Whether the stream shuffles, when the stream can."""
        endpoint = self.endpoint
        if endpoint is None or not (
            self.supported_features & MediaPlayerEntityFeature.SHUFFLE_SET
        ):
            return None
        return endpoint.media_shuffle

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set the repeat mode of the stream this speaker is on."""
        await self._async_transport(_REPEAT_COMMANDS[repeat])

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Turn shuffle on or off for the stream this speaker is on."""
        await self._async_transport("shuffle" if shuffle else "unshuffle")

    async def _async_transport(self, command: str) -> None:
        """Send a transport command to the server driving this stream.

        These act on the **stream**, not the speaker: everything listening to
        it is affected, which is what a group means in Sendspin.
        """
        endpoint = self.endpoint
        link = self.coordinator.link_for(endpoint.media_link if endpoint else None)
        if link is None:
            raise HomeAssistantError(
                "Nothing is observing this stream, so it cannot be controlled"
            )
        if command not in self._stream_commands():
            # The server drops a command it did not advertise. Say so, or a
            # stale card looks as if it worked.
            raise HomeAssistantError(
                f"The stream on {endpoint.name} does not accept {command!r}"
            )
        try:
            await link.async_send_command(command)
        except ConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def volume_level(self) -> float | None:
        """0.0-1.0, or None when the speaker has never reported a level.

        This is the **endpoint** level: the gain of this one speaker. It is not
        the group level of the stream, and it is not the source level on the
        sending device. Those are separate volumes in Plum-Audio, and this
        entity reads and writes neither of them.

        None must stay None. Substituting a default makes every speaker read
        100% while the audio is demonstrably quieter, which looks exactly like
        a UI bug and is not one.
        """
        endpoint = self.endpoint
        if endpoint is None or endpoint.volume is None:
            return None
        return endpoint.volume / 100

    @property
    def is_volume_muted(self) -> bool | None:
        """Mute state, or None when the speaker has never reported one."""
        endpoint = self.endpoint
        return endpoint.muted if endpoint is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Surface why a speaker is adopted but not held.

        Without this, an endpoint that another server has taken looks simply
        broken. See docs/OPEN-QUESTIONS.md §7.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return None
        attributes: dict[str, str] = {}
        if (kind := self.source_kind) is not None:
            attributes["source_kind"] = kind
        if endpoint.yielded_reason is not None:
            attributes["yielded_to"] = endpoint.yielded_reason
        if endpoint.routed_away:
            attributes["routed_away"] = "true"
        if endpoint.held_by_server is not None and not endpoint.connected:
            # Which server has it, so "not on a stream" is explicable rather
            # than mysterious.
            attributes["held_by"] = endpoint.held_by_server
        return attributes or None

    async def async_set_volume_level(self, volume: float) -> None:
        """Command the speaker's volume, through whoever is holding it."""
        await self._async_command_volume(volume=round(volume * 100))

    async def async_volume_up(self) -> None:
        """Raise the speaker's volume by one step."""
        await self._async_step_volume(VOLUME_STEP_PERCENT)

    async def async_volume_down(self) -> None:
        """Lower the speaker's volume by one step."""
        await self._async_step_volume(-VOLUME_STEP_PERCENT)

    async def _async_step_volume(self, delta: int) -> None:
        """Move the endpoint level by `delta` percent, held inside 0-100.

        Whole percent, because that is the unit of the protocol. A float step
        on `volume_level` drifts, and two presses do not always undo each other.
        """
        endpoint = self.endpoint
        if endpoint is None or endpoint.volume is None:
            raise HomeAssistantError(
                "The volume of this speaker is not known, so it cannot be stepped"
            )
        await self._async_command_volume(
            volume=max(0, min(100, endpoint.volume + delta))
        )

    async def async_mute_volume(self, mute: bool) -> None:
        """Command the speaker's mute state, through whoever is holding it."""
        await self._async_command_volume(muted=mute)

    async def _async_command_volume(
        self, *, volume: int | None = None, muted: bool | None = None
    ) -> None:
        """Route a volume command over whichever connection exists."""
        endpoint = self.endpoint
        if endpoint is None:
            return

        if endpoint.connected and (role := self._player_role()) is not None:
            if volume is not None:
                role.set_player_volume(volume)
            if muted is not None:
                role.set_player_mute(muted)
            return

        if endpoint.held_by_unit_host is None or endpoint.client_id is None:
            raise HomeAssistantError(
                f"Nothing is holding {endpoint.name}, so its volume cannot be set"
            )
        # Always send the pair. A unit before the 2026-11 change refuses a call
        # that has no `volume`, and reads an absent `muted` as false. So a mute
        # alone fails there, and a volume alone unmutes. A current unit accepts
        # either field alone and gets the same result from the pair.
        await self._call_mesh(
            self.coordinator.mesh.async_set_volume(
                endpoint.held_by_unit_host,
                endpoint.client_id,
                volume=volume if volume is not None else endpoint.volume,
                muted=muted if muted is not None else endpoint.muted,
            )
        )
        await self.coordinator.async_refresh_mesh()

    def _player_role(self) -> object | None:
        """The live player role for this endpoint, if it is connected."""
        endpoint = self.endpoint
        if endpoint is None or endpoint.client_id is None:
            return None
        client = self.coordinator.host.server.get_client(endpoint.client_id)
        return player_role(client) if client is not None else None


class SendspinStreamMediaPlayer(
    CoordinatorEntity[SendspinCoordinator], MediaPlayerEntity
):
    """A mesh source — a stream — exposed as a media player.

    The counterpart of an endpoint: what plays, where it plays and how loud
    the sender is, for everything on the stream at once.

    | Control | Acts on |
    |---|---|
    | play, pause, next, previous, repeat, shuffle | the stream |
    | volume and mute | the **source** level, on the sending device |
    | source dropdown | every adopted speaker on the stream, moved together |

    No `GROUPING`: a stream's speakers are the speakers whose own source is
    this stream, listed in the `endpoints` attribute. Joining a stream is still
    an endpoint's `select_source`, and nothing else.
    """

    # Named by its dropdown label, so a stream reads the same everywhere.
    _attr_has_entity_name = False
    _attr_device_class = MediaPlayerDeviceClass.RECEIVER
    _attr_volume_step = VOLUME_STEP_PERCENT / 100

    def __init__(self, coordinator: SendspinCoordinator, key: str) -> None:
        """Bind the entity to a source key, which is unique on the mesh."""
        super().__init__(coordinator)
        self._key = key
        stream = coordinator.data.streams[key]
        self._attr_unique_id = f"source:{key}"
        self._attr_name = stream.label
        self._attr_icon = _KIND_ICONS.get(stream.kind, "mdi:waveform")
        # One device per unit, so a unit's streams sit together and take the
        # unit's area. The unit id is its identity, never its host.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"unit:{stream.unit_id}")},
            name=stream.unit_name,
            manufacturer="Plum Solutions",
            model="Plum-Audio unit",
            configuration_url=f"http://{stream.unit_host}:5001/"
            if stream.unit_host
            else None,
        )

    @property
    def stream(self) -> StreamSnapshot | None:
        """This stream's latest snapshot, if the mesh still lists it."""
        return self.coordinator.data.streams.get(self._key)

    @property
    def available(self) -> bool:
        """Present in the mesh view. A stream with no sender is `off`, not gone."""
        return super().available and self.stream is not None

    @property
    def state(self) -> MediaPlayerState | None:
        """`off` with no sender, else what the stream's transport says.

        The same reading as an endpoint's, so a stream and the speakers on it
        show the same state at the same moment.
        """
        stream = self.stream
        if stream is None:
            return None
        if not stream.live:
            return MediaPlayerState.OFF
        if stream.media_state == "paused":
            return MediaPlayerState.PAUSED
        if stream.media_state == "playing" or stream.streaming:
            return MediaPlayerState.PLAYING
        return MediaPlayerState.IDLE

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Transport the stream advertises, and the source level when it exists.

        Volume only when the source both accepts one and has reported a level:
        a slider that cannot show where it is would read 100% over quieter
        audio. Bluetooth senders accept none.
        """
        stream = self.stream
        if stream is None:
            return MediaPlayerEntityFeature(0)
        features = MediaPlayerEntityFeature(0)
        if stream.supports_source_volume and stream.live:
            if stream.source_volume is not None:
                features |= (
                    MediaPlayerEntityFeature.VOLUME_SET
                    | MediaPlayerEntityFeature.VOLUME_STEP
                )
            if stream.source_muted is not None:
                features |= MediaPlayerEntityFeature.VOLUME_MUTE
        if self.source_list is not None:
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        for command in self._stream_commands():
            features |= _TRANSPORT_FEATURES.get(command, MediaPlayerEntityFeature(0))
        return features

    def _stream_commands(self) -> tuple[str, ...]:
        """What the stream accepts now. Nothing with no sender."""
        stream = self.stream
        if stream is None or stream.media_link is None or not stream.live:
            return ()
        return stream.media_commands

    @property
    def _shows_media(self) -> bool:
        return self.state in (MediaPlayerState.PLAYING, MediaPlayerState.PAUSED)

    @property
    def media_title(self) -> str | None:
        """What plays on the stream."""
        return self.stream.media_title if self._shows_media else None

    @property
    def media_artist(self) -> str | None:
        """Artist of the current track."""
        return self.stream.media_artist if self._shows_media else None

    @property
    def media_album_name(self) -> str | None:
        """Album of the current track."""
        return self.stream.media_album if self._shows_media else None

    @property
    def media_album_artist(self) -> str | None:
        """Album artist of the current track."""
        return self.stream.media_album_artist if self._shows_media else None

    @property
    def media_content_type(self) -> MediaType | None:
        """Sendspin carries audio and says nothing finer."""
        return MediaType.MUSIC if self._shows_media else None

    @property
    def app_name(self) -> str | None:
        """AirPlay, Bluetooth or Spotify."""
        stream = self.stream
        return _KIND_NAMES.get(stream.kind) if stream and stream.kind else None

    @property
    def media_duration(self) -> int | None:
        """Track length in seconds, or None for a live stream."""
        stream = self.stream
        if not self._shows_media or stream.media_duration is None:
            return None
        return int(stream.media_duration)

    @property
    def media_position(self) -> int | None:
        """Position in seconds, as of `media_position_updated_at`. Read-only."""
        stream = self.stream
        if not self._shows_media or stream.media_position is None:
            return None
        return int(stream.media_position)

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was true. None with no position."""
        stream = self.stream
        if (
            not self._shows_media
            or stream.media_position is None
            or stream.media_position_updated_at is None
        ):
            return None
        return dt_util.utc_from_timestamp(stream.media_position_updated_at)

    @property
    def media_image_hash(self) -> str | None:
        """Hash of the cover bytes, so Home Assistant publishes a picture."""
        cover = self._cover_info()
        return cover[1] if cover is not None else None

    def _cover_info(self) -> tuple[bytes, str, str] | None:
        """The current cover of the stream, or None with no track."""
        if not self._shows_media:
            return None
        return cover_for(self.coordinator.link_for(self.stream.media_link))

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Cover art, from the link reading this stream."""
        cover = self._cover_info()
        if cover is None:
            return None, None
        return cover[0], cover[2]

    async def async_media_play(self) -> None:
        """Resume the stream."""
        await self._async_transport("play")

    async def async_media_pause(self) -> None:
        """Pause the stream."""
        await self._async_transport("pause")

    async def async_media_stop(self) -> None:
        """Stop the stream."""
        await self._async_transport("stop")

    async def async_media_next_track(self) -> None:
        """Skip forward on the stream."""
        await self._async_transport("next")

    async def async_media_previous_track(self) -> None:
        """Skip back on the stream."""
        await self._async_transport("previous")

    @property
    def repeat(self) -> RepeatMode | None:
        """The repeat mode, when the stream has one."""
        stream = self.stream
        if stream is None or not (
            self.supported_features & MediaPlayerEntityFeature.REPEAT_SET
        ):
            return None
        try:
            return RepeatMode(stream.media_repeat)
        except ValueError:
            return None

    @property
    def shuffle(self) -> bool | None:
        """Whether the stream shuffles, when it can."""
        stream = self.stream
        if stream is None or not (
            self.supported_features & MediaPlayerEntityFeature.SHUFFLE_SET
        ):
            return None
        return stream.media_shuffle

    async def async_set_repeat(self, repeat: RepeatMode) -> None:
        """Set the repeat mode of the stream."""
        await self._async_transport(_REPEAT_COMMANDS[repeat])

    async def async_set_shuffle(self, shuffle: bool) -> None:
        """Turn shuffle on or off."""
        await self._async_transport("shuffle" if shuffle else "unshuffle")

    async def _async_transport(self, command: str) -> None:
        """Send a transport command over the link reading this stream."""
        stream = self.stream
        link = self.coordinator.link_for(stream.media_link if stream else None)
        if link is None:
            raise HomeAssistantError(
                "Nothing is observing this stream, so it cannot be controlled"
            )
        if command not in self._stream_commands():
            raise HomeAssistantError(f"This stream does not accept {command!r}")
        try:
            await link.async_send_command(command)
        except ConnectionError as err:
            raise HomeAssistantError(str(err)) from err

    @property
    def volume_level(self) -> float | None:
        """The **source** level, 0.0-1.0. Not any speaker's, and not the group's."""
        stream = self.stream
        if stream is None or stream.source_volume is None:
            return None
        return stream.source_volume / 100

    @property
    def is_volume_muted(self) -> bool | None:
        """Mute at the source, or None when the source reports none."""
        stream = self.stream
        return stream.source_muted if stream is not None else None

    async def async_set_volume_level(self, volume: float) -> None:
        """Set the level on the sending device."""
        await self._async_source_volume(volume=round(volume * 100))

    async def async_volume_up(self) -> None:
        """Raise the source level by one step."""
        await self._async_step_volume(VOLUME_STEP_PERCENT)

    async def async_volume_down(self) -> None:
        """Lower the source level by one step."""
        await self._async_step_volume(-VOLUME_STEP_PERCENT)

    async def _async_step_volume(self, delta: int) -> None:
        stream = self.stream
        if stream is None or stream.source_volume is None:
            raise HomeAssistantError(
                "The volume of this stream is not known, so it cannot be stepped"
            )
        await self._async_source_volume(
            volume=max(0, min(100, stream.source_volume + delta))
        )

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute at the source."""
        await self._async_source_volume(muted=mute)

    async def _async_source_volume(
        self, *, volume: int | None = None, muted: bool | None = None
    ) -> None:
        source = self._mesh_source()
        if source is None or not source.supports_source_volume:
            raise HomeAssistantError("This stream has no source volume")
        try:
            await self.coordinator.mesh.async_set_source_volume(
                source, volume=volume, muted=muted
            )
        except MeshError as err:
            raise HomeAssistantError(str(err)) from err
        await self.coordinator.async_refresh_mesh()

    def _mesh_source(self):
        return next(
            (s for s in self.coordinator.mesh_view.sources if s.key == self._key),
            None,
        )

    @property
    def source_list(self) -> list[str] | None:
        """Where the speakers on this stream can be moved, all at once.

        The same options an endpoint's dropdown offers. Empty of choices — no
        dropdown — while no adopted speaker is on the stream, since there is
        then nothing to move.
        """
        stream = self.stream
        if stream is None or not stream.members:
            return None
        data = self.coordinator.data
        options = [
            SOURCE_NONE,
            *(name for _server_id, name in data.servers),
            *(source.label for source in data.live_sources),
        ]
        if stream.label not in options:
            options.append(stream.label)
        return options

    @property
    def source(self) -> str | None:
        """This stream, which is where its speakers are."""
        stream = self.stream
        return stream.label if stream is not None and stream.members else None

    async def async_select_source(self, source: str) -> None:
        """Move every adopted speaker on this stream to `source`.

        Each speaker is moved by its own `select_source`, so a group move is
        exactly the sum of single moves and cannot route differently from one.
        """
        stream = self.stream
        if stream is None or source == stream.label:
            return
        targets = self._member_entity_ids()
        if not targets:
            raise HomeAssistantError("No speaker is on this stream")
        await self.hass.services.async_call(
            "media_player",
            "select_source",
            {"entity_id": targets, "source": source},
            blocking=True,
        )

    def _member_entity_ids(self) -> list[str]:
        """Entity ids of the adopted speakers on this stream, in name order."""
        stream = self.stream
        if stream is None:
            return []
        registry = er.async_get(self.hass)
        ids = (
            registry.async_get_entity_id("media_player", DOMAIN, f"player:{url}")
            for url in stream.members
        )
        return [entity_id for entity_id in ids if entity_id is not None]

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Who is on the stream, for dashboards and automations.

        `endpoints` is the entity ids of the adopted speakers on it. `speakers`
        is every speaker the mesh shows on it, by name, adopted or not.
        """
        stream = self.stream
        if stream is None:
            return None
        attributes: dict[str, object] = {
            "endpoints": self._member_entity_ids(),
            "speakers": list(stream.speaker_names),
            "source_label": stream.label,
        }
        if stream.kind is not None:
            attributes["source_kind"] = stream.kind
        return attributes
