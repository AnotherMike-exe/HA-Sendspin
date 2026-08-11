"""Config entry setup, unload and the hub config flow."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sendspin.const import DOMAIN
from tests.fakes.fake_sendspin import FakeSendspinServer


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """The server object the integration will be given."""
    return FakeSendspinServer()


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A configured Sendspin hub."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, unique_id="hub", title="Home", data={CONF_NAME: "Home"}
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def setup_entry(
    hass: HomeAssistant, entry: MockConfigEntry, fake_server: FakeSendspinServer
) -> None:
    """Set the entry up with the real server class swapped out."""
    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_setup_brings_up_the_server(
    hass: HomeAssistant, entry: MockConfigEntry, fake_server: FakeSendspinServer
) -> None:
    """A loaded entry owns a running server host on its runtime data."""
    await setup_entry(hass, entry, fake_server)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.host.server is fake_server
    # The identity is what peers see; it must be a real key, not a placeholder.
    assert entry.runtime_data.host.server_id


async def test_unload_closes_the_server(
    hass: HomeAssistant, entry: MockConfigEntry, fake_server: FakeSendspinServer
) -> None:
    """Unloading must not leave a socket or a dial task behind.

    A reload that leaked dialers would have the old and new entries racing each
    other for the same speakers.
    """
    await setup_entry(hass, entry, fake_server)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert fake_server.closed is True


async def test_the_server_is_never_started_or_advertised(
    hass: HomeAssistant, entry: MockConfigEntry, fake_server: FakeSendspinServer
) -> None:
    """`start_server()` must never be called.

    Calling it constructs an AsyncZeroconf, binds UDP 5353 and advertises this
    server on the LAN. Home Assistant browses via core's zeroconf integration;
    it does not advertise, and it never accepts inbound connections because
    Sendspin servers dial players rather than the reverse.

    The fake raises from `start_server`, so a setup that called it would fail
    here rather than quietly putting Home Assistant on the LAN as a server.
    """
    await setup_entry(hass, entry, fake_server)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.host.adopted_urls == frozenset()


async def test_the_hub_is_a_singleton(hass: HomeAssistant) -> None:
    """Only one Sendspin hub can exist: one identity, one set of speakers."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert first["type"] is FlowResultType.FORM

    created = await hass.config_entries.flow.async_configure(
        first["flow_id"], {CONF_NAME: "Home"}
    )
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert created["data"] == {CONF_NAME: "Home"}

    second = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "single_instance_allowed"


async def test_the_server_name_defaults_to_the_instance_name(
    hass: HomeAssistant,
) -> None:
    """The name speakers see should default to what the user already named HA."""
    hass.config.location_name = "Beach House"

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )

    assert result["data_schema"]({})[CONF_NAME] == "Beach House"
