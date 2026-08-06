"""Tests for the Vidaa TV remote entity activity reporting."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.vidaa_tv.const import ACTIVITY_HOME
from custom_components.vidaa_tv.remote import VidaaTVRemote


def _make_remote(data: dict | None) -> VidaaTVRemote:
    coordinator = MagicMock()
    coordinator.data = data
    entry = MagicMock()
    entry.data = {"device_id": "001122334455"}
    entry.entry_id = "test_entry"
    return VidaaTVRemote(coordinator, entry)


def test_current_activity_is_home_on_launcher() -> None:
    """At the home screen the activity should be Home.

    The coordinator names the launcher itself, so it arrives as the source like
    any other - see test_coordinator_launcher_reports_home_as_the_source.
    """
    remote = _make_remote(
        {
            "is_on": True,
            "statetype": "remote_launcher",
            "app": None,
            "source": ACTIVITY_HOME,
        }
    )
    assert remote.current_activity == ACTIVITY_HOME


def test_current_activity_is_dropped_when_unavailable() -> None:
    """A failed poll leaves the last data in place; do not report it as live."""
    coordinator = MagicMock()
    coordinator.data = {"is_on": True, "app": "Netflix", "source": "Netflix"}
    coordinator.available = False
    entry = MagicMock()
    entry.data = {"device_id": "001122334455"}
    entry.entry_id = "test_entry"

    assert VidaaTVRemote(coordinator, entry).current_activity is None


def test_current_activity_prefers_app_and_source() -> None:
    """An active app or source takes precedence over the Home fallback."""
    assert (
        _make_remote(
            {"is_on": True, "statetype": "app", "app": "Netflix", "source": None}
        ).current_activity
        == "Netflix"
    )
    assert (
        _make_remote(
            {"is_on": True, "statetype": "sourceswitch", "app": None, "source": "HDMI1"}
        ).current_activity
        == "HDMI1"
    )


def test_current_activity_none_when_off() -> None:
    """When the TV is off there is no current activity."""
    remote = _make_remote(
        {"is_on": False, "statetype": "fake_sleep_0", "app": None, "source": None}
    )
    assert remote.current_activity is None


def test_activity_list_includes_home() -> None:
    """Home is always offered, with fetched apps appended."""
    remote = _make_remote({"is_on": True})
    assert remote.activity_list == [ACTIVITY_HOME]

    remote._activity_list = ["Netflix", "YouTube"]
    assert remote.activity_list == [ACTIVITY_HOME, "Netflix", "YouTube"]


async def test_turn_on_home_sends_home_key_not_launch() -> None:
    """Selecting Home navigates via the home key rather than launching an app."""
    remote = _make_remote({"is_on": True})
    remote.coordinator.async_turn_on = AsyncMock()
    remote.coordinator.async_send_key = AsyncMock()
    remote.coordinator.async_launch_app = AsyncMock()

    await remote.async_turn_on(activity=ACTIVITY_HOME)

    remote.coordinator.async_send_key.assert_awaited_once_with("KEY_HOME")
    remote.coordinator.async_launch_app.assert_not_called()


def test_device_info_prefers_live_device_data() -> None:
    """DeviceInfo surfaces the TV's model, firmware, name, MAC, and IP."""
    from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC

    remote = _make_remote({"is_on": True})
    remote.coordinator.device_data = {
        "model": "32A35HUV_0002",
        "sw_version": "V0000.09.01O",
        "name": "Bodhi's TV",
        "ip": "10.0.0.162",
        "device_id": "001122334455",
    }

    info = remote.device_info
    assert info["model"] == "32A35HUV_0002"
    assert info["sw_version"] == "V0000.09.01O"
    assert info["name"] == "Bodhi's TV"
    assert info["configuration_url"] == "http://10.0.0.162"
    assert (CONNECTION_NETWORK_MAC, "00:11:22:33:44:55") in info["connections"]


def test_device_info_falls_back_to_entry_data() -> None:
    """With no live device_data yet, fall back to the config entry values."""
    remote = _make_remote({"is_on": True})
    remote.coordinator.device_data = {}
    remote._entry.data = {
        "device_id": "001122334455",
        "name": "Configured Name",
        "model": "EntryModel",
        "sw_version": "EntrySW",
    }

    info = remote.device_info
    assert info["model"] == "EntryModel"
    assert info["sw_version"] == "EntrySW"
    assert info["name"] == "Configured Name"
    assert "configuration_url" not in info
