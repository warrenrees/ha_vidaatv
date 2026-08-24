"""The Hisense TV integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    CONF_HOST,
    CONF_PORT,
    CONF_MAC,
    CONF_DEVICE_ID,
    CONF_AUTH_MODE,
    CONF_BRAND,
    CONF_CERTFILE,
    CONF_HW_MAC,
    CONF_KEYFILE,
    CONF_USE_SSL,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
    DEFAULT_AUTH_MODE,
    DEFAULT_BRAND,
    DEFAULT_PORT,
    DEFAULT_USE_SSL,
    PLATFORMS,
    SERVICE_SEND_KEY,
    SERVICE_LAUNCH_APP,
    ATTR_KEY,
    ATTR_APP,
)
from .coordinator import VidaaTVDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Import from PyPI package (pyvidaa)
from pyvidaa import AsyncVidaaTV, auth_mode_kwargs
from pyvidaa.config import get_storage
from pyvidaa.discovery import probe_ip


@dataclass
class VidaaTVRuntimeData:
    """Runtime data for Hisense TV integration."""

    coordinator: VidaaTVDataUpdateCoordinator
    tv: AsyncVidaaTV
    # The options this setup was built with, so the update listener can tell a
    # real options change from an entry.data write it should ignore.
    options_snapshot: dict[str, Any] = field(default_factory=dict)


type VidaaTVConfigEntry = ConfigEntry[VidaaTVRuntimeData]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the Hisense TV integration."""
    await _async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> bool:
    """Set up Hisense TV from a config entry."""
    host = entry.data[CONF_HOST]
    port = entry.data.get(CONF_PORT, DEFAULT_PORT)
    mac = entry.data.get(CONF_MAC)
    device_id = entry.data.get(CONF_DEVICE_ID)
    brand = entry.data.get(CONF_BRAND, DEFAULT_BRAND)
    certfile = entry.data.get(CONF_CERTFILE)
    keyfile = entry.data.get(CONF_KEYFILE)
    # Entries paired before this option existed default to SSL (their original
    # behavior); plain-MQTT TVs store it False and skip TLS entirely.
    use_ssl = entry.data.get(CONF_USE_SSL, DEFAULT_USE_SSL)
    # An options-flow override wins over the scheme the entry paired with.
    # Entries created before this option existed have neither, and get "auto".
    auth_mode = entry.options.get(
        CONF_AUTH_MODE, entry.data.get(CONF_AUTH_MODE)
    ) or DEFAULT_AUTH_MODE

    _LOGGER.debug("Setting up Hisense TV at %s:%s (auth mode: %s)", host, port, auth_mode)

    # Create the async TV client
    tv = AsyncVidaaTV(
        host=host,
        port=port,
        use_ssl=use_ssl,
        certfile=certfile,
        keyfile=keyfile,
        mac_address=mac or device_id,
        brand=brand,
        enable_persistence=True,
        **auth_mode_kwargs(auth_mode),
    )

    # Best-effort connect. The TV may be in deep sleep (Wake-on-LAN) — don't block
    # setup on it, or the entities (including the power button that sends WoL) would
    # never be created and the TV couldn't be turned on from Home Assistant.
    try:
        if not await tv.async_connect(timeout=10):
            _LOGGER.warning(
                "TV at %s is not reachable (it may be off); setting up anyway so it "
                "can be woken from Home Assistant", host
            )
    except Exception as err:
        _LOGGER.warning("Initial connect to TV at %s failed (it may be off): %s", host, err)

    # Create coordinator for data updates. Use async_refresh (not
    # async_config_entry_first_refresh) so an unreachable TV doesn't abort setup;
    # the coordinator reconnects on a later poll once the TV is on.
    coordinator = VidaaTVDataUpdateCoordinator(hass, tv, entry)
    await coordinator.async_refresh()

    # Store runtime data using the modern pattern
    entry.runtime_data = VidaaTVRuntimeData(
        coordinator=coordinator, tv=tv, options_snapshot=dict(entry.options)
    )

    # Set up platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register update listener for options
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    # Backfill the TV's hardware MAC for entries paired before it was stored.
    # Without it, Wake-on-LAN has nothing to aim at on TVs that never answer
    # getdeviceinfo (older firmware does not), so the TV can be turned off from
    # Home Assistant but not back on. Backgrounded: it must never delay setup,
    # and it simply does nothing while the TV is unreachable.
    if not entry.data.get(CONF_HW_MAC):
        entry.async_create_background_task(
            hass, _async_backfill_hw_mac(hass, entry), "vidaa_tv_hw_mac"
        )

    return True


