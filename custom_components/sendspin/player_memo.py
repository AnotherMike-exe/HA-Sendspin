"""What we know about each Sendspin endpoint, keyed on its frozen listener URL.

A Sendspin speaker presents **two identities that share exactly one field**:

- **Attached**, it reports a handshake name (`FutureProofHomes - Satellite1`)
  and a client id which is typically a MAC (`98:A3:16:D0:9E:E8`).
- **Idle**, all that exists is its mDNS instance name — and third-party devices
  routinely publish no `name` TXT, so that reads `home-assistant-voice-a1b2c3`.

One device therefore looks like two, and appears to rename itself every time it
joins or leaves. The **listener URL is the only identifier both views share**,
which is why it is the entity identity. Both halves were observed directly on
hardware; see docs/OPEN-QUESTIONS.md §7.

This module is the single place a display name is derived. Nothing else may
re-derive one, or the two views will disagree somewhere in the UI.

Two URLs, deliberately distinct:

- The **frozen URL** is the identity, recorded as first seen and never
  recomputed. It is what the entity's unique id is built from.
- The **dial URL** is where the speaker answers *now*, and moves with DHCP.

Keeping them apart is what lets a speaker change address without orphaning its
entity and losing the user's name, area and customisation.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MEMO_STORAGE_KEY = f"{DOMAIN}.players"

_KEY_HANDSHAKE_NAME = "handshake_name"
_KEY_INSTANCE_NAME = "mdns_instance_name"
_KEY_TXT_NAME = "mdns_txt_name"
_KEY_CLIENT_ID = "client_id"
_KEY_DIAL_URL = "dial_url"
_KEY_ROUTED_AWAY = "routed_away"
_KEY_HANDED_TO_SERVER = "handed_to_server"


def _clean(value: str | None) -> str | None:
    """Treat a blank or whitespace-only name as no name at all."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class PlayerMemo:
    """Persistent per-endpoint knowledge, keyed on the frozen listener URL."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the memo. Call `async_load` before using it."""
        self._store = Store[dict[str, dict[str, Any]]](
            hass, STORAGE_VERSION, MEMO_STORAGE_KEY
        )
        self._data: dict[str, dict[str, Any]] = {}

    async def async_load(self) -> None:
        """Read the memo from disk."""
        self._data = await self._store.async_load() or {}

    async def async_save(self) -> None:
        """Write the memo out."""
        await self._store.async_save(self._data)

    def async_schedule_save(self) -> None:
        """Write the memo out shortly, coalescing bursts.

        Names are learned during connection storms, so a delayed save keeps a
        reroute from producing a write per event.
        """
        self._store.async_delay_save(lambda: self._data, 10)

    # --- Recording ---------------------------------------------------------

    def remember_discovery(
        self,
        frozen_url: str,
        *,
        instance_name: str | None = None,
        txt_name: str | None = None,
    ) -> None:
        """Record what an mDNS record told us."""
        entry = self._data.setdefault(frozen_url, {})
        if (name := _clean(instance_name)) is not None:
            entry[_KEY_INSTANCE_NAME] = name
        if (name := _clean(txt_name)) is not None:
            entry[_KEY_TXT_NAME] = name

    def remember_handshake(
        self, frozen_url: str, *, name: str | None, client_id: str | None
    ) -> None:
        """Record what the device told us while attached.

        Worth calling even for an adoption that did not stick: on hardware the
        handshake name was learned from a connection that was taken away again
        one millisecond later.
        """
        entry = self._data.setdefault(frozen_url, {})
        if (cleaned := _clean(name)) is not None:
            entry[_KEY_HANDSHAKE_NAME] = cleaned
        if (cleaned := _clean(client_id)) is not None:
            entry[_KEY_CLIENT_ID] = cleaned

    def repair_dial_urls(self) -> list[str]:
        """Undo a dial URL that points at a different address family.

        A speaker advertising over both IPv4 and IPv6 was once treated as
        having moved, leaving it dialled at an address the mesh never reports —
        so every lookup keyed on it missed and the speaker read as unavailable.
        Refusing new such relocations does not undo one already saved, so this
        resets them on load.

        Returns the endpoints that were repaired.
        """
        repaired: list[str] = []
        for frozen_url, entry in self._data.items():
            dial_url = entry.get(_KEY_DIAL_URL)
            if dial_url and ("[" in str(dial_url)) != ("[" in frozen_url):
                entry.pop(_KEY_DIAL_URL, None)
                repaired.append(frozen_url)
                _LOGGER.info(
                    "Reset the address for Sendspin endpoint %s: it had been "
                    "moved to %s, which is the same speaker on another network "
                    "family",
                    frozen_url,
                    dial_url,
                )
        return repaired

    def remember_dial_url(self, frozen_url: str, dial_url: str) -> None:
        """Record where the speaker answers now, leaving its identity alone."""
        if dial_url == frozen_url:
            self._data.setdefault(frozen_url, {}).pop(_KEY_DIAL_URL, None)
            return
        _LOGGER.debug("Sendspin endpoint %s now answers at %s", frozen_url, dial_url)
        self._data.setdefault(frozen_url, {})[_KEY_DIAL_URL] = dial_url

    def set_routed_away(self, frozen_url: str, routed_away: bool) -> None:
        """Record that the user handed this speaker to another unit.

        Without this, restarting Home Assistant re-dials every adopted speaker
        and takes them straight back off the streams the user put them on.
        """
        self._data.setdefault(frozen_url, {})[_KEY_ROUTED_AWAY] = routed_away

    def routed_away(self, frozen_url: str) -> bool:
        """Whether this speaker was deliberately handed to another unit."""
        return bool(self._data.get(frozen_url, {}).get(_KEY_ROUTED_AWAY, False))

    def set_handed_to_server(self, frozen_url: str, server: str | None) -> None:
        """Record that the user handed this speaker to a whole server.

        Distinct from a hand-off to a *stream*, and the distinction decides
        whether the stranded-speaker rescue may take the speaker back. A stream
        hand-off is verifiable — the mesh either shows the speaker on that source
        or it does not — so a failure can be detected and undone. Handing a
        speaker to Music Assistant is not: MA publishes no mesh API, so the
        speaker is on no source permanently and by design, and a rescue that
        reads that as failure re-adopts it every few seconds forever.
        """
        entry = self._data.setdefault(frozen_url, {})
        if server is None:
            entry.pop(_KEY_HANDED_TO_SERVER, None)
        else:
            entry[_KEY_HANDED_TO_SERVER] = server

    def handed_to_server(self, frozen_url: str) -> str | None:
        """The server the user handed this speaker to, if they did."""
        value = self._data.get(frozen_url, {}).get(_KEY_HANDED_TO_SERVER)
        return str(value) if value is not None else None

    # --- Reading -----------------------------------------------------------

    def display_name(self, frozen_url: str) -> str:
        """The best name we have, in descending order of trustworthiness.

        The handshake name is **never demoted**: once learned, a later
        mDNS-only sighting must not overwrite it, or the speaker's name flips
        every time it disconnects — precisely when the good name stops being
        visible. A later *handshake* may still update it, so a genuine rename
        still lands.
        """
        entry = self._data.get(frozen_url, {})
        for key in (_KEY_HANDSHAKE_NAME, _KEY_TXT_NAME, _KEY_INSTANCE_NAME):
            if (name := entry.get(key)) is not None:
                return str(name)
        return self._host_and_port(frozen_url)

    def dial_url(self, frozen_url: str) -> str:
        """Where to dial this endpoint now."""
        return str(self._data.get(frozen_url, {}).get(_KEY_DIAL_URL, frozen_url))

    def client_id(self, frozen_url: str) -> str | None:
        """The last client id seen, needed to reclaim a speaker."""
        value = self._data.get(frozen_url, {}).get(_KEY_CLIENT_ID)
        return str(value) if value is not None else None

    def frozen_url_for_instance(self, instance_name: str) -> str | None:
        """Find an endpoint by mDNS instance name.

        The secondary matcher that lets a speaker which moved address be
        recognised as the same device, so its dial URL is updated instead of a
        second entity being created.
        """
        for frozen_url, entry in self._data.items():
            if entry.get(_KEY_INSTANCE_NAME) == instance_name:
                return frozen_url
        return None

    @staticmethod
    def _host_and_port(url: str) -> str:
        """Last-resort name: what the user would type to reach it."""
        parts = urlsplit(url)
        if parts.hostname is None:
            return url
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        return f"{host}:{parts.port}" if parts.port else host
