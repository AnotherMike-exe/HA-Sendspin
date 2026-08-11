"""The adoption lifecycle services.

The rule under test throughout: **no service takes a `player_id`.** They target
Home Assistant devices, and the only raw identifier accepted anywhere is the
listener URL — the one field a speaker presents both while attached and while
idle.
"""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.sendspin.const import (
    ATTR_LISTENER_URL,
    CONF_LISTENER_URL,
    DOMAIN,
    SERVICE_ADOPT_PLAYER,
    SERVICE_RECLAIM_PLAYER,
    SERVICE_RELEASE_PLAYER,
    SUBENTRY_TYPE_PLAYER,
)
from tests.fakes.fake_sendspin import FakeSendspinServer

PLAYER_URL = "ws://192.168.7.151:8928/sendspin"
CLIENT_ID = "98:A3:16:D0:9E:E8"


@pytest.fixture
def fake_server() -> FakeSendspinServer:
    """The server object the integration will be given."""
    server = FakeSendspinServer()
    server.attach(PLAYER_URL, CLIENT_ID, "Satellite1", volume=40)
    return server


async def setup_hub(
    hass: HomeAssistant, fake_server: FakeSendspinServer, *, adopted: bool = False
) -> MockConfigEntry:
    """Bring up a hub, optionally with one speaker already adopted."""
    subentries = (
        [
            {
                "subentry_type": SUBENTRY_TYPE_PLAYER,
                "title": "Satellite1",
                "data": {CONF_LISTENER_URL: PLAYER_URL},
                "unique_id": PLAYER_URL,
            }
        ]
        if adopted
        else []
    )
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


def device_id_for(hass: HomeAssistant, frozen_url: str) -> str:
    """The device registry id for an adopted endpoint."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, frozen_url)})
    assert device is not None
    return device.id


async def test_adopt_creates_the_same_state_as_the_ui(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """An automation and a click must produce identical results.

    The service routes through the same subentry machinery the UI uses, so the
    device and entity appear either way.
    """
    entry = await setup_hub(hass, fake_server)

    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADOPT_PLAYER,
            {ATTR_LISTENER_URL: PLAYER_URL},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert len(entry.subentries) == 1
    assert PLAYER_URL in [c.url for c in fake_server.dial_calls]
    assert hass.states.async_entity_ids("media_player")


async def test_adopt_normalises_a_hand_typed_address(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A typed address and a discovered one must resolve to one identity.

    Otherwise the same speaker acquires two entities.
    """
    entry = await setup_hub(hass, fake_server)

    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADOPT_PLAYER,
            {ATTR_LISTENER_URL: "  192.168.7.151:8928  "},
            blocking=True,
        )
        await hass.async_block_till_done()

    subentry = next(iter(entry.subentries.values()))
    assert subentry.data[CONF_LISTENER_URL] == PLAYER_URL


async def test_adopt_rejects_an_unusable_address(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Fail with something the user can act on, not a traceback."""
    await setup_hub(hass, fake_server)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADOPT_PLAYER,
            {ATTR_LISTENER_URL: "wss://192.168.7.151:8928/sendspin"},
            blocking=True,
        )


async def test_adopting_twice_is_harmless(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Automations re-run; a second adoption must not create a second entity."""
    entry = await setup_hub(hass, fake_server, adopted=True)

    with patch(
        "custom_components.sendspin.server_host.SendspinServer",
        return_value=fake_server,
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_ADOPT_PLAYER,
            {ATTR_LISTENER_URL: PLAYER_URL},
            blocking=True,
        )
        await hass.async_block_till_done()

    assert len(entry.subentries) == 1


async def test_release_stops_holding_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Releasing frees a speaker for another server to take."""
    await setup_hub(hass, fake_server, adopted=True)
    assert fake_server.live_dial_urls == {PLAYER_URL}

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RELEASE_PLAYER,
        {"device_id": device_id_for(hass, PLAYER_URL)},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert fake_server.live_dial_urls == set()


async def test_reclaim_asserts_a_playback_claim(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The escalation from a yielded adoption, which the user has to ask for."""
    await setup_hub(hass, fake_server, adopted=True)

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECLAIM_PLAYER,
        {"device_id": device_id_for(hass, PLAYER_URL), "timeout": 5},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert fake_server.reclaim_calls == [(CLIENT_ID, 5.0)]


async def test_targeting_an_unknown_device_is_a_clear_error(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Never fail with a KeyError at the user."""
    await setup_hub(hass, fake_server, adopted=True)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RELEASE_PLAYER,
            {"device_id": "does-not-exist"},
            blocking=True,
        )
