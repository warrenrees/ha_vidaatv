"""Data update coordinator for Hisense TV."""

from __future__ import annotations

import ipaddress
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from pyvidaa import APPS
from pyvidaa.wol import wake_tv
from .const import (
    DOMAIN,
    SCAN_INTERVAL,
    STATE_FAKE_SLEEP,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_HW_MAC,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
)

_LOGGER = logging.getLogger(__name__)


def _ipv4_broadcast_subnet(host: str) -> str | None:
    """Return the /24 subnet prefix (e.g. "10.0.0") for an IPv4 host.

    Returns None for hostnames or IPv6 addresses; wake_tv then falls back to
    the global broadcast address.
    """
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv4Address):
            return host.rsplit(".", 1)[0]
    except ValueError:
        pass
    return None


class VidaaTVDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to manage data updates from Hisense TV."""

    def __init__(
        self,
        hass: HomeAssistant,
        tv,  # AsyncVidaaTV
        entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        # Get scan interval from options, with fallback to default
        scan_interval = entry.options.get("scan_interval", SCAN_INTERVAL)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.tv = tv
        self.entry = entry
        self._available = True
        self._device_info_fetched = False
        # Older firmware never answers getdeviceinfo. Each attempt costs the
        # full timeout, so give up after a few misses rather than paying it on
        # every poll forever.
        self._device_info_misses = 0
        self._device_info_unsupported = False
        self._auth_failures = 0
        # Parsed device info (model, sw_version, name, ip, device_id) cached from
        # the TV's getdeviceinfo; entities build their DeviceInfo from this.
        self.device_data: dict[str, Any] = {}

    @property
    def available(self) -> bool:
        """Return if TV is available."""
        return self._available

    async def _async_fetch_device_info(self) -> None:
        """Fetch the TV's device info once and cache it in ``self.device_data``.

        The entities build their ``DeviceInfo`` from this cache. The first
        coordinator refresh runs before the entities/device are created, so the
        cache is ready by the time HA reads ``device_info`` at device creation —
        no after-the-fact device-registry surgery is required (that race is why
        model/firmware previously never showed up).
        """
        if self._device_info_fetched or self._device_info_unsupported:
            return

        try:
            info = await self.tv.async_get_device_info(timeout=5)
        except Exception as err:
            _LOGGER.debug("Error fetching device info: %s", err)
            return

        if not info:
            # Retry a few times - the TV may simply have been off during setup.
            # But older firmware never answers getdeviceinfo at all, and each
            # attempt costs the full timeout on every poll, so stop asking.
            # A reconnect clears this, so a TV that gains the capability (or
            # was merely asleep) is asked again.
            self._device_info_misses += 1
            if self._device_info_misses >= self._MAX_DEVICE_INFO_MISSES:
                self._device_info_unsupported = True
                _LOGGER.debug(
                    "TV did not report device info %d times; not asking again "
                    "until it reconnects", self._device_info_misses,
                )
            else:
                _LOGGER.debug("No device info returned from TV yet")
            return

        if not isinstance(info, dict):
            # The library's request/response matching is not topic-correlated,
            # so an unrelated push (a source list, a volume broadcast) can land
            # here. Ignore it rather than raising out of the whole poll.
            _LOGGER.debug("Ignoring non-dict device info: %r", info)
            return

        self._device_info_misses = 0

        self.device_data = {
            "model": info.get("model_name"),
            "sw_version": info.get("tv_version"),
            "name": info.get("tv_name"),
            "ip": info.get("ip"),
            # network_type is the device id (MAC without colons) per project convention.
            "device_id": info.get("network_type"),
        }
        self._device_info_fetched = True
        _LOGGER.debug("Cached device info: %s", self.device_data)

        # Best-effort: if the device already exists (TV came online after setup),
        # refresh it now so the user need not reload. If it doesn't exist yet
        # (first refresh, before entity setup), that's fine — entity creation
        # applies device_data via DeviceInfo.
        device_registry = dr.async_get(self.hass)
        identifier = self.entry.data.get(CONF_DEVICE_ID) or self.entry.entry_id
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, identifier)}
        )
        if device_entry:
            updates = {}
            if self.device_data["model"] and self.device_data["model"] != device_entry.model:
                updates["model"] = self.device_data["model"]
            if self.device_data["sw_version"] and self.device_data["sw_version"] != device_entry.sw_version:
                updates["sw_version"] = self.device_data["sw_version"]
            if updates:
                device_registry.async_update_device(device_entry.id, **updates)
                _LOGGER.debug("Refreshed existing device %s: %s", device_entry.id, updates)

    # Consecutive empty getdeviceinfo replies before we stop asking.
    _MAX_DEVICE_INFO_MISSES = 3

    # Refresh the access token when it has less than this until expiry.
    _TOKEN_REFRESH_THRESHOLD = 24 * 60 * 60  # 1 day

    async def _async_maybe_refresh_token(self) -> None:
        """Proactively refresh the access token while connected.

        The access token lasts ~7 days; refreshing before it expires keeps a
        continuously-loaded integration authenticated without an HA restart or
        reload. A successful refresh persists a new token, so the expiry check
        stops firing afterwards.
        """
        try:
            status = await self.tv.async_token_status()
            if not status.get("has_token") or status.get("needs_reauth"):
                return
            if status.get("tokenless"):
                # Older firmware issues no token at all - it authorizes the
                # client_id when the PIN is entered. There is nothing to renew,
                # and its zero expiry would otherwise look permanently overdue.
                return
            near_expiry = (
                status.get("access_valid")
                and status.get("access_expires_in", 0) < self._TOKEN_REFRESH_THRESHOLD
            )
            if status.get("needs_refresh") or near_expiry:
                _LOGGER.debug(
                    "Access token near expiry (%ss left), refreshing",
                    status.get("access_expires_in", 0),
                )
                if not await self.tv.async_refresh_token():
                    _LOGGER.debug("Proactive token refresh failed")
        except Exception as err:
            _LOGGER.debug("Token refresh check failed: %s", err)

    @staticmethod
    def _powered_off_data() -> dict[str, Any]:
        """The reading for a TV that is off, as opposed to an update failure."""
        return {
            "is_on": False,
            "state": None,
            "statetype": None,
            "volume": None,
            "is_muted": False,
            "app": None,
            "source": None,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from TV."""
        import time
        start = time.monotonic()

        try:
            # Check connection
            if not self.tv.is_connected:
                _LOGGER.debug("TV disconnected, rebuilding client and reconnecting")
                # Rebuild the client so saved-token status is re-evaluated; an
                # expired access token is then refreshed from the refresh token
                # rather than being replayed and rejected.
                try:
                    await self.tv.async_reset()
                except Exception:
                    pass
                # Try to connect with longer timeout for wake-up scenarios
                connected = await self.tv.async_connect(timeout=5)
                if not connected:
                    # A TV in standby takes its MQTT broker down with it, so
                    # being unreachable is a power state, not a fetch failure.
                    # Raising UpdateFailed here logged an error on every
                    # power-off and dropped the remote entity to unavailable
                    # for as long as the TV stayed off.
                    _LOGGER.debug("TV is not reachable; reporting it as off")
                    return self._powered_off_data()
                _LOGGER.debug("Reconnect took %.2fs", time.monotonic() - start)
                # A reconnect can mean the TV rebooted (e.g. a firmware update),
                # so re-fetch device info to pick up a new firmware version -
                # including on a TV we had given up asking.
                self._device_info_fetched = False
                self._device_info_misses = 0
                self._device_info_unsupported = False

            self._available = True

            # Renew the access token before it lapses while connected.
            await self._async_maybe_refresh_token()

            # Cache device info on first successful connection
            await self._async_fetch_device_info()

            # Get current state
            state_start = time.monotonic()
            state = await self.tv.async_get_state(timeout=3)
            if state is None:
                # State replies are QoS 0, so a single lost message would
                # otherwise flash the TV "off" and straight back on. Ask once
                # more before believing it: a genuinely sleeping TV stays quiet,
                # and this costs nothing while it is awake.
                _LOGGER.debug("No state reply; asking once more before reporting off")
                state = await self.tv.async_get_state(timeout=3)
            _LOGGER.debug("get_state took %.2fs, raw state: %s", time.monotonic() - state_start, state)

            # Determine power state. STATE_FAKE_SLEEP is matched exactly on
            # purpose: fake_sleep_1 is what the TV sends while WAKING, so a
            # prefix match would report it off just as it is switched on.
            is_on = bool(state) and state.get("statetype") != STATE_FAKE_SLEEP

            # Get volume and mute status (only if TV is on)
            # Note: getvolume request may not work on all TVs, but volume is cached
            # from volumechange broadcasts when user changes volume
            volume = None
            is_muted = False
            if is_on:
                try:
                    vol_start = time.monotonic()
                    # Short timeout since TV may not respond to direct volume query
                    volume = await self.tv.async_get_volume(timeout=1)
                    is_muted = self.tv.is_muted
                    _LOGGER.debug("get_volume took %.2fs, volume=%s, muted=%s",
                                 time.monotonic() - vol_start, volume, is_muted)
                except Exception as err:
                    _LOGGER.debug("get_volume failed: %s", err)

            # Build data dict
            # State contains 'statetype' which indicates current activity:
            # - 'app': running an app (has 'name', 'url', 'appId' fields)
            # - 'sourceswitch': watching a source (has 'sourceid', 'sourcename' fields)
            # - 'remote_launcher': at home screen
            # - 'fake_sleep_0': TV is off/sleeping
            statetype = state.get("statetype") if state else None

            # Extract current app or source based on statetype
            app = None
            source = None
            if state:
                if statetype == "app":
                    app_key = state.get("name", "").lower()
                    # Get human-readable name from library's APPS dict
                    if app_key in APPS:
                        app = APPS[app_key].get("name", app_key)
                    else:
                        # Fallback: capitalize first letter
                        app = state.get("name", "").capitalize()
                elif statetype == "sourceswitch":
                    source = state.get("displayname") or state.get("sourcename")

            data = {
                "is_on": is_on,
                "state": state,
                "statetype": statetype,
                "volume": volume,
                "is_muted": is_muted,
                "app": app,
                "source": source,
            }

            _LOGGER.debug("State data: is_on=%s, statetype=%s, volume=%s, app=%s, source=%s",
                         is_on, statetype, volume, app, source)
            _LOGGER.debug("Total update took %.2fs", time.monotonic() - start)
            # A poll got through, so any earlier auth trouble is behind us.
            self._auth_failures = 0
            return data

        except (UpdateFailed, ConfigEntryAuthFailed):
            # Ours already, and already described. Re-wrapping produced the
            # doubled "Error communicating with TV: Failed to connect to TV",
            # and would downgrade a reauth request to a plain update failure.
            self._available = False
            raise

        except Exception as err:
            self._available = False
            # Check for auth-related errors that should trigger reauth
            error_str = str(err).lower()
            if "auth" in error_str or "unauthorized" in error_str or "forbidden" in error_str:
                self._auth_failures += 1
                if self._auth_failures >= 3:
                    _LOGGER.warning("Multiple auth failures, triggering reauthentication")
                    raise ConfigEntryAuthFailed(
                        "Authentication failed. Please re-pair with the TV."
                    ) from err
            raise UpdateFailed(f"Error communicating with TV: {err}") from err

    def _wol_targets(self) -> list[str]:
        """Every MAC worth sending a magic packet to, most specific first.

        A packet only wakes the interface the TV is actually connected on, and
        the TV does not report which that is - so wake all of them rather than
        guessing. An explicit wol_mac still leads, and a packet addressed to an
        absent interface is simply ignored.
        """
        candidates = [
            self.entry.options.get("wol_mac"),
            self.entry.data.get(CONF_HW_MAC),
            self.entry.data.get(CONF_MAC_ETHERNET),
            self.entry.data.get(CONF_MAC_WIFI),
            self.entry.data.get(CONF_DEVICE_ID),
            self.device_data.get("device_id"),
        ]

        targets: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            flat = (raw or "").replace(":", "").replace("-", "").lower()
            if len(flat) != 12 or not all(c in "0123456789abcdef" for c in flat):
                continue
            if flat in seen:
                continue
            seen.add(flat)
            targets.append(":".join(flat[i:i + 2] for i in range(0, 12, 2)))
        return targets

    async def async_turn_on(self) -> None:
        """Turn TV on using WoL and power command."""
        targets = self._wol_targets()
        if targets:
            # Derive a /24 broadcast subnet only for a real IPv4 host.
            subnet = _ipv4_broadcast_subnet(self.entry.data.get(CONF_HOST, ""))
            _LOGGER.debug("Sending WoL to %s", ", ".join(targets))
            for mac in targets:
                await self.hass.async_add_executor_job(wake_tv, mac, subnet)
        else:
            _LOGGER.warning(
                "Skipping Wake-on-LAN: no valid MAC known for this TV. Set a "
                "'wol_mac' in the integration options to enable wake-on-LAN."
            )

        # Also send power on command
        await self.tv.async_power_on()
        await self.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn TV off."""
        # KEY_POWER is a toggle, so the library's power_off() reads the state
        # first to avoid switching a sleeping TV back on. We already polled that
        # state, so send the key directly and skip a round-trip the TV can take
        # seconds to answer.
        if self.data and self.data.get("is_on") is False:
            _LOGGER.debug("TV is already off; not sending power off")
            return

        if self.data:
            await self.tv.async_send_key("KEY_POWER")
        else:
            # No poll has succeeded yet, so let the library decide safely.
            await self.tv.async_power_off()
        await self.async_request_refresh()

    @staticmethod
    def _check_sent(sent: Any, description: str) -> None:
        """Turn a dropped command into a visible error.

        pyvidaa returns False when it could not publish - typically because the
        session is gone. Discarding that made Home Assistant report success for
        a command that never left the process, which is what "the buttons do
        nothing and nothing is logged" looks like from the outside.
        """
        if sent is False:
            raise HomeAssistantError(
                f"Could not send {description} to the TV - it is not reachable"
            )

    async def async_volume_up(self) -> None:
        """Increase volume."""
        self._check_sent(await self.tv.async_volume_up(), "volume up")
        await self.async_request_refresh()

    async def async_volume_down(self) -> None:
        """Decrease volume."""
        self._check_sent(await self.tv.async_volume_down(), "volume down")
        await self.async_request_refresh()

    async def async_mute(self) -> None:
        """Toggle mute."""
        self._check_sent(await self.tv.async_mute(), "mute")
        await self.async_request_refresh()

    async def async_set_volume(self, volume: int) -> None:
        """Set volume level."""
        self._check_sent(await self.tv.async_set_volume(volume), "volume")
        await self.async_request_refresh()

    async def async_select_source(self, source: str) -> None:
        """Select input source."""
        self._check_sent(await self.tv.async_set_source(source), f"source {source}")
        await self.async_request_refresh()

    async def async_send_key(self, key: str) -> None:
        """Send remote key."""
        self._check_sent(await self.tv.async_send_key(key), key)

    async def async_launch_app(self, app_name: str) -> None:
        """Launch app."""
        self._check_sent(await self.tv.async_launch_app(app_name), f"app {app_name}")
        await self.async_request_refresh()

    async def async_get_apps(self) -> list[dict] | None:
        """Get available apps."""
        return await self.tv.async_get_apps()

    async def async_get_sources(self) -> list[dict] | None:
        """Get available sources."""
        return await self.tv.async_get_sources()
