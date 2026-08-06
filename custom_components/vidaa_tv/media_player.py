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
    ACTIVITY_HOME,
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

# Import key utilities from the library
from pyvidaa.keys import get_key

if TYPE_CHECKING:
    from . import VidaaTVConfigEntry

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

# Consecutive attempts to ask a switched-on TV for its source list before
# leaving it alone. Reset when the TV goes off, so the next power-on tries
# again rather than the entity being stuck with an app-only list forever.
MAX_SOURCE_PROBES = 3


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
        self._sources: list[dict] = []
        self._apps: list[dict] = []
        # Home leads the list before anything has been probed, so the entity
        # always offers at least one source. Consumers that cannot match the
        # current source pick the first entry (HomeKit does exactly this), and
        # "Home" is a far less wrong guess than whichever app sorted first.
        self._source_list: list[str] = [ACTIVITY_HOME]
        # Every name the TV might call a source by - sourcename, displayname,
        # sourceid, app name - lowercased, mapped to the one label that appears
        # in _source_list.
        self._source_aliases: dict[str, str] = {ACTIVITY_HOME.lower(): ACTIVITY_HOME}
        # Counted, not latched: a set that never answers must not spawn two 5s
        # executor jobs on every single update, but one silent reply must not
        # cost the source list for the lifetime of the entity either.
        self._source_probe_attempts = 0

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
        """Return the current source, named as it appears in source_list.

        The TV calls one input three different things - sourcename ("HDMI2"),
        displayname ("PlayStation") and sourceid ("TV") - depending on which
        message you ask. Reporting whichever the state broadcast happened to use
        left the value absent from source_list, and a consumer that cannot match
        it has to fall back to something: HomeKit picks source_list[0], which is
        why it showed Netflix while the TV was on HDMI 2.
        """
        if not self.coordinator.data or not self.coordinator.available:
            return None
        return self._canonical_source(self.coordinator.data.get("source"))

    @property
    def source_list(self) -> list[str]:
        """Return list of available sources."""
        return self._source_list

    @property
    def app_name(self) -> str | None:
        """Return current app name."""
        if not self.coordinator.data or not self.coordinator.available:
            return None
        return self.coordinator.data.get("app")

    @property
    def media_title(self) -> str | None:
        """Return the channel name while watching live TV."""
        if not self.coordinator.data or not self.coordinator.available:
            return None
        return self.coordinator.data.get("channel_name")

    @property
    def media_channel(self) -> str | None:
        """Return the channel number while watching live TV."""
        if not self.coordinator.data or not self.coordinator.available:
            return None
        return self.coordinator.data.get("channel_number")

    def _canonical_source(self, value: str | None) -> str | None:
        """Map any name the TV uses for a source onto its source_list label.

        Unknown names are passed through rather than dropped: a TV whose source
        list we never got is no worse off than before.
        """
        if not value:
            return None
        return self._source_aliases.get(value.strip().lower(), value)

    def _source_command(self, label: str) -> str:
        """Return the name to send to the TV to select ``label``.

        The library maps sourcename to a source id (hdmi2 -> "4"); a display
        name like "HDMI 2" is not in that map and would be published verbatim as
        the source id, which the TV silently ignores.
        """
        for entry in self._sources:
            if not isinstance(entry, dict):
                continue
            if self._source_label(entry) != label:
                continue
            sourcename = entry.get("sourcename")
            if sourcename:
                return str(sourcename)
            sourceid = entry.get("sourceid")
            if sourceid is not None:
                return str(sourceid)
        return label

    @staticmethod
    def _source_label(entry: dict) -> str:
        """Return the name an input is listed under."""
        return str(
            entry.get("displayname")
            or entry.get("sourcename")
            or entry.get("name")
            or f"Source {entry.get('sourceid', '?')}"
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity is added to hass."""
        await super().async_added_to_hass()
        # Fetch sources and apps
        await self._async_update_sources()
        self._learn_current_source()

    def _learn_current_source(self) -> None:
        """Add the input the TV is on if its source list never mentioned it.

        Some sets answer sourcelist with nothing at all, which used to leave
        source_list holding apps and nothing else - so the current input could
        never be matched, and a consumer picking source_list[0] reported the TV
        as being on the first app in the list. The state broadcast names the
        input in every vocabulary the TV has; keep them all, so it can be
        selected again later and not only displayed.
        """
        data = self.coordinator.data
        source = data.get("source") if data else None
        if not source or self._canonical_source(source) in self._source_list:
            return

        raw = data.get("state")
        raw = raw if isinstance(raw, dict) else {}
        if data.get("statetype") in ("sourceswitch", "livetv"):
            # livetv names its input by sourceid alone; that doubles as the
            # sourcename the library maps to a source id.
            learned = {
                "sourceid": raw.get("sourceid"),
                "sourcename": raw.get("sourcename") or raw.get("sourceid"),
                "displayname": raw.get("displayname"),
            }
            self._sources = [*self._sources, {k: v for k, v in learned.items() if v}]
        else:
            # An app, or something the TV can only name one way.
            self._apps = [*self._apps, {"name": source}]

        self._rebuild_source_list()
        _LOGGER.debug("Learned source %r from the TV's state", source)

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._learn_current_source()
        is_on = bool(self.coordinator.data and self.coordinator.data.get("is_on"))
        if not is_on:
            # A TV that is off cannot answer, so those attempts do not count.
            self._source_probe_attempts = 0
        elif (
            (not self._sources or not self._apps)
            and self._source_probe_attempts < MAX_SOURCE_PROBES
        ):
            self._source_probe_attempts += 1
            self.hass.async_create_task(self._async_update_sources())
        super()._handle_coordinator_update()

    async def _async_update_sources(self) -> None:
        """Update source list from TV."""
        try:
            sources = await self.coordinator.async_get_sources()
            if sources and isinstance(sources, list):
                self._sources = sources

            apps = await self.coordinator.async_get_apps()
            if apps and isinstance(apps, list):
                self._apps = apps

            self._rebuild_source_list()
            _LOGGER.debug("Updated source list with %d entries", len(self._source_list))

        except Exception as err:
            _LOGGER.debug("Error updating sources: %s", err)

    def _rebuild_source_list(self) -> None:
        """Rebuild the source list and its alias map from the cached replies.

        Built into locals and swapped in one step: source_list is a state
        attribute, and a consumer watching it rebuilds everything it derived
        from it - HomeKit tears down and recreates the accessory - so it must
        never be observed half full.
        """
        source_list = [ACTIVITY_HOME]
        aliases = {ACTIVITY_HOME.lower(): ACTIVITY_HOME}

        for entry in self._sources:
            if not isinstance(entry, dict):
                continue
            label = self._source_label(entry)
            if label not in source_list:
                source_list.append(label)
            # Whichever of these the TV quotes in a state broadcast, it means
            # this input: sourceswitch sends sourcename/displayname, livetv
            # sends only sourceid.
            for alias in (
                entry.get("sourcename"),
                entry.get("displayname"),
                entry.get("sourceid"),
                label,
            ):
                if alias is not None and str(alias):
                    aliases.setdefault(str(alias).strip().lower(), label)

        for app in self._apps:
            if not isinstance(app, dict):
                continue
            name = app.get("name")
            if name and name not in source_list:
                source_list.append(name)
            if name:
                aliases.setdefault(str(name).strip().lower(), str(name))

        self._source_list = source_list
        self._source_aliases = aliases

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
        """Select input source.

        Accepts any name the TV knows the source by, so automations written
        against the old list ("HDMI2") keep working now that the list is
        labelled with the TV's display names.
        """
        selected = self._canonical_source(source) or source

        if selected == ACTIVITY_HOME:
            # The launcher is not an app - navigate to it with the key.
            await self.coordinator.async_send_key(get_key("home"))
            return

        # Check if it's an app
        for app in self._apps:
            if app.get("name") == selected:
                await self.coordinator.async_launch_app(selected)
                return

        # Otherwise treat as input source
        await self.coordinator.async_select_source(self._source_command(selected))

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
