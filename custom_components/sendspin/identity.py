"""Persistent cryptographic identity for Home Assistant's Sendspin server role.

Sendspin 8.0+ identifies a server by an X25519 key pair: the base64url-encoded
public key *is* the `server_id` peers see. It therefore has to survive restarts.
Regenerating it would make Home Assistant look like a brand new server to every
speaker it has ever paired with, invalidating their stored pairing records.

Two things are persisted, deliberately in different places:

- The **private key**, via Home Assistant's own `Store`, so it lands in
  `.storage` with the rest of HA's state and is covered by HA backups.
- The **pairing records**, via aiosendspin's `FileServerPairingStore`. That is
  upstream's own format, it does all its file I/O through `asyncio.to_thread`
  so it never blocks the event loop, and letting upstream own the schema means
  a pairing-format change is their migration rather than ours.

Both are secrets at rest. See docs/ARCHITECTURE.md §7.
"""

from __future__ import annotations

import base64
import logging

from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import FileServerPairingStore, ServerPairingStore
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import STORAGE_DIR, Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
IDENTITY_STORAGE_KEY = f"{DOMAIN}.identity"
PAIRING_STORE_FILENAME = f"{DOMAIN}.pairings.json"

_KEY_PRIVATE = "private_key"
_KEY_PEER_ID = "peer_id"


def _decode_private_key(private_b64u: str) -> bytes:
    """Decode aiosendspin's unpadded base64url private key form."""
    padding = "=" * (-len(private_b64u) % 4)
    return base64.urlsafe_b64decode(private_b64u + padding)


async def async_load_identity(hass: HomeAssistant) -> Identity:
    """Load this Home Assistant's Sendspin identity, generating it once.

    A corrupt or truncated stored key is replaced rather than raising: an
    unusable key would block setup entirely, and regenerating costs only the
    pairings, which can be redone. The event is logged as a warning because it
    *is* the "speakers suddenly need re-pairing" explanation.
    """
    store = Store[dict[str, str]](
        hass, STORAGE_VERSION, IDENTITY_STORAGE_KEY, private=True
    )
    data = await store.async_load()

    if data and (private_b64u := data.get(_KEY_PRIVATE)):
        try:
            return Identity.from_private_bytes(_decode_private_key(private_b64u))
        except ValueError, TypeError:
            _LOGGER.warning(
                "Stored Sendspin identity could not be read and is being "
                "regenerated. Speakers paired with the previous identity will "
                "need to be paired again"
            )

    identity = Identity.generate()
    await store.async_save(
        {_KEY_PRIVATE: identity.private_b64u, _KEY_PEER_ID: identity.peer_id}
    )
    _LOGGER.debug("Generated a new Sendspin server identity: %s", identity.peer_id)
    return identity


async def async_load_pairing_store(hass: HomeAssistant) -> ServerPairingStore:
    """Open the on-disk pairing store, creating it on first use."""
    return await FileServerPairingStore.open(
        hass.config.path(STORAGE_DIR, PAIRING_STORE_FILENAME)
    )
