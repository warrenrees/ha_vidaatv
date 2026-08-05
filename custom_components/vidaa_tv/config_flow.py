"""Config flow for Hisense TV integration."""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from .const import (
    DOMAIN,
    CONF_AUTH_MODE,
    CONF_DEVICE_ID,
    CONF_HW_MAC,
    CONF_MAC,
    CONF_MAC_ETHERNET,
    CONF_MAC_WIFI,
    CONF_MODEL,
    CONF_BRAND,
    CONF_SW_VERSION,
    CONF_CERTFILE,
    CONF_KEYFILE,
    CONF_USE_SSL,
    AUTH_MODES,
    AUTH_MODE_AUTO,
    AUTH_MODE_STATIC,
    DEFAULT_AUTH_MODE,
    DEFAULT_USE_SSL,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DEFAULT_CERT_DIR,
    DEFAULT_CERT_FILENAME,
    DEFAULT_KEY_FILENAME,
    TIMEOUT_CONNECT,
    TIMEOUT_PIN_DIALOG,
    SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def generate_random_mac() -> str:
    """Generate a random MAC address."""
    return ":".join(f"{random.randint(0, 255):02x}" for _ in range(6))


def _normalize_mac(mac: str | None) -> str | None:
    """Reduce a MAC to bare lowercase hex, or None if it is not one.

    Entries store MACs in whichever form their source used - colon-separated
    from a descriptor, flat from getdeviceinfo - so they have to be compared in
    a single form.
    """
    if not mac:
        return None
    flat = mac.replace(":", "").replace("-", "").lower()
    if len(flat) == 12 and all(c in "0123456789abcdef" for c in flat):
        return flat
    return None

# Import library
import sys
from pathlib import Path

lib_path = Path(__file__).parent.parent.parent
if str(lib_path) not in sys.path:
    sys.path.insert(0, str(lib_path))

from pyvidaa import AsyncVidaaTV, auth_mode_kwargs, delete_token
from pyvidaa.discovery import probe_ip


def auth_mode_selector() -> SelectSelector:
    """Selector for the credential scheme, shared by the setup and options forms."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(AUTH_MODES),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="auth_mode",
        )
    )


def parse_model_description(model_desc: str | None) -> dict[str, str]:
    """Parse an SSDP/UPnP modelDescription blob into its key=value pairs.

    Mirrors pyvidaa's own descriptor parsing (discovery._probe_ip_port) so the
    SSDP path and the UPnP probe path read the descriptor identically.
    """
    fields: dict[str, str] = {}
    for line in (model_desc or "").split("\n"):
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return fields


def mac_from_descriptor(fields: dict[str, str]) -> str | None:
    """Pick the TV's real hardware MAC out of parsed descriptor fields.

    Dynamic-auth credentials are derived from the MAC and the TV recomputes
    the same hash using its own real MAC - a random MAC here will never
    match, so the TV silently drops the session after the initial CONNACK.

    The priority (mac, then macEthernet, then macWifi) and the colon
    formatting deliberately match pyvidaa's probe_ip. A TV that reports more
    than one interface must resolve to the SAME MAC however it was found, or
    an SSDP-discovered TV would pair with one MAC and later reauth - which
    goes through the probe path - with another.
    """
    mac = (
        fields.get("mac")
        or fields.get("macEthernet")
        or fields.get("macWifi")
    )
    if not mac:
        return None
    if ":" not in mac and "-" not in mac and len(mac) == 12:
        mac = ":".join(mac[i:i + 2] for i in range(0, 12, 2))
    return mac


def get_default_cert_paths(hass: HomeAssistant) -> tuple[str, str]:
    """Get default certificate paths in HA config directory."""
    config_dir = Path(hass.config.config_dir)
    cert_dir = config_dir / DEFAULT_CERT_DIR
    certfile = cert_dir / DEFAULT_CERT_FILENAME
    keyfile = cert_dir / DEFAULT_KEY_FILENAME
    return str(certfile), str(keyfile)


def check_certs_exist(certfile: str, keyfile: str) -> bool:
    """Check if certificate files exist and are readable."""
    return (
        os.path.isfile(certfile)
        and os.path.isfile(keyfile)
        and os.access(certfile, os.R_OK)
        and os.access(keyfile, os.R_OK)
    )


async def validate_connection(
    hass: HomeAssistant,
    host: str,
    port: int,
    certfile: str | None = None,
    keyfile: str | None = None,
    mac_address: str | None = None,
    brand: str = "his",
    auth_mode: str = DEFAULT_AUTH_MODE,
    use_ssl: bool = DEFAULT_USE_SSL,
) -> dict[str, Any]:
    """Validate we can connect to the TV."""
    # Resolve the TV's real MAC. Dynamic-auth credentials are a hash of it and
    # the TV recomputes the same hash from its own, so a random one is never
    # accepted past the initial CONNACK. Static auth derives nothing from it,
    # but Wake-on-LAN still needs it to turn the TV back on, so probe either way.
    mac = mac_address
    probed = None
    try:
        probed = await hass.async_add_executor_job(probe_ip, host)
        if not mac and probed and probed.mac:
            mac = probed.mac
    except Exception as err:  # noqa: BLE001 - best effort, falls back below
        _LOGGER.debug("Could not probe MAC for %s: %s", host, err)
    mac_resolved = mac is not None
    if not mac_resolved and auth_mode != AUTH_MODE_STATIC:
        # Dynamic auth cannot build a client_id without one. A random MAC will
        # be rejected, but that failure is clearer than refusing to connect.
        mac = generate_random_mac()

    tv = AsyncVidaaTV(
        host=host,
        port=port,
        use_ssl=use_ssl,
        certfile=certfile,
        keyfile=keyfile,
        mac_address=mac,
        brand=brand,
        enable_persistence=False,
        **auth_mode_kwargs(auth_mode),
    )

    try:
        connected = await tv.async_connect(timeout=TIMEOUT_CONNECT)
        if not connected:
            raise CannotConnect("Failed to connect")

        # Get device info
        device_info = await tv.async_get_device_info(timeout=5)
        tv_info = await tv.async_get_tv_info(timeout=5)

        await tv.async_disconnect()

        result = {
            "name": DEFAULT_NAME,
            "model": None,
            "device_id": None,
            "sw_version": None,
            # The TV's real MAC, if this call managed to resolve one, so the
            # flow can reuse it instead of probing the TV a second time.
            # None when we fell back to a random MAC.
            "mac": mac if mac_resolved else None,
            # The scheme that actually connected. Persisting it means later
            # connects skip the methods this TV already rejected.
            "auth_mode": tv.auth_mode or auth_mode,
            # Both interfaces, so the user can pick the right Wake-on-LAN
            # target if the TV is not on the one we defaulted to.
            "mac_ethernet": probed.mac_ethernet if probed else None,
            "mac_wifi": probed.mac_wifi if probed else None,
        }

        if device_info:
            result["name"] = device_info.get("tv_name", DEFAULT_NAME)
            result["model"] = device_info.get("model_name")
            # async_get_tv_info returns Optional[dict], so it must be guarded.
            # No host fallback: device_id becomes the entry unique_id and the
            # device registry identifier, and an IP churns on DHCP renewal.
            # The entities already fall back to entry_id when it is None.
            result["device_id"] = (
                (tv_info or {}).get("deviceid")
                or device_info.get("network_type")
                or device_info.get("wifi_mac")
                or device_info.get("mac")
            )
            result["sw_version"] = device_info.get("tv_version")

        if tv_info:
            result["device_id"] = result["device_id"] or tv_info.get("deviceid")

        return result

    except Exception as err:
        _LOGGER.error("Error validating connection: %s", err)
        try:
            await tv.async_disconnect()
        except Exception:
            pass
        raise CannotConnect(str(err)) from err


class VidaaTVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hisense TV."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._host: str | None = None
        self._port: int = DEFAULT_PORT
        self._name: str = DEFAULT_NAME
        self._device_id: str | None = None
        self._mac: str = generate_random_mac()  # Placeholder until resolved
        self._mac_resolved: bool = False  # True once _mac is a real hw MAC
        self._model: str | None = None
        self._brand: str | None = None
        self._sw_version: str | None = None
        self._discovery_info: SsdpServiceInfo | None = None
        self._certfile: str | None = None
        self._keyfile: str | None = None
        self._use_ssl: bool = DEFAULT_USE_SSL
        self._auth_mode: str = DEFAULT_AUTH_MODE
        self._mac_ethernet: str | None = None
        self._mac_wifi: str | None = None
        # The connected client that triggered the PIN dialog. The TV ties the
        # pairing session to this one MQTT connection, so authenticate() must
        # run on it - reconnecting with a fresh client loses the session.
        self._pairing_tv: AsyncVidaaTV | None = None

    @callback
    def async_remove(self) -> None:
        """Tear down the held pairing connection when the flow goes away.

        Home Assistant calls this when a flow is aborted or abandoned (the user
        closes the dialog, or restarts setup). Without it the client stays
        connected and keeps auto-reconnecting: a second attempt then has two
        clients on the TV, and because MQTT requires a broker to drop the older
        session when a client id is reused, they kick each other in a loop and
        the PIN never survives long enough to appear.
        """
        tv = self._pairing_tv
        self._pairing_tv = None
        if tv is None:
            return

        async def _close() -> None:
            try:
                await tv.async_disconnect()
            except Exception as err:  # noqa: BLE001 - teardown is best effort
                _LOGGER.debug("Closing abandoned pairing connection failed: %s", err)

        self.hass.async_create_task(_close())

    @callback
    def _async_already_configured(self, host: str, mac: str | None) -> bool:
        """Whether an existing entry is already this TV.

        Matches on host first, then on the hardware MAC so a TV that moved to a
        new IP is still recognised. Deliberately does not use the entry's
        unique_id: that becomes the TV's device_id after pairing, which no
        longer resembles the SSDP USN a discovery arrives with.
        """
        wanted_mac = _normalize_mac(mac)

        for entry in self._async_current_entries(include_ignore=True):
            if entry.data.get(CONF_HOST) == host:
                return True
            if not wanted_mac:
                continue
            known = {
                _normalize_mac(entry.data.get(key))
                for key in (CONF_HW_MAC, CONF_MAC_ETHERNET, CONF_MAC_WIFI, CONF_DEVICE_ID)
            }
            if wanted_mac in known - {None}:
                return True

        return False

    def _remember_mac(self, mac: str | None) -> None:
        """Record a real hardware MAC resolved from the TV.

        Once resolved, later steps skip their own UPnP probe - each probe tries
        both candidate UPnP ports with a 3s timeout apiece, so re-probing costs
        seconds of config-flow latency against a slow or half-awake TV.
        """
        if mac and not self._mac_resolved:
            self._mac = mac
            self._mac_resolved = True

    def _apply_validation_result(self, info: dict[str, Any]) -> None:
        """Copy the fields validate_connection resolved onto the flow."""
        self._name = info.get("name") or self._name
        self._device_id = info.get("device_id")
        self._model = info.get("model")
        self._sw_version = info.get("sw_version")
        self._remember_mac(info.get("mac"))
        self._mac_ethernet = info.get("mac_ethernet") or self._mac_ethernet
        self._mac_wifi = info.get("mac_wifi") or self._mac_wifi
        # Pin "auto" down to whatever actually connected, so pairing and every
        # later connect go straight to it.
        if info.get("auth_mode"):
            self._auth_mode = info["auth_mode"]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step (manual IP entry)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST]
            self._port = user_input.get(CONF_PORT, DEFAULT_PORT)

            # Check for certificates before connecting
            return await self.async_step_certs()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
                }
            ),
            errors=errors,
        )

    async def async_step_certs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle certificate configuration step."""
        errors: dict[str, str] = {}

        # Get default paths
        default_certfile, default_keyfile = get_default_cert_paths(self.hass)

        if user_input is not None:
            self._use_ssl = user_input.get(CONF_USE_SSL, DEFAULT_USE_SSL)
            self._auth_mode = user_input.get(CONF_AUTH_MODE) or DEFAULT_AUTH_MODE

            # Plain MQTT only ever pairs with the fixed static login: "auto"
            # probes a protocol version the plain broker never serves and then
            # falls back to dynamic, which it rejects, and plain dynamic (PIN)
            # TVs do not exist. So plain implies static - the user can just turn
            # SSL off and leave auth on "Auto" without also picking "Static".
            if not self._use_ssl and self._auth_mode == AUTH_MODE_AUTO:
                self._auth_mode = AUTH_MODE_STATIC

            # Plain-MQTT TVs need no client certificates - skip the file check
            # and connect unencrypted. Otherwise require the cert/key pair for
            # the mutual-TLS handshake the encrypted broker expects.
            if self._use_ssl:
                self._certfile = user_input.get(CONF_CERTFILE) or default_certfile
                self._keyfile = user_input.get(CONF_KEYFILE) or default_keyfile
                certs_ok = check_certs_exist(self._certfile, self._keyfile)
            else:
                self._certfile = None
                self._keyfile = None
                certs_ok = True

            # Validate cert paths
            if not certs_ok:
                errors["base"] = "certs_not_found"
            else:
                # Try to connect with certs
                try:
                    info = await validate_connection(
                        self.hass,
                        self._host,
                        self._port,
                        certfile=self._certfile,
                        keyfile=self._keyfile,
                        use_ssl=self._use_ssl,
                        mac_address=self._mac if self._mac_resolved else None,
                        auth_mode=self._auth_mode,
                    )
                    self._apply_validation_result(info)

                    # Set unique ID if we got device_id
                    if self._device_id:
                        await self.async_set_unique_id(self._device_id)
                        self._abort_if_unique_id_configured(
                            updates={CONF_HOST: self._host, CONF_PORT: self._port}
                        )

                    return await self.async_step_pair()

                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"
        else:
            # First time - check if default certs exist
            if check_certs_exist(default_certfile, default_keyfile):
                # Default certs found, use them automatically
                self._certfile = default_certfile
                self._keyfile = default_keyfile
                try:
                    info = await validate_connection(
                        self.hass,
                        self._host,
                        self._port,
                        certfile=self._certfile,
                        keyfile=self._keyfile,
                        mac_address=self._mac if self._mac_resolved else None,
                        auth_mode=self._auth_mode,
                    )
                    self._apply_validation_result(info)

                    if self._device_id:
                        await self.async_set_unique_id(self._device_id)
                        self._abort_if_unique_id_configured(
                            updates={CONF_HOST: self._host, CONF_PORT: self._port}
                        )

                    return await self.async_step_pair()
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected exception")
                    errors["base"] = "unknown"

        # Show certificate configuration form
        cert_dir = Path(self.hass.config.config_dir) / DEFAULT_CERT_DIR
        return self.async_show_form(
            step_id="certs",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_USE_SSL, default=self._use_ssl): bool,
                    vol.Optional(CONF_CERTFILE, default=default_certfile): str,
                    vol.Optional(CONF_KEYFILE, default=default_keyfile): str,
                    vol.Optional(
                        CONF_AUTH_MODE, default=self._auth_mode
                    ): auth_mode_selector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "cert_dir": str(cert_dir),
                "cert_file": DEFAULT_CERT_FILENAME,
                "key_file": DEFAULT_KEY_FILENAME,
            },
        )

    async def async_step_ssdp(
        self, discovery_info: SsdpServiceInfo
    ) -> FlowResult:
        """Handle SSDP discovery."""
        _LOGGER.debug("SSDP discovery: %s", discovery_info)

        # Check for vidaa_support=1 in modelDescription to filter non-Hisense devices
        fields = parse_model_description(discovery_info.upnp.get("modelDescription"))

        # Older firmware omits vidaa_support entirely while still speaking the
        # protocol, so transport_protocol counts as proof too: it comes from
        # the same VIDAA descriptor, and the unrelated MediaRenderers that
        # match this SSDP type (Sonos, DLNA speakers) never publish it.
        if fields.get("vidaa_support") != "1" and not fields.get("transport_protocol"):
            _LOGGER.debug(
                "SSDP device has neither vidaa_support=1 nor transport_protocol, "
                "ignoring: %s", discovery_info.ssdp_headers.get("_host"))
            return self.async_abort(reason="not_vidaa_tv")

        # brand is an auth input (part of client_id/credentials)
        if fields.get("brand"):
            self._brand = fields["brand"]

        # The SSDP descriptor already carries the TV's real MAC - grab it so
        # dynamic-auth credentials match what the TV itself will compute, and
        # so the pairing step doesn't have to probe the TV for it.
        self._remember_mac(mac_from_descriptor(fields))

        # Extract host from discovery
        self._host = discovery_info.ssdp_headers.get("_host") or discovery_info.ssdp_location
        if self._host and "://" in self._host:
            # Extract host from URL
            from urllib.parse import urlparse
            parsed = urlparse(self._host)
            self._host = parsed.hostname

        if not self._host:
            return self.async_abort(reason="no_host")

        self._discovery_info = discovery_info
        self._name = discovery_info.upnp.get("friendlyName", DEFAULT_NAME)

        # Bail out early if this TV is already set up. The unique_id check below
        # is not enough on its own: an entry adopts the TV's device_id as its
        # unique_id once the TV answers getdeviceinfo, so the SSDP USN no longer
        # matches it and the TV gets offered again on every scan - each time
        # also running validate_connection, which opens another connection to a
        # TV that is already connected.
        if self._async_already_configured(self._host, self._mac if self._mac_resolved else None):
            return self.async_abort(reason="already_configured")

        # Try to get unique ID from USN
        usn = discovery_info.ssdp_usn
        if usn:
            # USN format: uuid:XXXX::urn:schemas-upnp-org:...
            if "::" in usn:
                unique_id = usn.split("::")[0].replace("uuid:", "")
            else:
                unique_id = usn.replace("uuid:", "")
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})

        # Check for certificates
        default_certfile, default_keyfile = get_default_cert_paths(self.hass)
        if not check_certs_exist(default_certfile, default_keyfile):
            # No certs found, show cert config form
            return await self.async_step_certs()

        self._certfile = default_certfile
        self._keyfile = default_keyfile

        # Validate connection and get device info
        try:
            info = await validate_connection(
                self.hass,
                self._host,
                self._port,
                certfile=self._certfile,
                keyfile=self._keyfile,
                mac_address=self._mac if self._mac_resolved else None,
                brand=self._brand or "his",
                auth_mode=self._auth_mode,
            )
            self._apply_validation_result(info)

            if self._device_id:
                await self.async_set_unique_id(self._device_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})

        except CannotConnect:
            return self.async_abort(reason="cannot_connect")

        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm the discovered device."""
        if user_input is not None:
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={
                "name": self._name,
                "host": self._host,
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> FlowResult:
        """Handle reauthentication."""
        self._host = entry_data[CONF_HOST]
        self._port = entry_data.get(CONF_PORT, DEFAULT_PORT)
        self._certfile = entry_data.get(CONF_CERTFILE)
        self._keyfile = entry_data.get(CONF_KEYFILE)
        # Entries paired before this option existed have no value stored and
        # default to SSL, matching their original (encrypted) behavior.
        self._use_ssl = entry_data.get(CONF_USE_SSL, DEFAULT_USE_SSL)
        self._device_id = entry_data.get(CONF_DEVICE_ID)
        # Entries paired before this option existed have no value stored.
        self._auth_mode = entry_data.get(CONF_AUTH_MODE) or DEFAULT_AUTH_MODE
        # Carry over what the entry already knows, so reauth refreshes it
        # rather than starting from nothing - the TV is often unreachable at
        # exactly this moment, which is why reauth was triggered.
        self._brand = entry_data.get(CONF_BRAND) or self._brand
        self._model = entry_data.get(CONF_MODEL) or self._model
        self._sw_version = entry_data.get(CONF_SW_VERSION) or self._sw_version
        self._mac_ethernet = entry_data.get(CONF_MAC_ETHERNET)
        self._mac_wifi = entry_data.get(CONF_MAC_WIFI)
        self._mac = generate_random_mac()  # Placeholder; resolved before pairing
        self._mac_resolved = False
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm reauth and trigger pairing."""
        if user_input is not None:
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"host": self._host},
        )

    async def _create_entry_from_current_state(self) -> FlowResult:
        """Persist the config entry from the flow's current device state.

        Shared by the PIN-auth success path and the no-auth path (TVs that
        connect and answer without ever showing a PIN), so both build
        identical entry data. All device fields are read from self, having
        been populated by validation/pairing before this is called.
        """
        new_data = {
            CONF_HOST: self._host,
            CONF_PORT: self._port,
            CONF_NAME: self._name,
            CONF_DEVICE_ID: self._device_id,
            CONF_MAC: self._mac,  # New MAC used for auth
            # Only when it really came from the TV: Wake-on-LAN aimed at a
            # random placeholder wakes nothing.
            CONF_HW_MAC: self._mac if self._mac_resolved else None,
            CONF_MAC_ETHERNET: self._mac_ethernet,
            CONF_MAC_WIFI: self._mac_wifi,
            CONF_MODEL: self._model,
            CONF_BRAND: self._brand or "his",
            CONF_SW_VERSION: self._sw_version,
            CONF_CERTFILE: self._certfile,
            CONF_KEYFILE: self._keyfile,
            CONF_USE_SSL: self._use_ssl,
            CONF_AUTH_MODE: self._auth_mode,
        }

        # Handle reauth - update existing entry
        if self.source == config_entries.SOURCE_REAUTH:
            # Merge, never replace. Reauth re-derives only what it can see, and
            # it usually runs against a TV that is misbehaving or asleep - so a
            # probe miss would otherwise null out the stored MACs, model and
            # firmware, taking Wake-on-LAN down with them.
            entry = self._get_reauth_entry()
            merged = {
                **entry.data,
                **{k: v for k, v in new_data.items() if v is not None},
            }
            return self.async_update_reload_and_abort(entry, data=merged)

        # Set unique ID to prevent duplicates
        if self._device_id:
            await self.async_set_unique_id(self._device_id)
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: self._host, CONF_PORT: self._port}
            )

        # Create the config entry
        return self.async_create_entry(title=self._name, data=new_data)

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle pairing step (PIN entry)."""
        errors: dict[str, str] = {}

        if user_input is not None and self._pairing_tv is not None:
            pin = user_input.get("pin", "")

            # Authenticate on the SAME connection that triggered the PIN. The TV
            # binds the pairing session to that MQTT connection, so a fresh
            # client here would just time out (the session would be gone).
            tv = self._pairing_tv

            try:
                # Time the auth so we can tell a rejected PIN (the TV answers
                # quickly) from no response at all (the PIN screen likely
                # expired or the TV stopped answering).
                auth_start = time.monotonic()
                success = await tv.async_authenticate(pin, timeout=10)
                auth_elapsed = time.monotonic() - auth_start

                if success:
                    # The PIN-authed connection usually won't answer
                    # getdeviceinfo (the TV only serves it on a token-authed
                    # session). Reconnect with the token we just persisted -
                    # the same session the coordinator uses - and fetch there.
                    #
                    # Capturing device_id HERE matters: the entity unique_ids
                    # and the device registry identifier are derived from it.
                    # If the entry is created without it they fall back to the
                    # entry_id, and backfilling device_id later would change
                    # those identifiers and orphan the device/entities. A miss
                    # is still not fatal - we fall back to entry_id and the
                    # coordinator surfaces model/firmware from its own fetch.
                    device_info = None
                    try:
                        await tv.async_reset()
                        if await tv.async_connect(timeout=TIMEOUT_CONNECT):
                            for _attempt in range(3):
                                device_info = await tv.async_get_device_info(timeout=5)
                                if device_info:
                                    break
                                await asyncio.sleep(1)
                    except Exception as err:  # noqa: BLE001 - best effort
                        _LOGGER.debug("Post-auth device info fetch failed: %s", err)
                    await tv.async_disconnect()
                    self._pairing_tv = None

                    if device_info:
                        self._device_id = device_info.get("network_type") or self._device_id
                        self._model = device_info.get("model_name") or self._model
                        self._sw_version = device_info.get("tv_version") or self._sw_version
                        if device_info.get("tv_name"):
                            self._name = device_info.get("tv_name")
                    else:
                        _LOGGER.warning(
                            "Auth succeeded but device info was not returned yet; "
                            "continuing - the integration will fetch it after setup"
                        )

                    return await self._create_entry_from_current_state()

                # Auth failed. Drop the (now stale) session; the block below
                # re-triggers a fresh PIN so the user can try again.
                # is_authenticated distinguishes "PIN accepted but no token"
                # from a plain rejection; timing distinguishes a rejection
                # (fast) from no response (~full timeout).
                authed = tv.is_authenticated
                await tv.async_disconnect()
                self._pairing_tv = None
                if not authed and auth_elapsed < 8:
                    _LOGGER.warning("TV rejected the PIN")
                    errors["base"] = "invalid_pin"
                else:
                    _LOGGER.warning(
                        "No authentication response from TV after %.1fs "
                        "(PIN may have expired or the TV stopped responding)",
                        auth_elapsed,
                    )
                    errors["base"] = "no_auth_response"

            except Exception as err:
                _LOGGER.exception("Error during pairing: %s", err)
                errors["base"] = "pairing_failed"
                try:
                    await tv.async_disconnect()
                except Exception:
                    pass
                self._pairing_tv = None

        # Trigger the PIN dialog and KEEP the connection open so authenticate()
        # (on the next form submission) runs on the same session. We only do
        # this when there's no live pairing session - i.e. on first entry or
        # after a failed attempt that needs a fresh PIN.
        if self._pairing_tv is None:
            # brand and MAC are both auth inputs. SSDP discovery already
            # captured them from the descriptor; for the manual entry path,
            # probe the TV's UPnP descriptor before connecting. The MAC in
            # particular matters: dynamic-auth credentials are a hash of it,
            # and the TV recomputes that hash with its own real MAC, so a
            # random placeholder is silently dropped right after CONNACK.
            # Static auth derives nothing from the MAC, but Wake-on-LAN needs
            # the TV's real one to turn it back on, so resolve it either way.
            if (not self._brand or not self._mac_resolved) and self._host:
                try:
                    device = await self.hass.async_add_executor_job(
                        probe_ip, self._host
                    )
                    # Don't let the probe overwrite a brand SSDP already gave
                    # us: this block now also runs purely to resolve the MAC,
                    # and brand is an auth input - clobbering a discovered
                    # "tpv" with the probe's value breaks pairing outright.
                    if device and device.brand and not self._brand:
                        self._brand = device.brand
                    if device:
                        self._remember_mac(device.mac)
                except Exception as err:  # noqa: BLE001 - best effort, falls back to "his"
                    _LOGGER.debug("Could not probe brand/MAC for %s: %s", self._host, err)

            if not self._mac_resolved and self._auth_mode != AUTH_MODE_STATIC:
                _LOGGER.warning(
                    "Could not resolve %s's real MAC address; falling back to a "
                    "random one for dynamic auth. The TV will likely reject this "
                    "past the initial connect.",
                    self._host,
                )

            # Clear any stale saved token for this host before pairing fresh -
            # a leftover token (from an earlier interrupted attempt, or from
            # before a TV firmware/pairing reset) makes the client try to
            # reconnect/refresh with credentials the TV no longer honours
            # instead of generating new ones, and start_pairing() never runs.
            try:
                await self.hass.async_add_executor_job(
                    delete_token, None, self._host, self._port
                )
            except Exception as err:  # noqa: BLE001 - best effort
                _LOGGER.debug("Could not clear stale token for %s: %s", self._host, err)

            tv = AsyncVidaaTV(
                host=self._host,
                port=self._port,
                use_ssl=self._use_ssl,
                certfile=self._certfile,
                keyfile=self._keyfile,
                mac_address=self._mac,
                brand=self._brand or "his",
                enable_persistence=True,
                **auth_mode_kwargs(self._auth_mode),
            )

            pin_shown = False
            try:
                connected = await tv.async_connect(timeout=TIMEOUT_CONNECT)
                if connected:
                    # "auto" may have landed on a different scheme than the one
                    # we came in with; record it so the entry stores what works.
                    self._auth_mode = tv.auth_mode or self._auth_mode
                    # A TV that needs no authentication (already authorized, or
                    # older firmware with a fixed static login) never shows a
                    # PIN and rejects any code entered - so asking for one would
                    # strand setup. Create the entry straight away, best-effort
                    # enriching it with whatever device info this session serves.
                    if not tv.needs_authentication():
                        device_info = None
                        try:
                            for _attempt in range(3):
                                device_info = await tv.async_get_device_info(
                                    timeout=5
                                )
                                if device_info:
                                    break
                                await asyncio.sleep(1)
                        except Exception as err:  # noqa: BLE001 - best effort
                            _LOGGER.debug("Device info fetch failed: %s", err)
                        if device_info:
                            self._device_id = (
                                device_info.get("network_type") or self._device_id
                            )
                            self._model = (
                                device_info.get("model_name") or self._model
                            )
                            self._sw_version = (
                                device_info.get("tv_version") or self._sw_version
                            )
                            if device_info.get("tv_name"):
                                self._name = device_info.get("tv_name")
                        await tv.async_disconnect()
                        self._pairing_tv = None
                        return await self._create_entry_from_current_state()
                    # Wait for the TV to confirm the dialog is actually on
                    # screen rather than assuming it appeared - otherwise we
                    # ask for a PIN the user cannot see.
                    pin_shown = await tv.async_start_pairing(
                        wait_for_pin=TIMEOUT_PIN_DIALOG
                    )
                    # Hold the connection for the authenticate step either way:
                    # an already-authorized TV shows no PIN, and the entered
                    # code still has to go over this same session.
                    self._pairing_tv = tv
                    if not pin_shown:
                        _LOGGER.warning(
                            "TV at %s did not confirm the PIN dialog; showing "
                            "the form anyway in case it is already paired",
                            self._host,
                        )
                        pin_shown = True
                else:
                    errors["base"] = "cannot_connect"
                    await tv.async_disconnect()
            except Exception as err:
                _LOGGER.warning("Could not trigger PIN dialog: %s", err)
                errors["base"] = "cannot_connect"
                try:
                    await tv.async_disconnect()
                except Exception:
                    pass

            # Only show PIN form if we successfully triggered the dialog
            if not pin_shown and not user_input:
                # Can't show PIN on TV - abort with helpful message
                return self.async_abort(reason="tv_not_responding")

        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema(
                {
                    vol.Required("pin"): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "name": self._name,
                "host": self._host,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return VidaaTVOptionsFlow()


class VidaaTVOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Hisense TV."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get("scan_interval", SCAN_INTERVAL)
        # WoL target: previously-set option, else the TV's real hardware MAC
        # (device_id). Deliberately not CONF_MAC: for entries paired before the
        # MAC is resolved from the TV's descriptor it still holds a random
        # dynamic-auth MAC, and nothing on the entry distinguishes the two.
        # The MAC read from the TV's own descriptor, else the hardware MAC
        # stored as device_id. Deliberately not CONF_MAC: for dynamic auth that
        # may still be a random placeholder. The trailing "" matters - these
        # keys can be present but None, and a None default fails validation
        # when the form is submitted without the field.
        current_wol_mac = (
            self.config_entry.options.get("wol_mac")
            or self.config_entry.data.get(CONF_HW_MAC)
            or self.config_entry.data.get(CONF_DEVICE_ID)
            or ""
        )
        # The escape hatch for a TV whose reported protocol version does not
        # match what it actually accepts. Falls back to the paired-with scheme,
        # then "auto" for entries created before this option existed.
        current_auth_mode = self.config_entry.options.get(
            CONF_AUTH_MODE,
            self.config_entry.data.get(CONF_AUTH_MODE) or DEFAULT_AUTH_MODE,
        )

        # Wake-on-LAN only reaches the interface the TV is actually connected
        # on, and the TV does not say which that is - so show both and let the
        # user try the other one.
        known_macs = []
        for label, key in (
            ("Ethernet", CONF_MAC_ETHERNET),
            ("Wi-Fi", CONF_MAC_WIFI),
        ):
            value = self.config_entry.data.get(key)
            if value:
                known_macs.append(f"{label}: {value}")
        macs_text = (
            "  \n".join(known_macs)
            if known_macs
            else "None found yet - the TV reports them only while it is reachable."
        )

        return self.async_show_form(
            step_id="init",
            description_placeholders={"known_macs": macs_text},
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "scan_interval",
                        default=current_interval,
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=10,
                            max=300,
                            step=5,
                            mode=NumberSelectorMode.SLIDER,
                            unit_of_measurement="seconds",
                        )
                    ),
                    vol.Optional(
                        "wol_mac",
                        default=current_wol_mac,
                    ): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.TEXT)
                    ),
                    vol.Optional(
                        CONF_AUTH_MODE,
                        default=current_auth_mode,
                    ): auth_mode_selector(),
                }
            ),
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""
