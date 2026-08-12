"""Config flow for the Sendspin integration.

A config entry represents **Home Assistant's own Sendspin server**, not a remote
one. There is exactly one, because there is one identity and one set of adopted
speakers. Individual speakers are **subentries** of it, so each gets a device, a
native rename and a native delete.

Discovery cannot create those subentries directly: `ConfigSubentryFlow` has no
discovery source — subentry flows are only ever started explicitly from the UI.
So zeroconf feeds a cache, and the user adopts from it via the hub's "Add
device" button. That indirection is not just a workaround: **a discovered
speaker must never be adopted automatically**, because dialling one takes it
from whatever currently holds it, and on hardware that produced a tug-of-war
with Music Assistant (docs/OPEN-QUESTIONS.md §7).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import voluptuous as vol

from .const import CONF_LISTENER_URL, DOMAIN, SUBENTRY_TYPE_PLAYER
from .discovery import SendspinDiscovery, normalise_listener_url, parse_zeroconf

_LOGGER = logging.getLogger(__name__)

# The hub is a singleton, so its unique id is a constant rather than derived
# from anything on the network.
HUB_UNIQUE_ID = "hub"

_DISCOVERED = "discovered"
_MESH_HOSTS = "mesh_hosts"


@callback
def async_discovered_players(hass: HomeAssistant) -> dict[str, SendspinDiscovery]:
    """Sendspin players seen on the LAN, keyed on frozen listener URL."""
    return hass.data.setdefault(DOMAIN, {}).setdefault(_DISCOVERED, {})


@callback
def async_remember_discovery(hass: HomeAssistant, found: SendspinDiscovery) -> None:
    """Cache a discovery so it can be offered for adoption later."""
    async_discovered_players(hass)[found.listener_url] = found


@callback
def async_mesh_hosts(hass: HomeAssistant) -> set[str]:
    """Hosts of discovered Sendspin servers, worth asking for a mesh view.

    A Plum-Audio unit advertises its Sendspin server over mDNS but does NOT
    advertise its HTTP mesh API, so the address has to be taken from the
    Sendspin record and the port assumed.
    """
    return hass.data.setdefault(DOMAIN, {}).setdefault(_MESH_HOSTS, set())


class SendspinConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sendspin."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the flow."""
        self._discovery: SendspinDiscovery | None = None

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Speakers are adopted as subentries of the hub."""
        return {SUBENTRY_TYPE_PLAYER: PlayerSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Sendspin hub.

        The only thing to ask for is the name speakers will see this server
        advertise itself as during the handshake. It defaults to the Home
        Assistant instance name, which is almost always what a user wants.
        """
        # Enforced here rather than via the manifest's `single_config_entry`,
        # which would also auto-abort zeroconf discovery flows — and those need
        # to reach the cache below.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            await self.async_set_unique_id(HUB_UNIQUE_ID)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME], data={CONF_NAME: user_input[CONF_NAME]}
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=self.hass.config.location_name
                    ): cv.string
                }
            ),
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle a Sendspin server or player appearing on the network.

        Both service types arrive here — servers advertise
        `_sendspin-server._tcp` and players `_sendspin._tcp` — so the record is
        parsed before anything is decided.
        """
        try:
            found = parse_zeroconf(discovery_info)
        except ValueError as err:
            _LOGGER.debug("Ignoring unusable Sendspin record: %s", err)
            return self.async_abort(reason="not_sendspin")

        if found.kind == "player":
            async_remember_discovery(self.hass, found)
            self._async_relocate_if_known(found)
        else:
            async_mesh_hosts(self.hass).add(found.host)
            for entry in self._async_current_entries():
                if (runtime := getattr(entry, "runtime_data", None)) is not None:
                    runtime.coordinator.async_note_mesh_host(found.host)

        if self._async_current_entries():
            # The hub exists; the user adopts from the cache via "Add device".
            # Nothing here may dial the speaker on its own.
            return self.async_abort(reason="single_instance_allowed")

        self._discovery = found
        self.context["title_placeholders"] = {
            "name": found.txt_name or found.instance_name
        }
        return await self.async_step_zeroconf_confirm()

    @callback
    def _async_relocate_if_known(self, found: SendspinDiscovery) -> None:
        """Follow an already-adopted speaker to a new address.

        A speaker's identity is its listener URL as first seen, so a new DHCP
        lease produces a record that looks like a brand new device. The mDNS
        instance name is the secondary matcher that ties the two together: it
        survives an address change, and survives a rename too, because a rename
        only alters the TXT `name`.
        """
        for entry in self._async_current_entries():
            runtime = getattr(entry, "runtime_data", None)
            if runtime is None:
                continue
            memo = runtime.memo
            frozen_url = memo.frozen_url_for_instance(found.instance_name)
            if frozen_url is None or frozen_url == found.listener_url:
                continue
            previous = memo.dial_url(frozen_url)
            if previous == found.listener_url:
                continue
            _LOGGER.info(
                "Sendspin endpoint %s has moved to %s", frozen_url, found.listener_url
            )
            memo.remember_dial_url(frozen_url, found.listener_url)
            memo.async_schedule_save()
            runtime.coordinator.async_relocate_endpoint(frozen_url, previous)

    async def async_step_zeroconf_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer to set Sendspin up, having seen something on the network.

        This creates the **hub only**. It never adopts the speaker that
        triggered it.
        """
        assert self._discovery is not None

        if user_input is not None:
            await self.async_set_unique_id(HUB_UNIQUE_ID)
            self._abort_if_unique_id_configured()
            name = user_input[CONF_NAME]
            return self.async_create_entry(title=name, data={CONF_NAME: name})

        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME, default=self.hass.config.location_name
                    ): cv.string
                }
            ),
            description_placeholders={
                "name": self._discovery.txt_name or self._discovery.instance_name
            },
        )


class PlayerSubentryFlow(ConfigSubentryFlow):
    """Adopt a Sendspin speaker."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Choose a discovered speaker, or type an address for one."""
        entry = self._get_entry()
        adopted = {
            subentry.data[CONF_LISTENER_URL]
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_PLAYER
        }
        available = {
            url: found
            for url, found in async_discovered_players(self.hass).items()
            if url not in adopted
        }

        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get(CONF_LISTENER_URL, "").strip()
            try:
                frozen_url = normalise_listener_url(raw)
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                if frozen_url in adopted:
                    return self.async_abort(reason="already_configured")
                found = available.get(frozen_url)
                name = (
                    found.txt_name or found.instance_name
                    if found is not None
                    else frozen_url
                )
                return self.async_create_entry(
                    title=name, data={CONF_LISTENER_URL: frozen_url}
                )

        # A dropdown of what was actually found, rather than a URL to type.
        # `custom_value` keeps the manual path open for a speaker mDNS never
        # showed — which is the whole reason the field accepts a URL at all.
        options = [
            SelectOptionDict(
                value=url,
                label=f"{found.txt_name or found.instance_name} ({found.host})",
            )
            for url, found in sorted(
                available.items(),
                key=lambda kv: (kv[1].txt_name or kv[1].instance_name).lower(),
            )
        ]
        selector: Any = (
            SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    custom_value=True,
                    mode=SelectSelectorMode.DROPDOWN,
                    sort=False,
                )
            )
            if options
            else cv.string
        )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_LISTENER_URL,
                        default=next(iter(available), vol.UNDEFINED),
                    ): selector
                }
            ),
            errors=errors,
            description_placeholders={
                "discovered": str(len(options)) if options else "no"
            },
        )
