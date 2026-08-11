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
            }
            for snapshot in data.endpoints.values()
        ],
        "mesh": {
            # False simply means no Plum-Audio unit answered. The integration
            # works without one; there are just no streams to list.
            "reachable": mesh.reachable,
            "sources": [
                {
                    "key": source.key,
                    "label": source.label,
                    "unit_host": source.unit_host,
                    "active": source.active,
                    "streaming": source.streaming,
                    "player_ids": list(source.player_ids),
                    "supports_source_volume": source.supports_source_volume,
                }
                for source in mesh.sources
            ],
        },
        "adoption": {
            "dialing": sorted(runtime.host.adopted_urls),
            "yielded": {
                url: reason.value for url, reason in runtime.host.yielded_urls.items()
            },
        },
    }
