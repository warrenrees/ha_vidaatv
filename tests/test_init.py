"""Tests for the Hisense TV integration setup."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.vidaa_tv import (
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.vidaa_tv.const import (
    AUTH_MODE_AUTO,
    AUTH_MODE_DYNAMIC,
    AUTH_MODE_STATIC,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_HW_MAC,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
    DOMAIN,
)

from .conftest import MOCK_CONFIG_ENTRY_DATA, create_mock_config_entry


async def test_async_setup(hass: HomeAssistant) -> None:
    """Test async_setup registers services."""
    result = await async_setup(hass, {})

    assert result is True
    assert hass.services.has_service(DOMAIN, "send_key")
    assert hass.services.has_service(DOMAIN, "launch_app")


async def test_async_setup_entry_success(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test successful entry setup."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        # Use the proper setup mechanism
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.runtime_data is not None
    assert entry.runtime_data.coordinator is not None
    assert entry.runtime_data.tv is not None


async def test_async_setup_entry_loads_when_tv_offline(
    hass: HomeAssistant,
    mock_vidaa_tv_offline: MagicMock,
) -> None:
    """A TV in deep sleep must still set up so the WoL power button exists."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv_offline,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Entry loads even though the TV is unreachable, and an unreachable TV is
    # reported as OFF rather than as a failed update: it took its MQTT broker
    # down with it, which is a power state, not an error. Treating it as a
    # failure logged an error on every power-off and dropped the remote entity
    # to unavailable for the whole time the TV was off.
    from homeassistant.config_entries import ConfigEntryState
    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data.coordinator
    assert coordinator.last_update_success is True
    assert coordinator.data["is_on"] is False
    assert coordinator.available is True


async def test_async_unload_entry(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test entry unload."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    result = await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert result is True
    mock_vidaa_tv.async_close.assert_awaited_once()


async def test_send_key_service(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test send_key service executes without error."""
    # Setup integration
    await async_setup(hass, {})

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Call service - should execute without error
        await hass.services.async_call(
            DOMAIN,
            "send_key",
            {"key": "KEY_POWER"},
            blocking=True,
        )

    # Service call completed without raising an error
    # (mock assertions are complex due to coordinator indirection)


async def test_launch_app_service(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Test launch_app service executes without error."""
    # Setup integration
    await async_setup(hass, {})

    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Call service - should execute without error
        await hass.services.async_call(
            DOMAIN,
            "launch_app",
            {"app": "netflix"},
            blocking=True,
        )

    # Service call completed without raising an error
    # (mock assertions are complex due to coordinator indirection)


# --- authentication mode ---------------------------------------------------


@pytest.mark.parametrize(
    ("entry_data_mode", "options_mode", "expect_dynamic"),
    [
        (None, None, True),                 # paired before the option existed
        (AUTH_MODE_AUTO, None, True),
        (AUTH_MODE_STATIC, None, False),    # what a legacy-firmware TV pairs as
        (AUTH_MODE_DYNAMIC, AUTH_MODE_STATIC, False),  # options override wins
        (AUTH_MODE_STATIC, AUTH_MODE_DYNAMIC, True),
    ],
)
async def test_setup_entry_uses_the_stored_auth_mode(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    entry_data_mode: str | None,
    options_mode: str | None,
    expect_dynamic: bool,
) -> None:
    """The runtime client must use the scheme the entry paired with.

    Regression: this was hardcoded to dynamic auth, so a TV that needs the
    static login could pair but never reconnect after a restart.
    """
    data = dict(MOCK_CONFIG_ENTRY_DATA)
    if entry_data_mode is not None:
        data[CONF_AUTH_MODE] = entry_data_mode
    options = {CONF_AUTH_MODE: options_mode} if options_mode else {}

    entry = create_mock_config_entry(hass, data=data, options=options)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV",
        return_value=mock_vidaa_tv,
    ) as mock_class:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert mock_class.call_args.kwargs["use_dynamic_auth"] is expect_dynamic


# --- Wake-on-LAN target ----------------------------------------------------


async def test_turn_on_wakes_the_tv_using_the_stored_hardware_mac(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Regression: the TV could be turned off but never back on.

    Older firmware never answers getdeviceinfo, so device_id stays empty and
    there was no MAC to wake - power-on silently did nothing (the TV is off, so
    the MQTT command cannot reach it either).
    """
    data = dict(MOCK_CONFIG_ENTRY_DATA)
    data.pop(CONF_DEVICE_ID, None)
    data[CONF_HW_MAC] = "a0:62:fb:66:77:ca"

    entry = create_mock_config_entry(hass, data=data)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ), patch("custom_components.vidaa_tv.coordinator.wake_tv") as mock_wake:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await entry.runtime_data.coordinator.async_turn_on()
        await hass.async_block_till_done()

    woken = {call[0][0] for call in mock_wake.call_args_list}
    assert "a0:62:fb:66:77:ca" in woken


async def test_setup_backfills_the_hardware_mac_for_older_entries(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
    mock_hw_mac_probe: MagicMock,
) -> None:
    """Entries paired before the MAC was stored must self-heal, not need re-pairing."""
    data = dict(MOCK_CONFIG_ENTRY_DATA)
    data.pop(CONF_HW_MAC, None)

    entry = create_mock_config_entry(hass, data=data)
    entry.add_to_hass(hass)

    with patch("custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    mock_hw_mac_probe.assert_called_once()
    assert entry.data[CONF_HW_MAC] == "00:11:22:33:44:55"


async def test_backfill_does_not_reload_the_integration(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Writing to entry.data fires the update listener; it must not reload.

    A reload mid-setup would tear down the client that was just built.
    """
    data = dict(MOCK_CONFIG_ENTRY_DATA)
    data.pop(CONF_HW_MAC, None)

    entry = create_mock_config_entry(hass, data=data)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv
    ), patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as mock_reload:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    mock_reload.assert_not_called()


async def test_changing_options_still_reloads(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """The listener must keep doing its actual job."""
    entry = create_mock_config_entry(hass)
    entry.add_to_hass(hass)

    with patch("custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(entry, options={"scan_interval": 60})
        await hass.async_block_till_done()

    assert entry.runtime_data.options_snapshot == {"scan_interval": 60}


async def test_device_page_shows_both_interface_macs(
    hass: HomeAssistant,
    mock_vidaa_tv: MagicMock,
) -> None:
    """Both MACs must be visible, since only one of them can be woken.

    Wake-on-LAN reaches only the interface the TV is actually connected on,
    and the TV does not say which - so the owner needs to see both to pick.
    """
    from homeassistant.helpers import device_registry as dr

    data = dict(MOCK_CONFIG_ENTRY_DATA)
    data[CONF_HW_MAC] = "a0:62:fb:66:77:ca"
    data[CONF_MAC_ETHERNET] = "a0:62:fb:66:77:ca"
    data[CONF_MAC_WIFI] = "f0:35:75:29:5a:e0"

    entry = create_mock_config_entry(hass, data=data)
    entry.add_to_hass(hass)

    with patch("custom_components.vidaa_tv.AsyncVidaaTV", return_value=mock_vidaa_tv):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, "001122334455")})
    assert device is not None

    macs = {value for kind, value in device.connections if kind == dr.CONNECTION_NETWORK_MAC}
    assert "a0:62:fb:66:77:ca" in macs
    assert "f0:35:75:29:5a:e0" in macs
