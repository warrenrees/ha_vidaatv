"""Tests for the Hisense TV coordinator."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.vidaa_tv.coordinator import VidaaTVDataUpdateCoordinator
from custom_components.vidaa_tv.const import (
    CONF_HW_MAC,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
    DOMAIN,
    SCAN_INTERVAL,
)

from .conftest import MOCK_CONFIG_ENTRY_DATA, MOCK_DEVICE_INFO, MOCK_TV_STATE, create_mock_config_entry


async def test_coordinator_update_success(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator successful update via proper entry setup."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator

    assert coordinator.data is not None
    assert coordinator.data["is_on"] is True
    assert coordinator.data["volume"] == 50
    assert coordinator.available is True


async def test_coordinator_update_tv_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator when TV is off."""
    mock_vidaa_tv.async_get_state = AsyncMock(
        return_value={"statetype": "fake_sleep_0"}
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["is_on"] is False


async def test_coordinator_live_tv_reports_source_and_channel(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A 'livetv' state must yield a source and the channel being watched.

    Payload captured verbatim from a 32A35HUV on 2026-08-04. Before 'livetv'
    was handled, watching a channel - the commonest thing a TV does - left both
    app and source None, so the UI showed no source at all.
    """
    mock_vidaa_tv.async_get_state = AsyncMock(
        return_value={
            "statetype": "livetv",
            "list_param": "400001",
            "channel_num": "5001",
            "channel_param": "#90546647#400001#100",
            "channel_name": "ROMCOM K-Drama",
            "sourceid": "TV",
        }
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    data = entry.runtime_data.coordinator.data
    assert data["is_on"] is True
    assert data["statetype"] == "livetv"
    # sourceid, not displayname: the source list is built from sourcename, and
    # this entry's displayname ("TV Channels") would match nothing in it.
    assert data["source"] == "TV"
    assert data["channel_name"] == "ROMCOM K-Drama"
    assert data["channel_number"] == "5001"
    assert data["app"] is None


async def test_coordinator_custom_scan_interval(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator respects custom scan interval from options."""
    entry = create_mock_config_entry(hass, options={"scan_interval": 60})
    entry.add_to_hass(hass)

    coordinator = VidaaTVDataUpdateCoordinator(hass, mock_vidaa_tv, entry)

    assert coordinator.update_interval == timedelta(seconds=60)


async def test_coordinator_default_scan_interval(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator uses default scan interval when not configured."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    coordinator = VidaaTVDataUpdateCoordinator(hass, mock_vidaa_tv, entry)

    assert coordinator.update_interval == timedelta(seconds=SCAN_INTERVAL)


async def test_coordinator_reconnect(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator reconnects when disconnected."""
    mock_vidaa_tv.is_connected = False

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Should have rebuilt the client (reset) then reconnected
    mock_vidaa_tv.async_reset.assert_called()
    mock_vidaa_tv.async_connect.assert_called()


async def test_coordinator_refreshes_token_near_expiry(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """An access token within a day of expiry is refreshed during update."""
    mock_vidaa_tv.async_token_status = AsyncMock(
        return_value={
            "has_token": True,
            "access_valid": True,
            "access_expires_in": 3600,  # < 1 day
            "needs_refresh": False,
            "needs_reauth": False,
        }
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    mock_vidaa_tv.async_refresh_token.assert_called()


async def test_coordinator_no_refresh_when_token_fresh(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A token with plenty of life left is not refreshed."""
    # Fixture default: access_expires_in = 7 days
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    mock_vidaa_tv.async_refresh_token.assert_not_called()


async def test_coordinator_no_refresh_when_needs_reauth(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """When both tokens are expired, don't attempt a doomed refresh."""
    mock_vidaa_tv.async_token_status = AsyncMock(
        return_value={
            "has_token": True,
            "access_valid": False,
            "access_expires_in": 0,
            "needs_refresh": False,
            "needs_reauth": True,
        }
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    mock_vidaa_tv.async_refresh_token.assert_not_called()


async def test_coordinator_turn_on_with_wol(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    mock_wake_tv: MagicMock,
) -> None:
    """Test coordinator turn_on sends WoL and power command."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_turn_on()

    # Should send WoL
    mock_wake_tv.assert_called()
    # Should send power on command
    mock_vidaa_tv.async_power_on.assert_called()


async def test_coordinator_turn_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator turn_off."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    await coordinator.async_turn_off()

    # We already know the TV is on from the poll, so send the key directly
    # instead of paying for power_off()'s state round-trip.
    mock_vidaa_tv.async_send_key.assert_called_once_with("KEY_POWER")
    mock_vidaa_tv.async_power_off.assert_not_called()


async def test_coordinator_volume_controls(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test coordinator volume controls."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator

    await coordinator.async_volume_up()
    mock_vidaa_tv.async_volume_up.assert_called()

    await coordinator.async_volume_down()
    mock_vidaa_tv.async_volume_down.assert_called()

    await coordinator.async_mute()
    mock_vidaa_tv.async_mute.assert_called()

    await coordinator.async_set_volume(75)
    mock_vidaa_tv.async_set_volume.assert_called_with(75)


async def test_coordinator_auth_failure_triggers_reauth(
    hass: HomeAssistant,
) -> None:
    """Test that multiple auth failures trigger reauth."""
    mock_tv = MagicMock()
    mock_tv.is_connected = True
    mock_tv.async_get_state = AsyncMock(side_effect=Exception("authentication failed"))
    mock_tv.async_disconnect = AsyncMock()

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    coordinator = VidaaTVDataUpdateCoordinator(hass, mock_tv, entry)

    # Simulate 3 auth failures
    for _ in range(3):
        with pytest.raises((UpdateFailed, ConfigEntryAuthFailed)):
            await coordinator._async_update_data()

    # Third failure should be ConfigEntryAuthFailed
    assert coordinator._auth_failures >= 3


# --- a TV that stops answering must read as OFF ----------------------------


async def test_silent_tv_reports_off_on_the_very_next_poll(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Regression: Home Assistant showed the TV as ON for ~2 minutes after it
    was switched off.

    pyvidaa's get_state() used to return the last known state when the TV
    stopped answering, so the coordinator could not tell "gone" from
    "unchanged" and kept reporting is_on=True until the MQTT keepalive expired
    and a reconnect finally failed. get_state() now returns None instead.
    """
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.data["is_on"] is True

    # The TV is switched off: still "connected" as far as the socket knows,
    # but it answers nothing.
    mock_vidaa_tv.async_get_state = AsyncMock(return_value=None)
    await coordinator.async_refresh()

    assert coordinator.data["is_on"] is False


async def test_turn_off_is_skipped_when_the_tv_is_already_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """KEY_POWER toggles, so sending it to a sleeping TV would wake it."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    mock_vidaa_tv.async_get_state = AsyncMock(return_value=None)
    await coordinator.async_refresh()
    mock_vidaa_tv.async_send_key.reset_mock()

    await coordinator.async_turn_off()

    mock_vidaa_tv.async_send_key.assert_not_called()


async def test_device_info_is_not_requested_forever(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Older firmware never answers getdeviceinfo, and each try costs a full
    timeout on every poll."""
    mock_vidaa_tv.async_get_device_info = AsyncMock(return_value=None)

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    for _ in range(6):
        await coordinator.async_refresh()

    assert coordinator._device_info_unsupported is True
    assert mock_vidaa_tv.async_get_device_info.await_count <= (
        coordinator._MAX_DEVICE_INFO_MISSES
    )


async def test_a_reconnect_re_asks_a_tv_we_had_given_up_on(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A rebooted TV may gain the capability (or was merely asleep)."""
    mock_vidaa_tv.async_get_device_info = AsyncMock(return_value=None)

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    for _ in range(6):
        await coordinator.async_refresh()
    assert coordinator._device_info_unsupported is True

    # The TV drops and comes back.
    mock_vidaa_tv.is_connected = False
    await coordinator.async_refresh()

    assert coordinator._device_info_unsupported is False


async def test_wake_on_lan_targets_every_known_interface(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """A magic packet only wakes the interface the TV is actually on, and the
    TV does not say which - so wake both rather than guessing.

    The reporter's TV needed the Wi-Fi MAC; we defaulted to Ethernet, so
    power-on silently did nothing until he set wol_mac by hand.
    """
    data = dict(MOCK_CONFIG_ENTRY_DATA)
    data[CONF_HW_MAC] = "a0:62:fb:66:77:ca"
    data[CONF_MAC_ETHERNET] = "a0:62:fb:66:77:ca"
    data[CONF_MAC_WIFI] = "f0:35:75:29:5a:e0"

    entry = create_mock_config_entry(hass, data=data)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ), patch("custom_components.vidaa_tv.coordinator.wake_tv") as mock_wake:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_turn_on()
        await hass.async_block_till_done()

    woken = [call[0][0] for call in mock_wake.call_args_list]
    assert "a0:62:fb:66:77:ca" in woken
    assert "f0:35:75:29:5a:e0" in woken
    # The Ethernet MAC is listed twice on the entry; wake it once.
    assert len(woken) == len(set(woken))


async def test_a_dropped_command_surfaces_as_an_error(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Regression: pyvidaa returns False when it cannot publish, and the
    coordinator discarded it - so Home Assistant reported success for a command
    that never left the process. That is what "the buttons do nothing, and
    nothing is logged" looked like."""
    from homeassistant.exceptions import HomeAssistantError

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    mock_vidaa_tv.async_send_key = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError):
        await coordinator.async_send_key("KEY_HOME")


async def test_a_single_lost_state_reply_does_not_flap_the_tv_off(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """State replies are QoS 0, so one dropped message must not read as off.

    Flipping off on the first miss would make the entity flicker; the TV is
    asked once more before we believe it.
    """
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    # First ask is lost, the retry succeeds.
    mock_vidaa_tv.async_get_state = AsyncMock(side_effect=[None, MOCK_TV_STATE])
    await coordinator.async_refresh()

    assert coordinator.data["is_on"] is True


async def test_an_unreachable_tv_is_off_not_an_error(
    hass: HomeAssistant,
    mock_vidaa_tv_offline: MagicMock,
) -> None:
    """A TV in standby takes its MQTT broker with it.

    Raising UpdateFailed for that logged an error on every power-off and made
    the remote entity unavailable for as long as the TV stayed off.
    """
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv_offline
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success is True
    assert coordinator.data["is_on"] is False
    assert coordinator.available is True


async def test_auth_failures_reset_after_a_good_poll(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Otherwise three auth blips spread over weeks trigger a spurious reauth."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    coordinator._auth_failures = 2
    await coordinator.async_refresh()

    assert coordinator._auth_failures == 0


async def test_device_info_ignores_a_non_dict_reply(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """An unrelated push can satisfy the library's request wait; a list landing
    here used to raise AttributeError and kill the whole poll."""
    mock_vidaa_tv.async_get_device_info = AsyncMock(
        return_value=[{"sourceid": "5", "sourcename": "HDMI2"}]
    )

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success is True
    assert coordinator.device_data == {}
