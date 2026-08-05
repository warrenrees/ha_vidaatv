"""Media Player platform for Hisense TV."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo, CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_HW_MAC,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
    CONF_MODEL,
    CONF_SW_VERSION,
    DEFAULT_NAME,
)
from .coordinator import VidaaTVDataUpdateCoordinator

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VidaaTVConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hisense TV media player from a config entry."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([VidaaTVMediaPlayer(coordinator, entry)])


class VidaaTVMediaPlayer(CoordinatorEntity[VidaaTVDataUpdateCoordinator], MediaPlayerEntity):
    """Representation of a Hisense TV media player."""

    _attr_device_class = MediaPlayerDeviceClass.TV
    _attr_has_entity_name = True
    _attr_name = None  # Use device name

    _attr_supported_features = (
        MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PLAY_MEDIA
    )

    def __init__(
        self,
        coordinator: VidaaTVDataUpdateCoordinator,
        entry: VidaaTVConfigEntry,
    ) -> None:
        """Initialize the media player."""
        super().__init__(coordinator)
        self._entry = entry
        self._device_id = entry.data.get(CONF_DEVICE_ID)
        self._attr_unique_id = f"{self._device_id}_media_player" if self._device_id else entry.entry_id

        # Source and app caches
        self._sources: list[str] = []
        self._apps: list[dict] = []
        self._source_list: list[str] = []
        # Set once we have asked the TV, so a set that never answers is not
        # re-asked on every single update. Cleared when the list arrives empty
        # after a reconnect is not worth the complexity: a reload re-probes.
        self._sources_probed = False

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

        # Every MAC we know of, so the device page shows them: the user needs
        # them to pick a Wake-on-LAN target when the default one is not the
        # interface the TV is actually connected on.
        connections = {
            (CONNECTION_NETWORK_MAC, value)
            for value in (
                mac,
                self._entry.data.get(CONF_HW_MAC),
                self._entry.data.get(CONF_MAC_ETHERNET),
                self._entry.data.get(CONF_MAC_WIFI),
            )
            if value
        }
        if connections:
            info["connections"] = connections
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
        """Return if entity is available.

        Always available so power button works for WoL even when TV is off.
        """
        return True

    @property
    def state(self) -> MediaPlayerState:
        """Return the state of the TV."""
        if not self.coordinator.data or not self.coordinator.available:
            return MediaPlayerState.OFF

        if self.coordinator.data.get("is_on"):
            return MediaPlayerState.ON
        return MediaPlayerState.OFF

    @property
    def volume_level(self) -> float | None:
        """Return volume level (0.0 to 1.0)."""
        if not self.coordinator.data:
            return None

        volume = self.coordinator.data.get("volume")
        if volume is not None:
            return volume / 100.0
        return None

    @property
    def is_volume_muted(self) -> bool | None:
        """Return if volume is muted."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("is_muted", False)

    @property
    def source(self) -> str | None:
        """Return current source."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("source")

    @property
    def source_list(self) -> list[str]:
        """Return list of available sources."""
        return self._source_list

    @property
    def app_name(self) -> str | None:
        """Return current app name."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("app")

    @property
    def media_title(self) -> str | None:
        """Return the channel name while watching live TV."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("channel_name")

    @property
    def media_channel(self) -> str | None:
        """Return the channel number while watching live TV."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("channel_number")

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        # Fetch sources and apps
        await self._async_update_sources()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # If TV is on and we don't have sources yet, try to fetch them
        if (
            self.coordinator.data
            and self.coordinator.data.get("is_on")
            and not self._source_list
            # Without this latch, a TV that never returns a source list spawns
            # two 5s executor jobs on EVERY coordinator update, forever.
            and not self._sources_probed
        ):
            self._sources_probed = True
            self.hass.async_create_task(self._async_update_sources())
        super()._handle_coordinator_update()

    async def _async_update_sources(self) -> None:
        """Update source list from TV."""
        try:
            sources = await self.coordinator.async_get_sources()
            if sources and isinstance(sources, list):
                self._sources = sources
                self._source_list = []
                for s in sources:
                    if isinstance(s, dict):
                        name = s.get("sourcename", s.get("name", f"Source {s.get('sourceid', '?')}"))
                        self._source_list.append(name)

            apps = await self.coordinator.async_get_apps()
            if apps and isinstance(apps, list):
                self._apps = apps
                # Add app names to source list
                for app in apps:
                    if isinstance(app, dict):
                        name = app.get("name")
                        if name and name not in self._source_list:
                            self._source_list.append(name)

            _LOGGER.debug("Updated source list with %d entries", len(self._source_list))

        except Exception as err:
            _LOGGER.debug("Error updating sources: %s", err)

    async def async_turn_on(self) -> None:
        """Turn the TV on."""
        await self.coordinator.async_turn_on()

    async def async_turn_off(self) -> None:
        """Turn the TV off."""
        await self.coordinator.async_turn_off()

    async def async_volume_up(self) -> None:
        """Increase volume."""
        await self.coordinator.async_volume_up()

    async def async_volume_down(self) -> None:
        """Decrease volume."""
        await self.coordinator.async_volume_down()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute the volume."""
        await self.coordinator.async_mute()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume level (0.0 to 1.0)."""
        await self.coordinator.async_set_volume(int(volume * 100))

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        # Check if it's an app
        for app in self._apps:
            if app.get("name") == source:
                await self.coordinator.async_launch_app(source)
                return

        # Otherwise treat as input source
        await self.coordinator.async_select_source(source)

    async def async_media_play(self) -> None:
        """Send play command."""
        await self.coordinator.async_send_key("KEY_PLAY")

    async def async_media_pause(self) -> None:
        """Send pause command."""
        await self.coordinator.async_send_key("KEY_PAUSE")

    async def async_media_stop(self) -> None:
        """Send stop command."""
        await self.coordinator.async_send_key("KEY_STOP")

    async def async_media_next_track(self) -> None:
        """Send next track command."""
        await self.coordinator.async_send_key("KEY_FAST_FORWARD")

    async def async_media_previous_track(self) -> None:
        """Send previous track command."""
        await self.coordinator.async_send_key("KEY_REWIND")

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs: Any
    ) -> None:
        """Play media - used for launching apps."""
        if media_type == "app":
            await self.coordinator.async_launch_app(media_id)
        elif media_type == "channel":
            # Could implement channel switching here
            pass
