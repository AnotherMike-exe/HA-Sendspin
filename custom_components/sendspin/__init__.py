"""The Sendspin integration.

Controller-role only: this integration discovers and controls Sendspin servers
and players on the LAN. It does not render audio.

Scaffold status: structure only. Setup/teardown is not yet implemented.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

# TODO: narrow to ConfigEntry[SendspinRuntimeData] once runtime_data exists (M1).
type SendspinConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Set up Sendspin from a config entry.

    TODO:
      - Construct the aiosendspin controller client for entry listener URL.
      - Open the controller websocket and attach the push-state coordinator.
      - Store the coordinator on `entry.runtime_data`.
      - Register the group/roam services (once, guarded against re-entry).
    """
    raise NotImplementedError("Sendspin setup is not implemented yet")


async def async_unload_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Unload a config entry.

    TODO: close the controller websocket and cancel any reconnect task before
    unloading platforms, so a reload does not leak a socket.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
