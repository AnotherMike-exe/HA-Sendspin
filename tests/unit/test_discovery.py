"""Tests for mDNS record parsing.

`discovery` is pure — no I/O, no Home Assistant runtime — so it is the cheapest
place in the integration to pin behaviour, and the place where getting it wrong
is most expensive: the listener URL produced here becomes an entity's frozen
unique id for the life of that entity.
"""

from __future__ import annotations

from ipaddress import ip_address

from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest

from custom_components.sendspin.const import (
    DEFAULT_PLAYER_PORT,
    DEFAULT_SERVER_PORT,
    ZEROCONF_SERVICE_TYPE_PLAYER,
    ZEROCONF_SERVICE_TYPE_SERVER,
)
from custom_components.sendspin.discovery import (
    SendspinDiscovery,
    build_listener_url,
    normalise_listener_url,
    parse_zeroconf,
)


def make_info(
    *,
    service_type: str = ZEROCONF_SERVICE_TYPE_SERVER,
    instance: str = "unit-210",
    host: str = "192.168.7.204",
    hostname: str = "pi4-02.local.",
    port: int | None = DEFAULT_SERVER_PORT,
    properties: dict | None = None,
) -> ZeroconfServiceInfo:
    """Build a ZeroconfServiceInfo the way HA core hands one over."""
    addr = ip_address(host)
    return ZeroconfServiceInfo(
        ip_address=addr,
        ip_addresses=[addr],
        port=port,
        hostname=hostname,
        type=service_type,
        name=f"{instance}.{service_type}",
        properties={"path": "/sendspin", "name": "Pi4-02"}
        if properties is None
        else properties,
    )


# --- Service type branching ------------------------------------------------


def test_parses_a_server_record() -> None:
    """A _sendspin-server._tcp record is a server, dialled by clients."""
    result = parse_zeroconf(make_info())

    assert result == SendspinDiscovery(
        kind="server",
        listener_url="ws://192.168.7.204:8927/sendspin",
        instance_name="unit-210",
        txt_name="Pi4-02",
        host="192.168.7.204",
        port=8927,
        path="/sendspin",
    )


def test_parses_a_player_record() -> None:
    """A _sendspin._tcp record is a player, dialled by servers.

    Servers and players are *different* service types in opposite directions.
    Treating them as one is the first thing that breaks discovery.
    """
    result = parse_zeroconf(
        make_info(
            service_type=ZEROCONF_SERVICE_TYPE_PLAYER,
            instance="player-211",
            port=DEFAULT_PLAYER_PORT,
            properties={"path": "/sendspin", "name": "Kitchen"},
        )
    )

    assert result.kind == "player"
    assert result.listener_url == "ws://192.168.7.204:8928/sendspin"
    assert result.instance_name == "player-211"
    assert result.txt_name == "Kitchen"


def test_rejects_an_unknown_service_type() -> None:
    """Anything that is not one of the two Sendspin types is not ours."""
    with pytest.raises(ValueError, match="service type"):
        parse_zeroconf(make_info(service_type="_http._tcp.local."))


# --- TXT record handling ---------------------------------------------------


def test_absent_path_txt_falls_back_to_the_default() -> None:
    """`path` is optional in practice; /sendspin is the protocol default."""
    result = parse_zeroconf(make_info(properties={"name": "Pi4-02"}))

    assert result.path == "/sendspin"
    assert result.listener_url == "ws://192.168.7.204:8927/sendspin"


def test_absent_name_txt_leaves_txt_name_unset() -> None:
    """Third-party devices routinely publish no `name` key.

    The instance name must survive as the fallback, and txt_name must be None
    rather than an invented value — the name precedence chain depends on being
    able to tell "not published" from "published as something".
    """
    result = parse_zeroconf(
        make_info(instance="home-assistant-voice-a1b2c3", properties={})
    )

    assert result.txt_name is None
    assert result.instance_name == "home-assistant-voice-a1b2c3"


def test_rejects_a_path_without_a_leading_slash() -> None:
    """aiosendspin rejects these server-side; fail here rather than on dial."""
    with pytest.raises(ValueError, match="path"):
        parse_zeroconf(make_info(properties={"path": "sendspin"}))


