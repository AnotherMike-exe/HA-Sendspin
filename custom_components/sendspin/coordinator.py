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

from .const import DEFAULT_SERVER_PORT as SENDSPIN_SERVER_PORT
from .const import DEFAULT_WEBSOCKET_PATH, DOMAIN
from .legacy_client import LegacyControllerClient
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

# Mesh polls confirming a routed-away speaker is on no stream before we take it
# back. More than one, because the mesh lags a routing call by a poll or two.
_STRAND_CONFIRMATIONS = 3

# And ignore strandedness entirely for this long after a routing call. Plum
# aggregates peer state on a 2s loop and we poll on a 5s one, so a successful
# hand-off looks exactly like a failed one for the first few seconds. Without
# this the rescue can take a speaker straight back off the stream the user just
# put it on.
_ROUTING_GRACE_S = 45.0

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
        self._strand_checks: dict[str, int] = {}
        self._links: dict[str, LegacyControllerClient] = {}
        self._routed_at: dict[str, float] = {}
        self._stopped = False
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
    def _async_sync_links(self) -> None:
        """Observe exactly the sources our speakers are actually on.

        A controller sees only the group it occupies, so reading what is
        playing needs one connection per source. Plum-Audio honours a client id
        of `ctrl:<source_id>:<nonce>` by placing that controller into the named
        source's group, which makes the target deterministic rather than
        whichever source happens to be primary.

        Only sources with one of our speakers on them are observed — a link per
        idle source would be a websocket each, for nothing.
        """
        if self._stopped:
            return

        wanted: dict[str, str] = {}
        for frozen_url in self._endpoints:
            client_id = self.memo.client_id(frozen_url)
            source = self._mesh_view.source_for_player(client_id)
            if source is not None and source.unit_host:
                wanted[source.key] = source.unit_host

        for key in list(self._links):
            if key not in wanted:
                link = self._links.pop(key)
                self.hass.async_create_task(link.async_stop(), "sendspin-link-stop")

        for key, host in wanted.items():
            if key in self._links:
                continue
            source_id = key.split(":", 1)[1]
            link = LegacyControllerClient(
                async_get_clientsession(self.hass),
                f"ws://{host}:{SENDSPIN_SERVER_PORT}{DEFAULT_WEBSOCKET_PATH}",
                client_name=(
                    self.config_entry.title if self.config_entry else "Home Assistant"
                ),
                # The nonce keeps two Home Assistants from colliding on one id.
                client_id=f"ctrl:{source_id}:ha{self.host.server_id[:8]}",
                on_update=lambda _snapshot: self.async_request_publish(),
            )
            self._links[key] = link
            link.async_start()

    def link_for(self, source_key: str | None) -> LegacyControllerClient | None:
        """The controller link observing a given source, if any."""
        return self._links.get(source_key) if source_key else None

    @callback
    def async_relocate_endpoint(self, frozen_url: str, previous: str) -> None:
        """Re-dial an endpoint that has moved to a new address.

        The frozen URL is the identity and never changes; only where we dial
        it does. Without this a speaker that gets a new DHCP lease is dialled
        at its old address forever, goes unavailable, and shows up in the
        adoption picker as if it were a different device.
        """
        if frozen_url not in self._endpoints:
            return
        self.hass.async_create_task(
            self._async_relocate(frozen_url, previous),
            f"sendspin-relocate-{frozen_url}",
        )

    async def _async_relocate(self, frozen_url: str, previous: str) -> None:
        """Stop dialling the old address and start on the new one."""
        if self._stopped or self.memo.routed_away(frozen_url):
            return
        if previous:
            await self.host.async_release(previous)
        await self.host.async_adopt(self.memo.dial_url(frozen_url))
        self.async_request_publish()

    @callback
    def async_note_routing(self, frozen_url: str) -> None:
        """Record that a routing call was just made for this endpoint.

        Starts the grace period during which strandedness is not judged.
        """
        self._routed_at[frozen_url] = self.hass.loop.time()
        self._strand_checks.pop(frozen_url, None)

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
        if self._stopped:
            return
        hosts = [*self._mesh_hosts]
        # Units discovered through the view itself are just as good to ask.
        hosts += [s.unit_host for s in self._mesh_view.sources if s.unit_host]
        if not hosts:
            return
        view = await self._mesh.async_fetch_view(list(dict.fromkeys(hosts)))
        if not view.reachable:
            return
        changed = (
            view.sources != self._mesh_view.sources
            or view.players != self._mesh_view.players
        )
        self._mesh_view = view
        self._async_sync_links()
        if not self._stopped:
            await self._async_rescue_stranded()
        if changed:
            self.async_request_publish()

    async def _async_rescue_stranded(self) -> None:
        """Resume dialling a speaker whose hand-off did not stick.

        Routing a speaker to another unit suppresses our dialling, otherwise a
        restart would take it straight back. But if the mesh then reports the
        speaker on no source at all, the hand-off is over — the stream ended,
        or something else took the speaker — and leaving it suppressed strands
        it: not held by us, not on a stream, and unavailable forever.

        Confirmation is required over several polls, because immediately after
        a routing call the mesh has not caught up yet and would look exactly
        like a failure.
        """
        now = self.hass.loop.time()
        for frozen_url in list(self._endpoints):
            if self._stopped:
                return
            routed_at = self._routed_at.get(frozen_url)
            if routed_at is not None and now - routed_at < _ROUTING_GRACE_S:
                continue  # too soon to tell a working hand-off from a failed one
            if not self.memo.routed_away(frozen_url):
                self._strand_checks.pop(frozen_url, None)
                continue
            client_id = self._client_id_for(frozen_url, self.memo.dial_url(frozen_url))
            if client_id is None:
                # We have never seen this speaker attach, so we cannot tell
                # whether a unit has it. Assuming the worst would take it off
                # a stream it may well be happily playing.
                self._strand_checks.pop(frozen_url, None)
                continue
            if self._mesh_view.source_for_player(client_id) is not None:
                self._strand_checks.pop(frozen_url, None)
                continue

            misses = self._strand_checks.get(frozen_url, 0) + 1
            self._strand_checks[frozen_url] = misses
            if misses < _STRAND_CONFIRMATIONS:
                continue

            _LOGGER.info(
                "Sendspin endpoint %s is on no stream any more; taking it back",
                frozen_url,
            )
            self._strand_checks.pop(frozen_url, None)
            self._routed_at.pop(frozen_url, None)
            self.memo.set_routed_away(frozen_url, False)
            self.memo.async_schedule_save()
            await self.host.async_adopt(self.memo.dial_url(frozen_url))

    @callback
    def async_stop(self) -> None:
        """Stop listening and cancel any pending publish.

        The debouncer holds a timer, so it has to be shut down explicitly or a
        reload leaves it armed against a coordinator that is going away.
        """
        self._stopped = True
        for link in self._links.values():
            self.hass.async_create_task(link.async_stop(), "sendspin-link-stop")
        self._links.clear()
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
        self._strand_checks.pop(frozen_url, None)
        self._routed_at.pop(frozen_url, None)
        self.async_request_publish()

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

    def _media_for(self, source_key: str | None):
        """The now-playing observed on a source, if we are observing it."""
        link = self._links.get(source_key) if source_key else None
        if link is None or not link.snapshot.connected:
            return None
        return link.snapshot

    def _client_id_for(self, frozen_url: str, dial_url: str) -> str | None:
        """Identify a speaker, in descending order of directness.

        The mesh lookup is what makes a speaker we have never held usable at
        all: its client id is a MAC or an opaque id only visible during a
        handshake with *us*, and a speaker another server holds never
        handshakes with us. Without this such a speaker matches no source, so
        it reports no stream, offers no controls, and reads as unavailable —
        despite being present and very likely playing.
        """
        if (client_id := self.host.client_id_for_url(dial_url)) is not None:
            return client_id
        if (known := self._mesh_view.player_by_url(dial_url)) is not None:
            if self.memo.client_id(frozen_url) != known.player_id:
                self.memo.remember_handshake(
                    frozen_url, name=None, client_id=known.player_id
                )
                self.memo.async_schedule_save()
            return known.player_id
        return self.memo.client_id(frozen_url)

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
            client_id = self._client_id_for(frozen_url, dial_url)
            client = (
                self.host.server.get_client(client_id)
                if client_id is not None
                else None
            )
            connected = bool(client is not None and client.is_connected)

            volume: int | None = None
            muted: bool | None = None
            held_by: str | None = None
            mesh_player = self._mesh_view.player_by_url(dial_url)
            if connected and (role := player_role(client)) is not None:
                volume = role.get_player_volume()
                muted = role.get_player_muted()
            elif mesh_player is not None:
                # A unit holds it, and reports what the speaker last told it.
                volume = mesh_player.volume
                muted = mesh_player.muted
                held_by = mesh_player.unit_host

            assigned = self._mesh_view.source_for_player(client_id)

            source_key = assigned.key if assigned is not None else None
            media = self._media_for(source_key)

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
                held_by_unit_host=held_by,
                known_to_mesh=mesh_player is not None,
                media_title=media.title if media else None,
                media_artist=media.artist if media else None,
                media_album=media.album if media else None,
                media_position=media.progress.position_s
                if media and media.progress
                else None,
                media_position_updated_at=(
                    media.progress.updated_at if media and media.progress else None
                ),
                media_duration=media.progress.duration_s
                if media and media.progress
                else None,
                media_commands=media.supported_commands if media else (),
                media_link=source_key if media is not None else None,
                held_by_server=(
                    mesh_player.held_by if mesh_player is not None else None
                ),
                routed_away=self.memo.routed_away(frozen_url),
            )

        return SendspinData(
            endpoints=endpoints, sources=tuple(self._mesh_view.sorted_sources)
        )
