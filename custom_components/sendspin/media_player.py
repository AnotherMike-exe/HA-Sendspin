"""Media player platform for Sendspin.

One entity per active stream/group: source, transport controls, and metadata /
artwork via the Sendspin metadata role.

Scaffold status: structure only.
"""

from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SendspinConfigEntry
from .coordinator import SendspinCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SendspinConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Sendspin media players from a config entry.

    TODO: add entities for the streams present at setup, and subscribe to the
    coordinator so streams appearing later are added dynamically.
    """
    raise NotImplementedError


class SendspinMediaPlayer(CoordinatorEntity[SendspinCoordinator], MediaPlayerEntity):
    """A Sendspin stream/group exposed as a media player.

    TODO:
      - `_attr_unique_id` derives from the listener URL, never the client id.
      - `available` reflects mesh presence so automations can trigger on a
        player dropping off.
      - Supported features depend on what the controller role actually exposes;
        do not claim transport support that cannot be honoured.
    """

    _attr_has_entity_name = True