async def _async_backfill_hw_mac(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> None:
    """Read the TV's real MAC from its UPnP descriptor and store it."""
    try:
        device = await hass.async_add_executor_job(probe_ip, entry.data[CONF_HOST])
    except Exception as err:  # noqa: BLE001 - best effort, retried next reload
        _LOGGER.debug("Could not probe %s for its MAC: %s", entry.data[CONF_HOST], err)
        return

    if not device or not device.mac:
        return

    _LOGGER.debug(
        "Storing MACs for Wake-on-LAN: %s (ethernet %s, wifi %s)",
        device.mac, device.mac_ethernet, device.mac_wifi,
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_HW_MAC: device.mac,
            CONF_MAC_ETHERNET: device.mac_ethernet,
            CONF_MAC_WIFI: device.mac_wifi,
        },
    )


async def _async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the integration."""

    async def async_send_key(call: ServiceCall) -> None:
        """Handle send_key service call."""
        key = call.data[ATTR_KEY]

        # Get all loaded config entries for this domain
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_tvs_configured",
            )

        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                continue
            runtime_data: VidaaTVRuntimeData = entry.runtime_data
            try:
                await runtime_data.coordinator.async_send_key(key)
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={"error": str(err)},
                ) from err

    async def async_launch_app(call: ServiceCall) -> None:
        """Handle launch_app service call."""
        app = call.data[ATTR_APP]

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_tvs_configured",
            )

        for entry in entries:
            if entry.state is not ConfigEntryState.LOADED:
                continue
            runtime_data: VidaaTVRuntimeData = entry.runtime_data
            try:
                await runtime_data.coordinator.async_launch_app(app)
            except Exception as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="command_failed",
                    translation_placeholders={"error": str(err)},
                ) from err

    # Only register services once
    if not hass.services.has_service(DOMAIN, SERVICE_SEND_KEY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SEND_KEY,
            async_send_key,
            schema=vol.Schema({
                vol.Required(ATTR_KEY): cv.string,
            }),
        )

    if not hass.services.has_service(DOMAIN, SERVICE_LAUNCH_APP):
        hass.services.async_register(
            DOMAIN,
            SERVICE_LAUNCH_APP,
            async_launch_app,
            schema=vol.Schema({
                vol.Required(ATTR_APP): cv.string,
            }),
        )


async def async_unload_entry(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        runtime_data = entry.runtime_data
        if runtime_data.tv:
            await runtime_data.tv.async_disconnect()

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: VidaaTVConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Allow a device to be removed from the UI.

    Returning True lets HA delete the device from the device page. Each TV is its
    own config entry, so manual removal of a stale device is always permitted.
    """
    return True


async def async_update_options(hass: HomeAssistant, entry: VidaaTVConfigEntry) -> None:
    """Reload when the options change.

    This listener fires for ANY entry update, including writes to entry.data
    such as the hardware-MAC backfill. Reloading for those would tear the
    integration down and rebuild it mid-setup, so only a genuine options change
    counts.
    """
    runtime = entry.runtime_data
    if runtime is not None and entry.options == runtime.options_snapshot:
        return
    if runtime is not None:
        runtime.options_snapshot = dict(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)