def test_decodes_bytes_txt_values() -> None:
    """python-zeroconf yields bytes; HA normalises, but do not rely on it."""
    result = parse_zeroconf(
        make_info(properties={b"path": b"/sendspin", b"name": b"Pi4-02"})
    )

    assert result.path == "/sendspin"
    assert result.txt_name == "Pi4-02"


# --- Instance name extraction ----------------------------------------------


def test_instance_name_strips_the_service_type_and_trailing_dot() -> None:
    """`name` arrives fully qualified: instance + type + trailing dot."""
    result = parse_zeroconf(make_info(instance="unit-7204"))

    assert result.instance_name == "unit-7204"


def test_instance_name_survives_dots_in_the_instance_itself() -> None:
    """mDNS instance names may contain dots; only the type suffix is stripped."""
    result = parse_zeroconf(make_info(instance="Michael's Pi 4.0"))

    assert result.instance_name == "Michael's Pi 4.0"


# --- Address handling ------------------------------------------------------


def test_ipv6_addresses_are_bracketed() -> None:
    """An unbracketed IPv6 literal produces a URL that cannot be dialled."""
    result = parse_zeroconf(make_info(host="fd00::1"))

    assert result.listener_url == "ws://[fd00::1]:8927/sendspin"


def test_port_falls_back_to_the_default_for_the_service_type() -> None:
    """A record with no port is malformed, but the type implies the port."""
    assert parse_zeroconf(make_info(port=None)).port == DEFAULT_SERVER_PORT
    assert (
        parse_zeroconf(
            make_info(service_type=ZEROCONF_SERVICE_TYPE_PLAYER, port=None)
        ).port
        == DEFAULT_PLAYER_PORT
    )


# --- URL normalisation -----------------------------------------------------
#
# A hand-typed URL and a discovered one MUST normalise identically. If they do
# not, the same physical device acquires two frozen identities and therefore
# two entities.


@pytest.mark.parametrize(
    "typed",
    [
        "ws://192.168.7.204:8927/sendspin",
        "WS://192.168.7.204:8927/sendspin",
        "ws://192.168.7.204:8927/sendspin/",
        "ws://192.168.7.204:8927/sendspin  ",
        "  ws://192.168.7.204:8927/sendspin",
        "192.168.7.204:8927/sendspin",
        "192.168.7.204:8927",
        "ws://192.168.7.204:8927",
    ],
)
def test_hand_typed_urls_normalise_to_the_discovered_form(typed: str) -> None:
    """Every reasonable spelling collapses onto one canonical identity."""
    discovered = parse_zeroconf(make_info()).listener_url

    assert normalise_listener_url(typed) == discovered


def test_normalisation_lowercases_the_host_but_not_the_path() -> None:
    """Hostnames are case-insensitive; paths are not."""
    assert (
        normalise_listener_url("ws://Pi4-02.local:8927/Sendspin")
        == "ws://pi4-02.local:8927/Sendspin"
    )


def test_normalisation_strips_the_mdns_trailing_dot() -> None:
    """`pi4-02.local.` and `pi4-02.local` are the same host."""
    assert (
        normalise_listener_url("ws://pi4-02.local.:8927/sendspin")
        == "ws://pi4-02.local:8927/sendspin"
    )


def test_wss_is_rejected() -> None:
    """Sendspin has no TLS at any version. Fail loudly, never downgrade."""
    with pytest.raises(ValueError, match="ws://"):
        normalise_listener_url("wss://192.168.7.204:8927/sendspin")


def test_a_url_without_a_port_is_rejected() -> None:
    """There is no single default port — it depends on server vs player."""
    with pytest.raises(ValueError, match="port"):
        normalise_listener_url("ws://192.168.7.204/sendspin")


def test_build_listener_url_defaults_the_path() -> None:
    """The builder is the one place a URL is constructed, so it defaults too."""
    assert build_listener_url("192.168.7.204", 8927) == (
        "ws://192.168.7.204:8927/sendspin"
    )
