"""Diagnostics for the Sendspin integration.

Most of what goes wrong here is only visible against real hardware — a speaker
another server keeps taking back, a mesh that is unreachable, a player that
never reports its volume. This dump is meant to answer those questions without
a debugging session.

Nothing secret is included. The X25519 private key and the pairing records live
in `.storage` and are deliberately not read here; only the **public** server id
appears, which is what peers see anyway.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import SendspinConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SendspinConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Sendspin hub."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    mesh = coordinator.mesh_view

    return {
        "server": {
            # Public key, not the private one. This is the `server_id` every
            # speaker on the network already sees.
            "server_id": runtime.host.server_id,
            "name": entry.title,
            # Both must be false for the whole design to hold: this server
            # renders no audio and puts nothing on mDNS.
            "advertises_mdns": False,
            "originates_audio": False,
        },
        "endpoints": [
            {
                # Identity, as first seen — never recomputed.
                "frozen_url": snapshot.frozen_url,
                # Where it answers now; differs after a DHCP change.
                "dial_url": snapshot.dial_url,
                "name": snapshot.name,
                "client_id": snapshot.client_id,
                "connected": snapshot.connected,
                # Set when another server holds it and we stopped competing.
                "yielded_reason": snapshot.yielded_reason,
                # None means the speaker never reported one, which is normal
                # and is not the same as zero.
                "volume": snapshot.volume,
                "muted": snapshot.muted,
                "source": snapshot.source_label,
                # Which group the mesh puts it in, and who holds it. Together
                # these are how the assigned source is resolved, so a wrong
                # `source` is diagnosed here.
                "held_by_server": snapshot.held_by_server,
                "held_by_us": snapshot.held_by_us,
                "media_title": snapshot.media_title,
                "media_artist": snapshot.media_artist,
                "media_playing": snapshot.media_playing,
                "media_link": snapshot.media_link,
            }
            for snapshot in data.endpoints.values()
        ],
        "mesh": {
            # False simply means no Plum-Audio unit answered. The integration
            # works without one; there are just no streams to list.
            "reachable": mesh.reachable,
            # The dropdown shows only live sources, so which ones qualified is
            # the first thing to check when a stream is missing from it.
            "live_sources": [source.label for source in data.live_sources],
            "sources": [
                {
                    "key": source.key,
                    "label": source.label,
                    "unit_host": source.unit_host,
                    "active": source.active,
                    "streaming": source.streaming,
                    "group_id": source.group_id,
                    "player_ids": list(source.player_ids),
                    "supports_source_volume": source.supports_source_volume,
                }
                for source in mesh.sources
            ],
        },
        "links": [
            {
                "key": key,
                "url": link.url,
                "connected": link.snapshot.connected,
                "server": link.snapshot.server_name,
                "server_id": link.snapshot.server_id,
                "group_id": link.snapshot.group_id,
                "playback_state": link.snapshot.playback_state,
                "title": link.snapshot.title,
                "artwork_bytes": len(link.snapshot.artwork or b""),
                "commands": list(link.snapshot.supported_commands),
            }
            for key, link in coordinator.links.items()
        ],
        "adoption": {
            "dialing": sorted(runtime.host.adopted_urls),
            "yielded": dict(runtime.host.yielded_urls),
        },
    }
