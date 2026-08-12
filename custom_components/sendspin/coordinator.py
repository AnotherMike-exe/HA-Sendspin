"""State coordination for Sendspin.

Push-based. The source of truth is the **in-process server's event stream**, not
a controller websocket — a controller-role client cannot see a server's clients
or groups at all. So this is a `DataUpdateCoordinator` used *without* a poll
interval: `async_set_updated_data` is called from the event callbacks.

Two invariants matter more than anything else here:

1. **The endpoint set is the adopted set, never the connected set.** A speaker
   that drops off the network must go `unavailable`, not disappear. Pruning on
   a dropped connection would make "the network hiccupped" indistinguishable
   from "the user removed this speaker", and would churn the entity registry.

2. **Unknown is not zero.** A player that never reports its volume surfaces
   `None`, which the entity turns into *no attribute*. Substituting a default
   makes every speaker read 100% while the audio is quieter.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from aiosendspin.server.server import (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
    SendspinEvent,
    SendspinServer,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .mesh import MESH_POLL_INTERVAL_S, MeshClient, MeshView
from .models import EndpointSnapshot, SendspinData
from .server_host import ServerHost, player_role

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .player_memo import PlayerMemo

_LOGGER = logging.getLogger(__name__)

# A single re-route emits a burst of member-added/removed/group-changed events.
# Coalesce them so entities are written once, not five times.
_EVENT_DEBOUNCE_S = 0.2

_INTERESTING_EVENTS = (
    ClientAddedEvent,
    ClientConnectedEvent,
    ClientDisconnectedEvent,
    ClientRemovedEvent,
    ClientUpdatedEvent,
)


class SendspinCoordinator(DataUpdateCoordinator[SendspinData]):
    """Fan the in-process server's event stream out to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        host: ServerHost,
        memo: PlayerMemo,
    ) -> None:
        """Initialise the coordinator.

        No `update_interval` is passed on purpose — state arrives by push.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
            config_entry=config_entry,
        )
        self.host = host
        self.memo = memo
        self.data = SendspinData()
        # Nothing has been observed yet, so entities must not claim to be
        # available. With no poll interval nothing else would ever set this.
        self.last_update_success = False

        self._endpoints: set[str] = set()
        self._unsubscribe: callable | None = None
        self._cancel_mesh_poll: callable | None = None
        self._mesh = MeshClient(async_get_clientsession(hass))
        self._mesh_view = MeshView()
        self._mesh_hosts: list[str] = []
        self._debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=_EVENT_DEBOUNCE_S,
            immediate=True,
            function=self._async_publish,
        )

    @callback
    def async_start(self) -> None:
        """Begin listening to the server and polling the mesh."""
        self._unsubscribe = self.host.server.add_event_listener(self._on_server_event)
        self._cancel_mesh_poll = async_track_time_interval(
            self.hass,
            self._async_poll_mesh,
            timedelta(seconds=MESH_POLL_INTERVAL_S),
            name="sendspin-mesh-poll",
        )

    @callback
    def async_note_mesh_host(self, host: str) -> None:
        """Remember a unit worth asking for the mesh view.

        Any one unit's view describes the whole mesh, so a single reachable
        host bootstraps everything.
        """
        if host and host not in self._mesh_hosts:
            self._mesh_hosts.append(host)

    @property
    def mesh(self) -> MeshClient:
        """The mesh client, for entities that need to write."""
        return self._mesh

    @property
    def mesh_view(self) -> MeshView:
        """The last mesh view fetched."""
        return self._mesh_view

    async def async_refresh_mesh(self) -> None:
        """Fetch the mesh now, rather than waiting for the next poll.

        Used straight after a routing call so the dropdown reflects reality
        without a five second lag.
        """
        await self._async_poll_mesh(None)

    async def _async_poll_mesh(self, _now: object) -> None:
        """Poll the Plum mesh, if there is one.

        An unreachable mesh yields no sources but must not mark anything
        unavailable: speakers are adopted and controlled through our own
        server, which knows nothing about this API.
        """
        hosts = [*self._mesh_hosts]
        # Units discovered through the view itself are just as good to ask.
        hosts += [s.unit_host for s in self._mesh_view.sources if s.unit_host]
        if not hosts:
            return
        view = await self._mesh.async_fetch_view(list(dict.fromkeys(hosts)))
        if not view.reachable:
            return
        if view.sources != self._mesh_view.sources:
            self._mesh_view = view
            self.async_request_publish()

    @callback
    def async_stop(self) -> None:
        """Stop listening and cancel any pending publish.

        The debouncer holds a timer, so it has to be shut down explicitly or a
        reload leaves it armed against a coordinator that is going away.
        """
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._cancel_mesh_poll is not None:
            self._cancel_mesh_poll()
            self._cancel_mesh_poll = None
        self._debouncer.async_shutdown()

    @callback
    def async_track_endpoint(self, frozen_url: str) -> None:
        """Start reporting an adopted endpoint."""
        self._endpoints.add(frozen_url)
        self.async_request_publish()

    @callback
    def async_forget_endpoint(self, frozen_url: str) -> None:
        """Stop reporting an endpoint, on un-adoption.

        The only path by which an endpoint leaves the data set. Disconnection
        never does.
        """
        self._endpoints.discard(frozen_url)
        self.async_request_publish()

    @property
    def tracked_endpoints(self) -> frozenset[str]:
        """Frozen URLs currently reported."""
        return frozenset(self._endpoints)

    @callback
    def async_request_publish(self) -> None:
        """Rebuild and publish, coalescing bursts."""
        self.hass.async_create_task(
            self._debouncer.async_call(), "sendspin-coordinator-publish"
        )

    async def _async_update_data(self) -> SendspinData:
        """Return current state.

        Never called on a timer — there is no update interval — but Home
        Assistant may request a refresh, and answering from live state is
        cheaper and more correct than refusing.
        """
        return self._build_snapshot()

    async def _async_publish(self) -> None:
        """Push a freshly built snapshot at the entities."""
        self.async_set_updated_data(self._build_snapshot())

    @callback
    def _on_server_event(self, _server: SendspinServer, event: SendspinEvent) -> None:
        """Handle a server event. Called synchronously on the event loop."""
        if not isinstance(event, _INTERESTING_EVENTS):
            return
        if isinstance(event, ClientConnectedEvent | ClientUpdatedEvent):
            self._learn_names(event.client_id)
        self.async_request_publish()

    @callback
    def _learn_names(self, client_id: str) -> None:
        """Capture the handshake name while the speaker is attached.

        This is the *only* moment it is visible. Once the speaker detaches, all
        that remains is its mDNS instance name, which for third-party devices
        is something like `home-assistant-voice-a1b2c3`.
        """
        client = self.host.server.get_client(client_id)
        if client is None:
            return
        dial_url = self.host.server.get_client_url(client_id)
        if dial_url is None:
            return
        frozen_url = self._frozen_url_for_dial(dial_url)
        if frozen_url is None:
            return
        before = self.memo.display_name(frozen_url)
        self.memo.remember_handshake(frozen_url, name=client.name, client_id=client_id)
        self.memo.async_schedule_save()

        after = self.memo.display_name(frozen_url)
        if after != before:
            self._rename_device(frozen_url, after)

    @callback
    def _rename_device(self, frozen_url: str, name: str) -> None:
        """Apply a better name to the device once we learn one.

        A speaker's good name is only visible while it is attached, which is
        after its entity has already been created. Without this the device
        keeps whatever it was called at adoption — usually its address.

        This sets the device's *original* name; any name the user has chosen
        takes precedence in the UI and is untouched.
        """
        registry = dr.async_get(self.hass)
        device = registry.async_get_device(identifiers={(DOMAIN, frozen_url)})
        if device is not None and device.name != name:
            registry.async_update_device(device.id, name=name)

    def _frozen_url_for_dial(self, dial_url: str) -> str | None:
        """Resolve a live dial URL back to the endpoint's frozen identity."""
        for frozen_url in self._endpoints:
            if self.memo.dial_url(frozen_url) == dial_url:
                return frozen_url
        return None

    def _build_snapshot(self) -> SendspinData:
        """Assemble what the entities render."""
        yielded_by_dial = self.host.yielded_urls
        endpoints: dict[str, EndpointSnapshot] = {}

        for frozen_url in self._endpoints:
            dial_url = self.memo.dial_url(frozen_url)
            client_id = self.host.client_id_for_url(dial_url) or self.memo.client_id(
                frozen_url
            )
            client = (
                self.host.server.get_client(client_id)
                if client_id is not None
                else None
            )
            connected = bool(client is not None and client.is_connected)

            volume: int | None = None
            muted: bool | None = None
            if connected and (role := player_role(client)) is not None:
                volume = role.get_player_volume()
                muted = role.get_player_muted()

            assigned = self._mesh_view.source_for_player(client_id)

            endpoints[frozen_url] = EndpointSnapshot(
                frozen_url=frozen_url,
                dial_url=dial_url,
                name=self.memo.display_name(frozen_url),
                client_id=client_id,
                connected=connected,
                yielded_reason=yielded_by_dial.get(dial_url),
                volume=volume,
                muted=muted,
                source_label=assigned.label if assigned is not None else None,
                source_streaming=assigned.streaming if assigned is not None else False,
                routed_away=self.memo.routed_away(frozen_url),
            )

        return SendspinData(
            endpoints=endpoints, sources=tuple(self._mesh_view.sorted_sources)
        )
