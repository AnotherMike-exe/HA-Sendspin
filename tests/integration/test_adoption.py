"""Discovery, consent and adoption.

The rule these tests exist to protect: **a discovered speaker is never adopted
automatically.** Dialling one takes it from whatever currently holds it, and on
hardware that produced a tug-of-war with Music Assistant that neither side won
(docs/OPEN-QUESTIONS.md §7).
"""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sendspin.const import (
    CONF_LISTENER_URL,
    DOMAIN,
    SUBENTRY_TYPE_PLAYER,
    ZEROCONF_SERVICE_TYPE_PLAYER,
    ZEROCONF_SERVICE_TYPE_SERVER,
)
from tests.fakes.fake_sendspin import FakeSendspinServer

PLAYER_URL = "ws://192.168.7.151:8928/sendspin"


def player_record(host: str = "192.168.7.151", instance: str = "satellite1-a1b2c3"):
    """An mDNS record for a Sendspin speaker."""
    addr = ip_address(host)
    return ZeroconfServiceInfo(
        ip_address=addr,
        ip_addresses=[addr],
        port=8928,
        hostname="satellite1.local.",
        type=ZEROCONF_SERVICE_TYPE_PLAYER,
        name=f"{instance}.{ZEROCONF_SERVICE_TYPE_PLAYER}",
        properties={"path": "/sendspin", "name": "Satellite1"},
    )


def server_record():
    """An mDNS record for a Sendspin server."""
    addr = ip_address("192.168.7.204")
    return ZeroconfServiceInfo(
        ip_address=addr,
        ip_addresses=[addr],
        port=8927,
        hostname="plum-amp100.local.",
        type=ZEROCONF_SERVICE_TYPE_SERVER,
        name=f"unit-7204.{ZEROCONF_SERVICE_TYPE_SERVER}",
        properties={"path": "/sendspin", "name": "Plum Amp100"},
    )


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """The server object the integration will be given."""
    return FakeSendspinServer()


async def setup_hub(
    hass: HomeAssistant, fake_server: FakeSendspinServer, subentries=()
) -> MockConfigEntry:
    """Bring up a hub with the real server class swapped out."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hub",
        title="Home",
        data={CONF_NAME: "Home"},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


# --- Discovery never adopts ------------------------------------------------


async def test_discovering_a_speaker_never_dials_it(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The single most important rule in the integration.

    Adopting a speaker takes it from whatever holds it. That has to be a
    decision the user makes, not a consequence of turning a speaker on.
    """
    await setup_hub(hass, fake_server)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=player_record()
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert fake_server.dial_calls == []


async def test_a_discovered_speaker_is_offered_for_adoption(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Discovery is not wasted — it populates the adoption picker."""
    entry = await setup_hub(hass, fake_server)
    await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=player_record()
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PLAYER), context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.FORM
    assert PLAYER_URL in result["description_placeholders"]["discovered"]
    # Pre-filled, so adopting a discovered speaker is one click.
    assert result["data_schema"]({})[CONF_LISTENER_URL] == PLAYER_URL


async def test_adopting_dials_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Consent given, the speaker is dialled and an entity appears."""
    entry = await setup_hub(hass, fake_server)
    fake_server.attach(PLAYER_URL, "98:A3:16:D0:9E:E8", "Satellite1", volume=40)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_PLAYER), context={"source": SOURCE_USER}
    )
    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        done = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_LISTENER_URL: PLAYER_URL}
        )
        await hass.async_block_till_done()

    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert PLAYER_URL in [c.url for c in fake_server.dial_calls]
    assert hass.states.async_entity_ids("media_player")


# --- Hub creation ----------------------------------------------------------


async def test_discovery_offers_to_set_sendspin_up_when_there_is_no_hub(
    hass: HomeAssistant,
) -> None:
    """Seeing Sendspin on the network is a reasonable prompt to configure it."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=server_record()
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["description_placeholders"]["name"] == "Plum Amp100"


async def test_confirming_discovery_creates_the_hub_but_adopts_nothing(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The confirm step sets up the integration; it does not claim a speaker."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=player_record()
    )
    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        created = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_NAME: "Home"}
        )
        await hass.async_block_till_done()

    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert fake_server.dial_calls == []


async def test_an_unrecognised_record_is_ignored(hass: HomeAssistant) -> None:
    """Other people's mDNS traffic is not ours to act on."""
    addr = ip_address("192.168.7.99")
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=ZeroconfServiceInfo(
            ip_address=addr,
            ip_addresses=[addr],
            port=80,
            hostname="printer.local.",
            type="_http._tcp.local.",
            name="printer._http._tcp.local.",
            properties={},
        ),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_sendspin"


# --- Restoring adoptions ---------------------------------------------------


async def test_adoptions_are_restored_on_restart(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Every speaker the user consented to is re-adopted, and only those."""
    await setup_hub(
        hass,
        fake_server,
        subentries=[
            {
                "subentry_type": SUBENTRY_TYPE_PLAYER,
                "title": "Satellite1",
                "data": {CONF_LISTENER_URL: PLAYER_URL},
                "unique_id": PLAYER_URL,
            }
        ],
    )

    assert [c.url for c in fake_server.dial_calls] == [PLAYER_URL]
    assert len(hass.states.async_entity_ids("media_player")) == 1
