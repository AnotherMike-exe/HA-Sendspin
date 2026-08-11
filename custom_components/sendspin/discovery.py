"""mDNS record handling for Sendspin.

HA core's `zeroconf` integration does the browsing (declared via the `zeroconf`
key in manifest.json), so this module only parses the records core hands over —
it does not run its own browser, and unlike Plum-Audio's `mesh/avahi.py` it
never *advertises*.

Sendspin publishes two service types, in opposite directions:

    _sendspin-server._tcp   servers, port 8927  — clients dial these
    _sendspin._tcp          players, port 8928  — servers dial these

Everything here is pure: no I/O, no Home Assistant runtime. That matters
because the listener URL this module produces becomes an entity's frozen unique
id, so a normalisation difference between a discovered record and a hand-typed
URL would give one physical device two identities and two entities.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import (
    DEFAULT_PLAYER_PORT,
    DEFAULT_SERVER_PORT,
    DEFAULT_WEBSOCKET_PATH,
    TXT_KEY_NAME,
    TXT_KEY_PATH,
    WEBSOCKET_SCHEME,
    ZEROCONF_SERVICE_TYPE_PLAYER,
    ZEROCONF_SERVICE_TYPE_SERVER,
)

type SendspinKind = Literal["server", "player"]

_KIND_BY_SERVICE_TYPE: dict[str, SendspinKind] = {
    ZEROCONF_SERVICE_TYPE_SERVER: "server",
    ZEROCONF_SERVICE_TYPE_PLAYER: "player",
}

_DEFAULT_PORT_BY_KIND: dict[SendspinKind, int] = {
    "server": DEFAULT_SERVER_PORT,
    "player": DEFAULT_PLAYER_PORT,
}


@dataclass(frozen=True, slots=True)
class SendspinDiscovery:
    """A Sendspin server or player seen on the LAN."""

    kind: SendspinKind
    """Which side of the protocol this is. Servers and players are dialled in
    opposite directions and must never be conflated."""

    listener_url: str
    """Canonical identity for HA purposes — entities are keyed on this, frozen
    as first seen. The *live* dial URL is stored separately and may move with
    DHCP; this must not be recomputed."""

    instance_name: str
    """The mDNS instance name. NOT the handshake name, and not necessarily
    human-friendly — third-party devices publish things like
    `home-assistant-voice-a1b2c3`."""

    txt_name: str | None
    """The TXT `name` value, or None when the device publishes none. The
    distinction matters: the name precedence chain needs to tell "not
    published" from "published as something"."""

    host: str
    port: int
    path: str


def _txt(properties: dict[Any, Any], key: str) -> str | None:
    """Read a TXT value, tolerating the bytes that python-zeroconf yields.

    HA core normalises these to `str`, but the raw library does not, and a
    silently-missed key here would fall back to a default rather than fail.
    """
    for raw_key, raw_value in properties.items():
        candidate = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
        if candidate != key:
            continue
        if raw_value is None:
            return None
        return raw_value.decode() if isinstance(raw_value, bytes) else str(raw_value)
    return None


def _instance_name(service_name: str, service_type: str) -> str:
    """Strip the service type suffix off a fully qualified service name.

    Instance names may themselves contain dots, so only the exact type suffix
    is removed — splitting on the first dot would truncate `Michael's Pi 4.0`.
    """
    suffix = f".{service_type}"
    if service_name.endswith(suffix):
        return service_name[: -len(suffix)]
    return service_name.removesuffix(".").removesuffix(
        f".{service_type.removesuffix('.')}"
    )


def _format_host(host: str) -> str:
    """Lower-case the host, drop the mDNS trailing dot, bracket IPv6."""
    cleaned = host.strip().rstrip(".").lower()
    if cleaned.startswith("["):
        return cleaned
    try:
        if isinstance(ip_address(cleaned), IPv6Address):
            return f"[{cleaned}]"
    except ValueError:
        pass
    return cleaned


def _format_path(path: str | None) -> str:
    """Normalise the websocket path, defaulting when absent.

    aiosendspin rejects a record whose path does not start with `/`
    (`server/server.py`), so failing here turns a silent dial failure into an
    explicit parse error.
    """
    if path is None or not path.strip():
        return DEFAULT_WEBSOCKET_PATH
    cleaned = path.strip()
    if not cleaned.startswith("/"):
        raise ValueError(f"Sendspin path must start with '/', got {path!r}")
    if len(cleaned) > 1:
        cleaned = cleaned.rstrip("/") or DEFAULT_WEBSOCKET_PATH
    return cleaned


def build_listener_url(
    host: str, port: int, path: str | None = DEFAULT_WEBSOCKET_PATH
) -> str:
    """Construct a canonical listener URL.

    The single place a Sendspin URL is built, so that discovered and
    hand-entered devices cannot acquire different identities.
    """
    return f"{WEBSOCKET_SCHEME}://{_format_host(host)}:{int(port)}{_format_path(path)}"


def normalise_listener_url(raw: str) -> str:
    """Normalise a user-supplied URL onto the canonical form.

    Accepts a bare `host:port[/path]` as well as a full `ws://` URL, because
    that is what people type. Rejects `wss://` outright rather than silently
    downgrading it: Sendspin has no TLS at any version, so a user who typed
    `wss` has a wrong expectation that should surface now.
    """
    candidate = raw.strip()
    if "://" not in candidate:
        candidate = f"{WEBSOCKET_SCHEME}://{candidate}"

    parts = urlsplit(candidate)
    if parts.scheme.lower() != WEBSOCKET_SCHEME:
        raise ValueError(
            f"Sendspin listener URLs must be ws:// (Sendspin has no TLS), "
            f"got {parts.scheme}://"
        )
    if not parts.hostname:
        raise ValueError(f"No host in Sendspin listener URL {raw!r}")

    try:
        port = parts.port
    except ValueError as err:  # non-numeric port
        raise ValueError(f"Invalid port in Sendspin listener URL {raw!r}") from err
    if port is None:
        raise ValueError(
            f"A port is required in a Sendspin listener URL (servers use "
            f"{DEFAULT_SERVER_PORT}, players {DEFAULT_PLAYER_PORT}), got {raw!r}"
        )

    return build_listener_url(parts.hostname, port, parts.path or None)


def parse_zeroconf(discovery_info: ZeroconfServiceInfo) -> SendspinDiscovery:
    """Turn a zeroconf record into a SendspinDiscovery."""
    kind = _KIND_BY_SERVICE_TYPE.get(discovery_info.type)
    if kind is None:
        raise ValueError(f"Not a Sendspin service type: {discovery_info.type!r}")

    properties = discovery_info.properties or {}
    path = _format_path(_txt(properties, TXT_KEY_PATH))
    host = _format_host(str(discovery_info.ip_address))
    port = discovery_info.port or _DEFAULT_PORT_BY_KIND[kind]

    return SendspinDiscovery(
        kind=kind,
        listener_url=build_listener_url(host, port, path),
        instance_name=_instance_name(discovery_info.name, discovery_info.type),
        txt_name=_txt(properties, TXT_KEY_NAME),
        host=host,
        port=port,
        path=path,
    )
