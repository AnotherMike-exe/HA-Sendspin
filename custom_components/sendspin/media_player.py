"""Media player platform for Sendspin.

One durable entity per **physical endpoint** — a speaker — not one per stream.
Streams come and go constantly; anchoring entities to them would churn the
entity registry and discard the user's renames, areas and icons on every
reconnect. Instead the stream an endpoint is assigned to becomes state *on* the
endpoint, and in M4 the available streams become its `source_list`.

M2 scope: adoption, presence, volume and mute. There is no transport, no
metadata and no source list yet, because Home Assistant originates no audio and
nothing is reading a stream. `supported_features` reflects that honestly rather
than offering controls that cannot be honoured.
"""

from __future__ import annotations

from datetime import datetime
import hashlib

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
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SendspinConfigEntry
from .const import CONF_LISTENER_URL, SOURCE_NONE, SUBENTRY_TYPE_PLAYER
from .entity import SendspinEndpointEntity
from .mesh import MeshError
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
    entry.async_on_unload(coordinator.async_add_listener(_add_new_endpoints))


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

        Home Assistant originates no audio, so a speaker we hold is idle by
        definition. But a speaker sitting on someone else's stream may well be
        playing, and the mesh tells us whether audio is flowing on it — so say
        so rather than reporting idle over the top of music.
        """
        endpoint = self.endpoint
        if endpoint is None:
            return None
        if endpoint.media_title is not None:
            # Home Assistant only surfaces media attributes for a player that
            # is playing or paused, so a speaker with an observed track must
            # say so or its title, artist and cover art never reach the UI.
            return (
                MediaPlayerState.PLAYING
                if endpoint.media_playing
                else MediaPlayerState.PAUSED
            )
        if endpoint.source_label is not None:
            return (
                MediaPlayerState.PLAYING
                if endpoint.source_streaming
                else MediaPlayerState.PAUSED
                if endpoint.media_title is not None
                else MediaPlayerState.IDLE
            )
        if (
            endpoint.connected
            or endpoint.yielded_reason is not None
            or endpoint.known_to_mesh
        ):
            # Present, just not on a stream — or on another server entirely.
            return MediaPlayerState.IDLE
        return MediaPlayerState.OFF

    @property
    def media_title(self) -> str | None:
        """What is playing on this speaker's stream."""
        return self.endpoint.media_title if self.endpoint else None

    @property
    def media_artist(self) -> str | None:
        """Artist of the current track."""
        return self.endpoint.media_artist if self.endpoint else None

    @property
    def media_album_name(self) -> str | None:
        """Album of the current track."""
        return self.endpoint.media_album if self.endpoint else None

    @property
    def media_content_type(self) -> MediaType | None:
        """Sendspin carries audio and says nothing finer."""
        return MediaType.MUSIC if self.endpoint and self.endpoint.media_title else None

    @property
    def media_duration(self) -> int | None:
        """Track length in seconds, or None for a live stream."""
        endpoint = self.endpoint
        if endpoint is None or endpoint.media_duration is None:
            return None
        return int(endpoint.media_duration)

    @property
    def media_position(self) -> int | None:
        """Position in seconds, as of `media_position_updated_at`.

        Reported rather than extrapolated: position is never pushed
        periodically, and Home Assistant already advances it from the timestamp
        while the entity is playing.
        """
        endpoint = self.endpoint
        if endpoint is None or endpoint.media_position is None:
            return None
        return int(endpoint.media_position)

    @property
    def media_position_updated_at(self) -> datetime | None:
        """When the position was last reported."""
        endpoint = self.endpoint
        if endpoint is None or endpoint.media_position_updated_at is None:
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
        artwork = self._artwork()
        return hashlib.sha256(artwork).hexdigest()[:16] if artwork else None

    def _artwork(self) -> bytes | None:
        """Raw cover art from the link observing this speaker's stream."""
        link = self.coordinator.link_for(
            self.endpoint.media_link if self.endpoint else None
        )
        return link.snapshot.artwork if link is not None else None

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Cover art, pushed as binary frames by the artwork role.

        Plum-Audio publishes no `artwork_url`, so these frames are the only
        cover art that exists.
        """
        artwork = self._artwork()
        if not artwork:
            return None, None
        return artwork, "image/jpeg"

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
