"""The Sendspin integration.

Home Assistant hosts a source-less Sendspin server so it can adopt and route
generic Sendspin endpoints without any additional hardware on the network. It
never renders audio and never advertises itself over mDNS. See
`server_host.py` for why a controller-role client cannot do this job.

M1 scope: the config entry brings up the identity and the server. Discovery,
adoption and entities arrive in M2, so no platforms are forwarded yet.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .identity import async_load_identity, async_load_pairing_store
from .models import SendspinRuntimeData
from .server_host import async_create_server_host

_LOGGER = logging.getLogger(__name__)

# Populated in M2, when there is something to put on them.
PLATFORMS: list[Platform] = []

type SendspinConfigEntry = ConfigEntry[SendspinRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Set up Sendspin from a config entry."""
    identity = await async_load_identity(hass)
    pairing_store = await async_load_pairing_store(hass)

    try:
        host = await async_create_server_host(
            hass,
            identity=identity,
            pairing_store=pairing_store,
            server_name=entry.data.get(CONF_NAME, hass.config.location_name),
        )
    except Exception as err:
        raise ConfigEntryNotReady(
            f"Could not start the Sendspin server: {err}"
        ) from err

    _LOGGER.debug("Sendspin server ready with server_id %s", host.server_id)
    entry.runtime_data = SendspinRuntimeData(host=host)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Unload a config entry.

    The server is closed *after* the platforms unload but regardless of whether
    they did: leaving dial tasks running would mean a reload races the new
    entry's dialers for the same speakers.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.host.async_close()
    return unloaded
