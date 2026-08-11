"""Tests for the player memo — the one place a display name is derived.

A Sendspin speaker presents two identities that share exactly one field:

  * attached, it reports a **handshake name** ("FutureProofHomes - Satellite1")
    and a client id that is typically a MAC;
  * idle, all that exists is its **mDNS instance name**, and third-party
    devices routinely publish no `name` TXT at all, leaving something like
    `home-assistant-voice-a1b2c3`.

One device therefore reads as two and appears to rename itself every time it
joins or leaves. The listener URL is the only identifier both views share.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.sendspin.player_memo import MEMO_STORAGE_KEY, PlayerMemo

URL = "ws://192.168.7.151:8928/sendspin"
OTHER_URL = "ws://192.168.7.201:8928/sendspin"


async def load(hass: HomeAssistant) -> PlayerMemo:
    """Build and load a memo."""
    memo = PlayerMemo(hass)
    await memo.async_load()
    return memo


# --- Name precedence -------------------------------------------------------


async def test_falls_back_to_host_and_port_when_nothing_is_known(
    hass: HomeAssistant,
) -> None:
    """An unknown speaker is still identifiable, if not pretty."""
    memo = await load(hass)

    assert memo.display_name(URL) == "192.168.7.151:8928"


async def test_the_mdns_instance_name_beats_nothing(hass: HomeAssistant) -> None:
    """Better than host:port, and it is all an idle third-party device gives."""
    memo = await load(hass)
    memo.remember_discovery(URL, instance_name="home-assistant-voice-a1b2c3")

    assert memo.display_name(URL) == "home-assistant-voice-a1b2c3"


async def test_the_name_txt_beats_the_instance_name(hass: HomeAssistant) -> None:
    """A published `name` is human-chosen; the instance name is machine-shaped."""
    memo = await load(hass)
    memo.remember_discovery(URL, instance_name="player-211", txt_name="Kitchen Speaker")

    assert memo.display_name(URL) == "Kitchen Speaker"


async def test_the_handshake_name_wins(hass: HomeAssistant) -> None:
    """What the device calls itself when talking to us is the best answer."""
    memo = await load(hass)
    memo.remember_discovery(URL, instance_name="player-211", txt_name="Kitchen Speaker")
    memo.remember_handshake(URL, name="Plum Amp100", client_id="player-7204")

    assert memo.display_name(URL) == "Plum Amp100"


async def test_a_handshake_name_is_never_demoted(hass: HomeAssistant) -> None:
    """A later mDNS-only sighting must not undo what we learned while attached.

    This is the whole point of the memo. Without it a speaker's name flips
    every time it disconnects, because the good name is only visible while it
    is attached.
    """
    memo = await load(hass)
    memo.remember_handshake(URL, name="Plum Amp100", client_id="player-7204")

    memo.remember_discovery(URL, instance_name="home-assistant-voice-a1b2c3")

    assert memo.display_name(URL) == "Plum Amp100"


async def test_a_handshake_name_can_be_updated_by_a_later_handshake(
    hass: HomeAssistant,
) -> None:
    """Never demoted is not the same as frozen — a real rename must land."""
    memo = await load(hass)
    memo.remember_handshake(URL, name="Old Name", client_id="player-7204")

    memo.remember_handshake(URL, name="New Name", client_id="player-7204")

    assert memo.display_name(URL) == "New Name"


async def test_a_blank_name_is_not_a_name(hass: HomeAssistant) -> None:
    """Some devices hand over an empty string; that must not win."""
    memo = await load(hass)
    memo.remember_discovery(URL, instance_name="player-211")
    memo.remember_handshake(URL, name="   ", client_id="player-7204")

    assert memo.display_name(URL) == "player-211"


# --- Identity --------------------------------------------------------------


async def test_a_moved_speaker_is_matched_by_its_instance_name(
    hass: HomeAssistant,
) -> None:
    """DHCP moves the dial URL; the frozen identity must not move with it.

    Rediscovery at a new address has to resolve back to the original entity,
    or the speaker acquires a second one and the user loses its name, area and
    customisation.
    """
    memo = await load(hass)
    memo.remember_discovery(URL, instance_name="satellite1-a1b2c3")

    moved = "ws://192.168.7.99:8928/sendspin"
    assert memo.frozen_url_for_instance("satellite1-a1b2c3") == URL
    assert memo.frozen_url_for_instance("something-else") is None

    memo.remember_dial_url(URL, moved)
    assert memo.dial_url(URL) == moved
    # The identity itself is untouched.
    assert memo.display_name(URL) == "satellite1-a1b2c3"


async def test_the_dial_url_defaults_to_the_frozen_url(hass: HomeAssistant) -> None:
    """Until a speaker moves, the two are the same thing."""
    memo = await load(hass)

    assert memo.dial_url(URL) == URL


async def test_the_client_id_is_remembered_for_reclaim(hass: HomeAssistant) -> None:
    """Reclaim needs a client id, which is only visible while attached."""
    memo = await load(hass)
    memo.remember_handshake(URL, name="Satellite1", client_id="98:A3:16:D0:9E:E8")

    assert memo.client_id(URL) == "98:A3:16:D0:9E:E8"
    assert memo.client_id(OTHER_URL) is None


# --- Persistence -----------------------------------------------------------


async def test_the_memo_survives_a_restart(
    hass: HomeAssistant, hass_storage: dict
) -> None:
    """An offline speaker must keep its good name across a restart.

    Otherwise every Home Assistant restart renames every speaker that happens
    to be unreachable at that moment.
    """
    memo = await load(hass)
    memo.remember_handshake(URL, name="Plum Amp100", client_id="player-7204")
    await memo.async_save()

    assert MEMO_STORAGE_KEY in hass_storage

    reloaded = await load(hass)
    assert reloaded.display_name(URL) == "Plum Amp100"
    assert reloaded.client_id(URL) == "player-7204"
