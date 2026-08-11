"""Base entity for Sendspin endpoints."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SendspinCoordinator
from .models import EndpointSnapshot


class SendspinEndpointEntity(CoordinatorEntity[SendspinCoordinator]):
    """An entity backed by one adopted Sendspin endpoint."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SendspinCoordinator, frozen_url: str) -> None:
        """Bind the entity to an endpoint's frozen identity.

        `frozen_url` is the listener URL as first seen and is never recomputed.
        The live dial URL moves with DHCP; using it as identity would orphan
        the entity on a lease change and lose the user's name, area and icon.
        """
        super().__init__(coordinator)
        self._frozen_url = frozen_url
        self._attr_unique_id = f"player:{frozen_url}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, frozen_url)},
            name=coordinator.memo.display_name(frozen_url),
        )

    @property
    def endpoint(self) -> EndpointSnapshot | None:
        """This endpoint's latest snapshot, if it is still adopted."""
        return self.coordinator.data.endpoints.get(self._frozen_url)

    @property
    def available(self) -> bool:
        """Whether we know anything trustworthy about this endpoint.

        `CoordinatorEntity.available` is only `last_update_success` — a whole-
        coordinator health flag that knows nothing per-item. Without this
        override a speaker that had dropped off would keep reporting its last
        state, and every property access would raise inside a state write.

        A **yielded** speaker still counts as available. Another server holding
        it is not a fault: the speaker is present and very likely playing. And
        Home Assistant drops attributes on unavailable entities, so marking it
        unavailable would hide the `yielded_to` explanation at exactly the
        moment it is worth reading. Unavailable is reserved for "we cannot see
        this speaker at all".
        """
        endpoint = self.endpoint
        if endpoint is None:
            return False
        return super().available and (
            endpoint.connected or endpoint.yielded_reason is not None
        )
