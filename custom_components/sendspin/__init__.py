"""The Sendspin integration.

Home Assistant hosts a source-less Sendspin server so it can adopt and route
generic Sendspin endpoints without any additional hardware on the network. It
never renders audio and never advertises itself over mDNS. See
`server_host.py` for why a controller-role client cannot do this job.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_LISTENER_URL, SUBENTRY_TYPE_PLAYER
from .coordinator import SendspinCoordinator
from .identity import async_load_identity, async_load_pairing_store
from .models import SendspinRuntimeData
from .player_memo import PlayerMemo
from .server_host import async_create_server_host
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.MEDIA_PLAYER]

type SendspinConfigEntry = ConfigEntry[SendspinRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Set up Sendspin from a config entry."""
    identity = await async_load_identity(hass)
    pairing_store = await async_load_pairing_store(hass)

    memo = PlayerMemo(hass)
    await memo.async_load()

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

    coordinator = SendspinCoordinator(hass, entry, host, memo)
    coordinator.async_start()
    entry.runtime_data = SendspinRuntimeData(
        host=host, coordinator=coordinator, memo=memo
    )

    await _async_restore_adoptions(entry, host, coordinator, memo)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    # Registered once per Home Assistant, not per entry — services are global,
    # and re-registering on reload would replace handlers mid-call.
    async_register_services(hass)
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: SendspinConfigEntry) -> None:
    """Reload when the user adopts or removes a speaker.

    Adoption changes which speakers we dial and which entities exist. Reloading
    is blunt but keeps a single code path for bringing an adoption up, so a
    speaker added at runtime is set up exactly as one restored at startup.
    """
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_restore_adoptions(
    entry: SendspinConfigEntry,
    host: object,
    coordinator: SendspinCoordinator,
    memo: PlayerMemo,
) -> None:
    """Re-adopt every speaker the user has previously consented to.

    Each adopted speaker is a config subentry, so the set survives restarts
    without the integration ever adopting anything the user did not ask for.
    """
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PLAYER:
            continue
        frozen_url = subentry.data[CONF_LISTENER_URL]
        coordinator.async_track_endpoint(frozen_url)
        await host.async_adopt(memo.dial_url(frozen_url))


async def async_unload_entry(hass: HomeAssistant, entry: SendspinConfigEntry) -> bool:
    """Unload a config entry.

    The coordinator stops listening before the server closes, and the server is
    closed regardless of whether the platforms unloaded cleanly: leaving dial
    tasks running would mean a reload races the new entry's dialers for the
    same speakers.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime = entry.runtime_data
    runtime.coordinator.async_stop()
    await runtime.host.async_close()
    await runtime.memo.async_save()
    return unloaded
