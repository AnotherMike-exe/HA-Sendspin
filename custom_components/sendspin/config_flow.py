"""Config flow for the Sendspin integration.

Two entry paths:
  - `async_step_user`     — manual entry of a server listener URL.
  - `async_step_zeroconf` — triggered by HA core when a Sendspin server is seen
                            on the LAN, per the `zeroconf` key in manifest.json.

Scaffold status: structure only. No steps are implemented.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN


class SendspinConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sendspin."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a manually initiated flow.

        TODO: prompt for the listener URL, probe it with aiosendspin, and set
        the unique id to the listener URL (see docs/OPEN-QUESTIONS.md §2 for
        why the client id is unsuitable as a unique id).
        """
        raise NotImplementedError

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via zeroconf.

        TODO:
          - Build the listener URL from discovery_info host/port/properties.
          - `await self.async_set_unique_id(listener_url)` then
            `self._abort_if_unique_id_configured(updates=...)` so a server that
            changes IP updates the existing entry instead of duplicating it.
          - Decide which name to surface: the mDNS instance name or the
            handshake name. These differ. See docs/OPEN-QUESTIONS.md §2.
        """
        raise NotImplementedError
