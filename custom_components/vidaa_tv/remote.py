"""Remote platform for Hisense TV."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable

from homeassistant.components.remote import RemoteEntity, RemoteEntityFeature
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_SW_VERSION,
    DEFAULT_NAME,
    ACTIVITY_HOME,
)
from .coordinator import VidaaTVDataUpdateCoordinator

# Import key utilities from the library
from pyvidaa.keys import get_key

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from . import VidaaTVConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV remote from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([VidaaTVRemote(coordinator, entry)])


class VidaaTVRemote(CoordinatorEntity[VidaaTVDataUpdateCoordinator], RemoteEntity):
    """Representation of a Hisense TV remote."""

    _attr_has_entity_name = True
    _attr_name = "Remote"
    _attr_supported_features = RemoteEntityFeature.ACTIVITY

    def __init__(
        self,
        coordinator: VidaaTVDataUpdateCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the remote."""
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = entry.data.get(CONF_DEVICE_ID)
        self._attr_unique_id = f"{self._device_id}_remote" if self._device_id else f"{entry.entry_id}_remote"
        self._apps: list[dict] = []
        self._activity_list: list[str] = []
        # See media_player: without this the TV is re-asked on every update.
        self._activities_probed = False

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        await self._async_update_activities()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # If TV is on and we don't have activities yet, try to fetch them
        if (
            self.coordinator.data
            and self.coordinator.data.get("is_on")
            and not self._activity_list
            and not self._activities_probed
        ):
            self._activities_probed = True
            self.hass.async_create_task(self._async_update_activities())
        super()._handle_coordinator_update()

    async def _async_update_activities(self) -> None:
        """Update activity list from TV."""
        try:
            apps = await self.coordinator.async_get_apps()
            if apps and isinstance(apps, list):
                self._apps = apps
                self._activity_list = [
                    app.get("name")
                    for app in apps
                    if isinstance(app, dict) and app.get("name")
                ]
                _LOGGER.debug("Updated activity list with %d apps", len(self._activity_list))
        except Exception as err:
            _LOGGER.debug("Error updating activities: %s", err)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info, preferring the TV's live values."""
        data = self.coordinator.device_data
        # Stable identity: keep the existing identifier (device_id or entry_id);
        # do not switch to a MAC for existing installs (would orphan the device).
        device_id = self._entry.data.get(CONF_DEVICE_ID) or self._entry.entry_id
        mac_src = data.get("device_id") or self._entry.data.get(CONF_DEVICE_ID)
        mac = self._format_mac(mac_src) if mac_src else None

        info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=data.get("name") or self._entry.data.get(CONF_NAME, DEFAULT_NAME),
            manufacturer="Hisense",
            model=data.get("model") or self._entry.data.get(CONF_MODEL),
            sw_version=data.get("sw_version") or self._entry.data.get(CONF_SW_VERSION),
        )

        if mac:
            info["connections"] = {(CONNECTION_NETWORK_MAC, mac)}
        if data.get("ip"):
            info["configuration_url"] = f"http://{data['ip']}"

        return info

    def _format_mac(self, device_id: str) -> str | None:
        """Format device_id as MAC address."""
        if not device_id or len(device_id) != 12:
            return None
        return ":".join(device_id[i:i+2] for i in range(0, 12, 2)).upper()

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.available

    @property
    def is_on(self) -> bool | None:
        """Return if TV is on.

        Mirrors the media player: an unreachable TV is off, not "whatever it was
        doing last". Without the coordinator.available guard the two entities
        could disagree, since coordinator.data keeps its last value on a failed
        update.
        """
        if not self.coordinator.data or not self.coordinator.available:
            return False
        return self.coordinator.data.get("is_on", False)

    @property
    def current_activity(self) -> str | None:
        """Return current activity (app name, source, or the home screen).

        Guarded like is_on above: coordinator.data keeps its last value when an
        update fails, and reporting the TV off while still naming what it was
        watching is worse than naming nothing.
        """
        data = self.coordinator.data
        if not data or not self.coordinator.available:
            return None
        # The coordinator names the launcher "Home" itself, so it arrives as the
        # source like any other.
        return data.get("app") or data.get("source")

    @property
    def activity_list(self) -> list[str] | None:
        """Return list of activities (Home plus launchable apps)."""
        if not self._activity_list:
            # Still offer Home even before the app list has been fetched.
            return [ACTIVITY_HOME]
        return [ACTIVITY_HOME, *self._activity_list]

    async def async_turn_on(self, activity: str | None = None, **kwargs: Any) -> None:
        """Turn the TV on and optionally start an activity."""
        await self.coordinator.async_turn_on()
        if activity == ACTIVITY_HOME:
            # "Home" is the launcher, not an app - navigate there via the key.
            await self.coordinator.async_send_key(get_key("home"))
        elif activity:
            # Launch the app/activity
            await self.coordinator.async_launch_app(activity)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the TV off."""
        await self.coordinator.async_turn_off()

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        """Send remote commands.

        Supports all keys from the pyvidaa library including:
        - Navigation: up, down, left, right, ok, enter, select
        - Menu: back, return, menu, home, exit
        - Volume: volumeup, volup, vol+, volumedown, voldown, vol-, mute
        - Playback: play, pause, stop, forward, ff, rewind, rw
        - Numbers: 0-9
        - Channels: channelup, chup, ch+, channeldown, chdown, ch-
        - Colors: red, green, yellow, blue
        - Extras: info, subtitle, sub, power
        - Mouse: mouse, zoomin, zoomout
        """
        num_repeats = kwargs.get("num_repeats", 1)
        delay_secs = kwargs.get("delay_secs", 0.2)

        for _ in range(num_repeats):
            for cmd in command:
                # Use the library's key mapping which supports all keys
                key = get_key(cmd)
                await self.coordinator.async_send_key(key)

                if delay_secs > 0:
                    import asyncio
                    await asyncio.sleep(delay_secs)

    async def async_learn_command(self, **kwargs: Any) -> None:
        """Learn a command (not supported)."""
        _LOGGER.warning("Learning commands is not supported on Hisense TV")

    async def async_delete_command(self, **kwargs: Any) -> None:
        """Delete a command (not supported)."""
        _LOGGER.warning("Deleting commands is not supported on Hisense TV")
