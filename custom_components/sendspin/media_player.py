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

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SendspinConfigEntry
from .const import CONF_LISTENER_URL, SOURCE_NONE, SUBENTRY_TYPE_PLAYER
from .entity import SendspinEndpointEntity
from .mesh import MeshError
from .server_host import player_role


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

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Only claim what this endpoint can actually honour.

        Volume is offered solely when the speaker has reported one. A server
        can command a player's volume but cannot read it back unless the player
        echoes `client/state`, and many never do — offering a slider that
        cannot show the current level is worse than offering nothing.
        """
        endpoint = self.endpoint
        if endpoint is None or not endpoint.connected:
            return MediaPlayerEntityFeature(0)

        features = MediaPlayerEntityFeature(0)
        if endpoint.volume is not None:
            features |= MediaPlayerEntityFeature.VOLUME_SET
        if endpoint.muted is not None:
            features |= MediaPlayerEntityFeature.VOLUME_MUTE
        if self.coordinator.data.sources:
            # Only offered where streams actually exist — with no Plum mesh on
            # the network there is nothing to select, and an empty dropdown is
            # worse than no dropdown.
            features |= MediaPlayerEntityFeature.SELECT_SOURCE
        return features

    @property
    def source_list(self) -> list[str] | None:
        """Streams this speaker can be assigned to, right now.

        Rebuilt on every mesh poll, so a source appearing or disappearing shows
        up without any entity being created or destroyed. That is the whole
        reason entities are anchored to speakers rather than to streams.

        All sources are listed, active ones first — not just active ones.
        Assigning a speaker and then starting the music is a normal workflow,
        and on a real mesh everything reads inactive most of the time.
        """
        sources = self.coordinator.data.sources
        if not sources:
            return None
        return [SOURCE_NONE, *(source.label for source in sources)]

    @property
    def source(self) -> str | None:
        """The stream this speaker is on, or None for nothing."""
        endpoint = self.endpoint
        if endpoint is None:
            return None
        return endpoint.source_label or SOURCE_NONE

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

        if source == SOURCE_NONE:
            if current is None:
                return
            await self._call_mesh(
                self.coordinator.mesh.async_unassign(
                    current, endpoint.dial_url, endpoint.client_id
                )
            )
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
        """Home Assistant renders `None` as unknown, which is the honest answer.

        Nothing can be playing yet: Home Assistant originates no audio in this
        version, so a connected endpoint is idle by definition.
        """
        endpoint = self.endpoint
        if endpoint is None or not endpoint.connected:
            return MediaPlayerState.OFF
        return MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float | None:
        """0.0-1.0, or None when the speaker has never reported a level.

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
        if endpoint is None or endpoint.yielded_reason is None:
            return None
        return {"yielded_to": endpoint.yielded_reason}

    async def async_set_volume_level(self, volume: float) -> None:
        """Command the speaker's volume."""
        if (role := self._player_role()) is not None:
            role.set_player_volume(round(volume * 100))

    async def async_mute_volume(self, mute: bool) -> None:
        """Command the speaker's mute state."""
        if (role := self._player_role()) is not None:
            role.set_player_mute(mute)

    def _player_role(self) -> object | None:
        """The live player role for this endpoint, if it is connected."""
        endpoint = self.endpoint
        if endpoint is None or endpoint.client_id is None:
            return None
        client = self.coordinator.host.server.get_client(endpoint.client_id)
        return player_role(client) if client is not None else None
