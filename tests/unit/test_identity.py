"""Tests for the persisted Sendspin server identity.

The X25519 public key *is* the `server_id` peers see, so losing it makes Home
Assistant look like a different server to every speaker it has paired with.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.sendspin.identity import (
    IDENTITY_STORAGE_KEY,
    async_load_identity,
)


async def test_identity_is_generated_and_persisted(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """First run mints an identity and writes it out immediately."""
    identity = await async_load_identity(hass)

    stored = hass_storage[IDENTITY_STORAGE_KEY]["data"]
    assert stored["peer_id"] == identity.peer_id
    assert stored["private_key"] == identity.private_b64u


async def test_identity_survives_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """A second load returns the same identity, not a new one.

    This is the whole reason the key is persisted: regenerating it would
    invalidate every speaker's pairing record.
    """
    first = await async_load_identity(hass)
    second = await async_load_identity(hass)

    assert second.peer_id == first.peer_id


async def test_a_corrupt_stored_key_is_replaced_rather_than_fatal(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """An unreadable key must not block setup.

    Regenerating costs the pairings, which can be redone; raising would leave
    the integration permanently unable to start.
    """
    hass_storage[IDENTITY_STORAGE_KEY] = {
        "version": 1,
        "data": {"private_key": "this is not a key", "peer_id": "stale"},
    }

    identity = await async_load_identity(hass)

    assert identity.peer_id != "stale"
    assert hass_storage[IDENTITY_STORAGE_KEY]["data"]["peer_id"] == identity.peer_id
