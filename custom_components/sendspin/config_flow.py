"""Config flow for the Sendspin integration.

A config entry represents **Home Assistant's own Sendspin server**, not a
remote one. There is exactly one, because there is one identity and one set of
adopted speakers; discovered players and attached servers become subentries of
it in M2 rather than entries of their own.

M1 scope: the manual step that brings the hub into existence. The zeroconf
branches for `_sendspin-server._tcp` and `_sendspin._tcp` land in M2, together
with the adoption consent step — a discovered speaker must never be adopted
automatically, because dialling one takes it from whatever currently holds it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN

# The hub is a singleton, so its unique id is a constant rather than derived
# from anything on the network.
HUB_UNIQUE_ID = "hub"


class SendspinConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sendspin."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the Sendspin hub.

        The only thing to ask for is the name speakers will see this server
        advertise itself as during the handshake. It defaults to the Home
        Assistant instance name, which is almost always what a user wants.
        """
        # Enforced here rather than via the manifest's `single_config_entry`,
        # which would also auto-abort zeroconf discovery flows — and M2 needs
        # those to reach the adoption step.
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
