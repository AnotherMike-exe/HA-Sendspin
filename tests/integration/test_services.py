"""The adoption lifecycle services.

The rule under test throughout: **no service takes a `player_id`.** They target
Home Assistant devices, and the only raw identifier accepted anywhere is the
listener URL — the one field a speaker presents both while attached and while
idle.
"""

from __future__ import annotations

from unittest.mock import patch

from aiosendspin.models.types import ConnectionReason
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


async def test_reclaim_redials_the_speaker(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The escalation from a yielded adoption, which the user has to ask for.

    Asserted as a fresh dial rather than as a call to upstream's
    `reclaim_client_for_playback`: yielding now evicts the client from the
    library's registry, which is what that call resolves its URL against, so
    reclaim goes back through the ordinary dialling path.
    """
    await setup_hub(hass, fake_server, adopted=True)
    fake_server.dial_calls.clear()

    await hass.services.async_call(
        DOMAIN,
        SERVICE_RECLAIM_PLAYER,
        {"device_id": device_id_for(hass, PLAYER_URL), "timeout": 5},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert [call.url for call in fake_server.dial_calls] == [PLAYER_URL]
    assert fake_server.dial_calls[0].connection_reason is ConnectionReason.PLAYBACK
    assert fake_server.live_dial_urls == {PLAYER_URL}


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


# --- Device removal and diagnostics ----------------------------------------


async def test_deleting_a_device_drops_the_adoption(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """Removing a speaker from the UI must actually un-adopt it.

    Leaving the subentry behind would have the next reload re-dial the speaker
    and re-create the device the user just deleted.
    """
    from custom_components.sendspin import async_remove_config_entry_device

    entry = await setup_hub(hass, fake_server, adopted=True)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, PLAYER_URL)})

    assert await async_remove_config_entry_device(hass, entry, device) is True
    await hass.async_block_till_done()

    assert entry.subentries == {}


async def test_diagnostics_never_leak_the_private_key(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """The identity's private half must not appear anywhere in the dump."""
    from custom_components.sendspin.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await setup_hub(hass, fake_server, adopted=True)
    identity = entry.runtime_data.host.identity

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert identity.private_b64u not in str(dump)
    # The public server id is fine — every speaker on the network sees it.
    assert dump["server"]["server_id"] == identity.peer_id
    assert dump["server"]["advertises_mdns"] is False
    assert [e["frozen_url"] for e in dump["endpoints"]] == [PLAYER_URL]


async def test_removing_a_device_lets_it_be_adopted_again(
    hass: HomeAssistant, fake_server: FakeSendspinServer
) -> None:
    """A speaker handed to another unit, then deleted, must still re-adopt.

    The routed-away flag suppresses dialling and lives in the memo, which
    outlives the subentry. Left set, re-adopting the speaker would silently
    never dial it.
    """
    from custom_components.sendspin import async_remove_config_entry_device

    entry = await setup_hub(hass, fake_server, adopted=True)
    entry.runtime_data.memo.set_routed_away(PLAYER_URL, True)
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, PLAYER_URL)})

    await async_remove_config_entry_device(hass, entry, device)
    await hass.async_block_till_done()

    assert entry.runtime_data.memo.routed_away(PLAYER_URL) is False
