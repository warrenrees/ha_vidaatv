"""Fixtures for Hisense TV tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.vidaa_tv import DEFAULT_BRAND

# Enable pytest-homeassistant-custom-component
pytest_plugins = ["pytest_homeassistant_custom_component"]

from custom_components.vidaa_tv.const import (
    AUTH_MODE_DYNAMIC,
    CONF_CERTFILE,
    CONF_DEVICE_ID,
    CONF_KEYFILE,
    CONF_MAC,
    CONF_MODEL,
    CONF_SW_VERSION,
    DEFAULT_PORT,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations for all tests."""
    yield


@pytest.fixture(autouse=True)
def mock_hw_mac_probe() -> Generator[MagicMock, None, None]:
    """Stub the setup-time hardware-MAC backfill.

    It runs as a background task on any entry without a stored MAC and does a
    real UPnP probe, which the test suite rightly blocks as network access.
    """
    device = MagicMock(
        mac="00:11:22:33:44:55",
        mac_ethernet="00:11:22:33:44:55",
        mac_wifi="66:55:44:33:22:11",
    )
    with patch(
        "custom_components.vidaa_tv.probe_ip", return_value=device
    ) as mock_probe:
        yield mock_probe


def create_mock_config_entry(
    hass: HomeAssistant,
    data: dict | None = None,
    options: dict | None = None,
    entry_id: str = "test_entry_id",
    unique_id: str | None = "001122334455",
    title: str = "Living Room TV",
) -> "MockConfigEntry":
    """Create a mock config entry for testing."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data=data or MOCK_CONFIG_ENTRY_DATA,
        options=options or {},
        entry_id=entry_id,
        unique_id=unique_id,
    )


# Mock device info returned by TV
MOCK_DEVICE_INFO = {
    "tv_name": "Living Room TV",
    "model_name": "H55A6500",
    "network_type": "001122334455",
    "tv_version": "V0000.01.00a.N0816",
    "ip": "192.168.1.100",
    "eth0": "001122334455",
    "wlan0": "665544332211",
}

# Mock TV state
MOCK_TV_STATE = {
    "statetype": "app",
    "name": "netflix",
    "url": "netflix://",
}

MOCK_TV_STATE_OFF = {
    "statetype": "fake_sleep_0",
}

# Default config entry data
MOCK_CONFIG_ENTRY_DATA = {
    CONF_HOST: "192.168.1.100",
    CONF_PORT: DEFAULT_PORT,
    CONF_NAME: "Living Room TV",
    CONF_DEVICE_ID: "001122334455",
    CONF_MAC: "aa:bb:cc:dd:ee:ff",
    CONF_MODEL: "H55A6500",
    CONF_SW_VERSION: "V0000.01.00a.N0816",
    CONF_CERTFILE: "/config/certs/vidaa_client.pem",
    CONF_KEYFILE: "/config/certs/vidaa_client.key",
}


@pytest.fixture
def mock_vidaa_tv() -> Generator[MagicMock, None, None]:
    """Create a mock AsyncVidaaTV client instance."""
    mock_instance = MagicMock()

    # Connection methods
    mock_instance.async_connect = AsyncMock(return_value=True)
    mock_instance.async_disconnect = AsyncMock()
    mock_instance.async_reset = AsyncMock()
    mock_instance.is_connected = True

    # Token lifecycle
    mock_instance.async_token_status = AsyncMock(
        return_value={
            "has_token": True,
            "access_valid": True,
            "access_expires_in": 7 * 24 * 3600,
            "needs_refresh": False,
            "needs_reauth": False,
        }
    )
    mock_instance.async_refresh_token = AsyncMock(return_value=True)

    # Commands return True when published; False now means 'not sent'.
    for _command in (
        "async_send_key", "async_set_source", "async_launch_app",
        "async_set_volume", "async_volume_up", "async_volume_down",
        "async_mute", "async_power_on", "async_power_off",
    ):
        setattr(mock_instance, _command, AsyncMock(return_value=True))

    # Device info methods
    mock_instance.async_get_device_info = AsyncMock(return_value=MOCK_DEVICE_INFO)
    mock_instance.async_get_tv_info = AsyncMock(
        return_value={"deviceid": "001122334455"}
    )

    # State methods
    mock_instance.async_get_state = AsyncMock(return_value=MOCK_TV_STATE)
    mock_instance.async_get_volume = AsyncMock(return_value=50)
    mock_instance.is_muted = False

    # Control methods are stubbed above with return_value=True; a bare
    # AsyncMock() here would return a MagicMock, which reads as "sent" only by
    # accident and would hide a regression in the dropped-command check.

    # App/source methods
    mock_instance.async_get_apps = AsyncMock(
        return_value=[
            {"name": "Netflix", "appId": "netflix"},
            {"name": "YouTube", "appId": "youtube"},
        ]
    )
    # sourceid values are the library's SOURCE_MAP ids (tv "0", hdmi1 "3",
    # hdmi2 "4"), not a 1-based index; a TV entry is included because livetv
    # names its source by sourceid alone.
    mock_instance.async_get_sources = AsyncMock(
        return_value=[
            {"sourceid": "0", "sourcename": "TV", "displayname": "TV Channels"},
            {"sourceid": "3", "sourcename": "HDMI1", "displayname": "HDMI 1"},
            {"sourceid": "4", "sourcename": "HDMI2", "displayname": "HDMI 2"},
        ]
    )

    # Pairing methods
    mock_instance.async_start_pairing = AsyncMock()
    mock_instance.async_authenticate = AsyncMock(return_value=True)

    yield mock_instance


@pytest.fixture
def mock_vidaa_tv_offline() -> Generator[MagicMock, None, None]:
    """Create a mock AsyncVidaaTV client that fails to connect."""
    mock_instance = MagicMock()
    mock_instance.async_connect = AsyncMock(return_value=False)
    mock_instance.async_disconnect = AsyncMock()
    mock_instance.is_connected = False

    yield mock_instance


@pytest.fixture
def mock_config_flow_tv() -> Generator[MagicMock, None, None]:
    """Mock AsyncVidaaTV for config flow tests."""
    probe_device = MagicMock()
    probe_device.brand = DEFAULT_BRAND
    probe_device.mac = "00:11:22:33:44:55"
    # Real values, not mocks: these land in entry.data, which HA serializes.
    probe_device.mac_ethernet = "00:11:22:33:44:55"
    probe_device.mac_wifi = "66:55:44:33:22:11"
    with patch(
        "custom_components.vidaa_tv.config_flow.AsyncVidaaTV", autospec=True
    ) as mock_class, patch(
        "custom_components.vidaa_tv.config_flow.probe_ip",
        return_value=probe_device,
    ), patch(
        # The real delete_token reads and rewrites ./tokens.json, so an
        # unpatched pairing test would clobber a developer's live token.
        "custom_components.vidaa_tv.config_flow.delete_token"
    ):
        mock_instance = mock_class.return_value

        mock_instance.async_connect = AsyncMock(return_value=True)
        mock_instance.async_disconnect = AsyncMock()
        mock_instance.async_get_device_info = AsyncMock(return_value=MOCK_DEVICE_INFO)
        mock_instance.async_get_tv_info = AsyncMock(
            return_value={"deviceid": "001122334455"}
        )
        mock_instance.async_start_pairing = AsyncMock()
        mock_instance.async_authenticate = AsyncMock(return_value=True)
        # The scheme the client reports after connecting; the flow persists it.
        # A bare autospec mock would yield a MagicMock here and store that.
        mock_instance.auth_mode = AUTH_MODE_DYNAMIC

        yield mock_instance


@pytest.fixture
def mock_certs_exist() -> Generator[MagicMock, None, None]:
    """Mock certificate files existing."""
    with patch(
        "custom_components.vidaa_tv.config_flow.check_certs_exist",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def mock_certs_not_exist() -> Generator[MagicMock, None, None]:
    """Mock certificate files not existing."""
    with patch(
        "custom_components.vidaa_tv.config_flow.check_certs_exist",
        return_value=False,
    ) as mock:
        yield mock


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock, None, None]:
    """Mock async_setup_entry."""
    with patch(
        "custom_components.vidaa_tv.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture
def mock_wake_tv() -> Generator[MagicMock, None, None]:
    """Mock wake_tv WoL function."""
    with patch("custom_components.vidaa_tv.coordinator.wake_tv") as mock:
        yield mock
