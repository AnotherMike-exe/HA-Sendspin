"""Sendspin services.

Three verbs, covering the adoption lifecycle:

- **adopt_player** — start holding a speaker. Also available through the UI as
  a subentry; this exists for automations and for speakers mDNS never showed.
- **release_player** — stop holding it, so another server can have it.
- **reclaim_player** — take it back from a server that has it. This is the
  escalation from a *yielded* adoption: Home Assistant gives up a contested
  speaker rather than fighting for it, and this is how the user overrides that.

**No service takes a `player_id`.** Every one targets a Home Assistant entity or
device, and the sole raw identifier accepted anywhere is the listener URL — on
`adopt_player`, where by definition no entity exists yet. Plum-Audio's docs
call identifying speakers by `player_id` a standing bug generator: mDNS names
by instance while the handshake reports a MAC, so the two identity views share
only the URL.
"""

from __future__ import annotations

import logging
from types import MappingProxyType

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import (
    ATTR_LISTENER_URL,
    ATTR_TIMEOUT,
    CONF_LISTENER_URL,
    DOMAIN,
    SERVICE_ADOPT_PLAYER,
    SERVICE_RECLAIM_PLAYER,
    SERVICE_RELEASE_PLAYER,
    SUBENTRY_TYPE_PLAYER,
)
from .discovery import normalise_listener_url

_LOGGER = logging.getLogger(__name__)

_ADOPT_SCHEMA = vol.Schema({vol.Required(ATTR_LISTENER_URL): cv.string})

_TARGET_SCHEMA = vol.Schema(
    {
        vol.Required("device_id"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(ATTR_TIMEOUT, default=30.0): vol.Coerce(float),
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the Sendspin services once per Home Assistant instance."""
    if hass.services.has_service(DOMAIN, SERVICE_ADOPT_PLAYER):
        return

    hass.services.async_register(
        DOMAIN, SERVICE_ADOPT_PLAYER, _async_adopt, schema=_ADOPT_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RELEASE_PLAYER, _async_release, schema=_TARGET_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RECLAIM_PLAYER, _async_reclaim, schema=_TARGET_SCHEMA
    )


def _loaded_entry(hass: HomeAssistant) -> ConfigEntry:
    """The Sendspin hub, or an error the user can act on.

    There is only ever one, and it must be loaded: every verb here needs the
    running server on `runtime_data`.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if getattr(entry, "runtime_data", None) is not None:
            return entry
    raise ServiceValidationError(
        translation_domain=DOMAIN, translation_key="not_loaded"
    )


def _frozen_urls_for_devices(hass: HomeAssistant, device_ids: list[str]) -> list[str]:
    """Resolve targeted devices to the endpoints they represent."""
    registry = dr.async_get(hass)
    urls: list[str] = []
    for device_id in device_ids:
        device = registry.async_get(device_id)
        if device is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_device",
                translation_placeholders={"device_id": device_id},
            )
        for domain, identifier in device.identifiers:
            if domain == DOMAIN:
                urls.append(identifier)
                break
    if not urls:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_sendspin_device"
        )
    return urls


async def _async_adopt(call: ServiceCall) -> None:
    """Start holding a speaker at a given listener URL."""
    hass = call.hass
    entry = _loaded_entry(hass)
    try:
        frozen_url = normalise_listener_url(call.data[ATTR_LISTENER_URL])
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_url",
            translation_placeholders={"url": call.data[ATTR_LISTENER_URL]},
        ) from err

    already = any(
        subentry.subentry_type == SUBENTRY_TYPE_PLAYER
        and subentry.data.get(CONF_LISTENER_URL) == frozen_url
        for subentry in entry.subentries.values()
    )
    if already:
        _LOGGER.debug("Sendspin endpoint %s is already adopted", frozen_url)
        return

    # Route through the same subentry machinery the UI uses, so an automation
    # and a click produce identical state — including the device and entity.
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType({CONF_LISTENER_URL: frozen_url}),
            subentry_type=SUBENTRY_TYPE_PLAYER,
            title=entry.runtime_data.memo.display_name(frozen_url),
            unique_id=frozen_url,
        ),
    )


async def _async_release(call: ServiceCall) -> None:
    """Stop holding a speaker, leaving it free for another server."""
    hass = call.hass
    entry = _loaded_entry(hass)
    host = entry.runtime_data.host
    memo = entry.runtime_data.memo

    for frozen_url in _frozen_urls_for_devices(hass, call.data["device_id"]):
        await host.async_release(memo.dial_url(frozen_url))


async def _async_reclaim(call: ServiceCall) -> None:
    """Take a speaker back from whatever currently holds it.

    The deliberate escalation from a yielded adoption. Adoption gives up when
    another server claims the speaker rather than starting a tug-of-war; this
    clears that and dials again. Both assert the same playback claim — what the
    user is overriding is the decision to stop competing, not the strength of
    the claim.
    """
    hass = call.hass
    entry = _loaded_entry(hass)
    runtime = entry.runtime_data
    timeout = call.data[ATTR_TIMEOUT]

    for frozen_url in _frozen_urls_for_devices(hass, call.data["device_id"]):
        dial_url = runtime.memo.dial_url(frozen_url)
        client_id = runtime.host.client_id_for_url(dial_url) or runtime.memo.client_id(
            frozen_url
        )
        if client_id is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="never_seen",
                translation_placeholders={"url": frozen_url},
            )
        if not await runtime.host.async_reclaim(client_id, timeout_s=timeout):
            _LOGGER.warning("Sendspin could not reclaim %s", frozen_url)
