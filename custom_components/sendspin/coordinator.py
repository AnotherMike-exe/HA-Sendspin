"""State coordination for Sendspin.

Push-based: the controller websocket is the source of truth, so this is a
DataUpdateCoordinator used *without* a poll interval — `async_set_updated_data`
is called from the websocket callback.

Server/player reachability maps onto entity availability, which is what lets HA
automations trigger on a stream coming online or a player dropping off the mesh
without a polling script.

Scaffold status: structure only.
"""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class SendspinCoordinator(DataUpdateCoordinator[dict]):
    """Fan out Sendspin controller websocket state to entities."""

    def __init__(self, hass: HomeAssistant, listener_url: str) -> None:
        """Initialise the coordinator.

        No `update_interval` is passed on purpose — state arrives by push.
        """
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.listener_url = listener_url

    async def async_connect(self) -> None:
        """Open the controller websocket and begin streaming state.

        TODO (M2):
          - Fan out the in-process SendspinServer's event stream, not a
            controller websocket — a controller client cannot see the server's
            clients or groups at all.
          - On each event, rebuild an immutable snapshot and call
            `self.async_set_updated_data(...)`, debounced ~0.2s (one reroute
            emits a burst of member-added/removed/group-deleted events).
          - On disconnect, mark unavailable rather than tearing down entities.
            Reconnect is aiosendspin's job (`retry_indefinitely=True`), not
            ours.
        """
        raise NotImplementedError

    async def async_disconnect(self) -> None:
        """Close the websocket and cancel the reconnect task."""
        raise NotImplementedError
