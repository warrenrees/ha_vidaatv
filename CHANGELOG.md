# Changelog

All notable changes to the Vidaa TV Home Assistant integration will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

(Library/protocol changes are tracked separately in the [`pyvidaa`](https://github.com/warrenrees/pyvidaa) repository.)

## [Unreleased]

## [2.2.0] - 2026-08-04

Requires `pyvidaa` 2.3.0.

**If you installed from the repository between 2.1.2 and this release**, the
manifest pinned `pyvidaa==2.2.7` — a version that was declared but never
published to PyPI, so the dependency could not be installed. This release pins
a version that exists.

### Added

- The channel being watched is exposed as the media title and channel number.

### Fixed

- **No source was shown while watching live TV.** The TV reports watching a
  channel as `statetype: livetv`, which was not handled — only `app` and
  `sourceswitch` were — so both the source and the app came back empty for the
  single most common thing a television does. The source now reads `TV`, and
  the channel name and number the TV already sends are exposed as the media
  title and channel. Captured from a live set; three different channels were
  recorded to confirm the payload shape.

## [2.1.2]

### Fixed (second round)

- **The TV showed as On for about two minutes after being switched off, and
  could not be controlled in the meantime.** The library could not tell "the TV
  stopped answering" from "its state has not changed", so Home Assistant kept
  reporting the last thing the TV was doing until the connection finally timed
  out. It now reports Off on the first poll after the TV goes away — and
  usually sooner, because the TV's own standby announcement is now listened
  for, which also catches the set being switched off with its own remote.
- **Commands that never reached the TV reported success.** Pressing a button on
  an unreachable TV did nothing, silently, with nothing in the log — which is
  what "I can no longer control it" looked like. Such commands now raise a
  visible error.
- Each poll no longer wastes several seconds: an update against an idle TV
  drops from ~9s to well under a second, because neither the state request nor
  the device-info request waits out a timeout the TV was never going to answer.
- Wake-on-LAN now targets every MAC the TV reports, not just the Ethernet one —
  so a TV on Wi-Fi wakes without hand-setting **wol_mac** first.
- Turning the TV off no longer re-reads its state first; the integration
  already knows whether it is on.
- Re-authenticating no longer wipes the stored MAC addresses, model and
  firmware — which had been silently disabling Wake-on-LAN.
- The remote entity could report On while the media player reported Off.
- The **wol_mac** option form could fail to submit on entries with no stored
  MAC — the very case where it is needed.
- A TV in standby is now reported as simply **off** rather than as a
  communication error. It takes its MQTT service down with it, so being
  unreachable is a power state — treating it as a failure logged an error every
  time the TV was switched off and made the remote entity unavailable for as
  long as it stayed off.
- A single lost state message can no longer flicker the TV to Off and back; it
  is asked once more before being believed.
- Source and app lists are no longer re-requested on every single update on TVs
  that do not return them, which was two five-second requests per poll forever.
- Repeated authentication blips spread over a long time no longer accumulate
  into a spurious re-authentication prompt; the counter resets after any
  successful poll.

### Added

- The TV's Ethernet and Wi-Fi MAC addresses are now shown on its device page,
  and listed above the **wol_mac** field in the integration options. A
  Wake-on-LAN packet only reaches the interface the TV is actually connected
  on, and the TV does not report which that is — so if turning the TV on does
  not work, set **wol_mac** to the other address.

### Fixed

- **The TV could be turned off from Home Assistant but not back on.** Turning a
  TV on needs Wake-on-LAN — once it is off its MQTT service is gone, so no
  command can reach it — and Wake-on-LAN had no MAC to aim at on TVs that never
  report one via `getdeviceinfo`, which older firmware does not. The TV's real
  MAC is now read from its UPnP descriptor during setup and stored. Existing
  entries backfill it automatically in the background on the next restart; no
  re-pairing needed. If your TV is on Wi-Fi and still will not wake, set
  **wol_mac** in the integration options to its Wi-Fi MAC (Wake-on-LAN over
  Wi-Fi also has to be supported and enabled on the TV).
- Older TVs no longer retry a pointless token refresh on every poll. They issue
  no token at all, so the log filled with "No refresh token available".
- **An already-configured TV was offered again as a new discovery.** Once a TV
  has been paired its entry adopts the TV's own device id, which no longer
  matches the identifier an SSDP announcement carries — so every scan re-offered
  it, and each of those discoveries also opened a connection to a TV the
  integration was already connected to. Discovered TVs are now matched against
  existing entries by address and MAC, which also stops a TV that changed IP
  from being added twice.

### Changed

- Requires `pyvidaa` 2.2.2, which identifies itself to the TV as `pyvidaa`
  rather than `HomeAssistant`. That avoids fighting a Mosquitto-bridge-based
  integration for the same connection. Already-paired TVs keep working as they
  are; only newly-paired ones use the new identifier.

## [2.1.1]

### Fixed

- **Home Assistant asked for a PIN that never appeared on the TV.** An
  abandoned or restarted setup left its connection to the TV open and
  auto-reconnecting. A second attempt then had two clients on the TV, and
  because MQTT requires a broker to drop the older session when a client id is
  reused, they kicked each other roughly once a second — so the pairing request
  never survived long enough for the TV to show its code. Setup now closes its
  connection when the flow is abandoned, and `pyvidaa` gives each connection a
  distinct MQTT client id.
- Setup now waits for the TV to confirm the PIN dialog is on screen instead of
  pausing a second and hoping, so a TV that ignores the request is reported in
  the log rather than silently producing an unenterable form.
- **Older TVs were invisible to discovery.** Their UPnP descriptor omits
  `vidaa_support` entirely, so SSDP discarded them and they had to be added by
  IP. A descriptor carrying `transport_protocol` now counts as proof it is a
  VIDAA TV; unrelated MediaRenderers on the same SSDP type (Sonos and other
  DLNA speakers) publish no such field and are still ignored.

### Changed

- Requires `pyvidaa` 2.2.1.

## [2.1.0]

### Added

- **Support for TVs on older firmware.** These models predate the credential
  scheme newer TVs use, so setup failed at the first connection with
  `not authorized` (MQTT CONNACK code 5) — after the TLS handshake had already
  succeeded, so the certificates were never the problem. Setup now detects
  them and falls back automatically. Such TVs issue no auth token; they
  authorise Home Assistant directly once the PIN is entered, and the pairing is
  stored so it survives restarts.
- **Authentication mode** option (auto / dynamic / static) on the certificates
  step and in the options flow, as an escape hatch for a TV whose reported
  firmware version does not match what it actually accepts. Existing entries
  have no value stored and keep working as "auto".
- The scheme that actually connected is recorded on the config entry, so later
  connections go straight to it instead of retrying ones the TV rejected.

### Changed

- Requires `pyvidaa` 2.2.0.
- Setup skips the UPnP MAC probe when using static authentication, which does
  not derive anything from the MAC — several seconds faster against a slow TV.

## [2.0.5]

### Fixed

- Pairing now uses the TV's real MAC address instead of a freshly generated random one.
  Dynamic-auth credentials are a hash derived from the client's MAC, and the TV recomputes
  that hash from its own hardware MAC to validate the connection — so a random MAC could
  never match. The TV accepted the initial connection and then silently dropped the session
  before the PIN reached the screen, which surfaced as "The TV did not respond" with nothing
  in the log to explain it. The MAC is now read from the SSDP descriptor when the TV was
  discovered, or from a UPnP probe on manual IP entry.
- A leftover authentication token for the host is cleared before a fresh pairing attempt.
  A token from an earlier interrupted attempt (or from before a TV factory reset) made the
  client try to reconnect with credentials the TV no longer honours instead of generating
  new ones, so the PIN prompt never appeared.
- A TV reporting both a wired and a wireless MAC now resolves to the same address whether it
  was found by discovery or by probing. Previously the two paths could pick different
  interfaces, so a TV paired via discovery would fail to re-authenticate later.
- The brand read from the SSDP descriptor is no longer overwritten while resolving the MAC.
  Brand is part of the authentication credentials, so on non-Hisense VIDAA sets (e.g. `tpv`)
  this produced credentials the TV rejected.
- Setting up a TV no longer fails with a generic "cannot connect" when the TV answers
  `getdeviceinfo` but not `gettvinfo`. The device ID also no longer falls back to the TV's IP
  address, which became the entry's unique ID and changed whenever DHCP reassigned the address.

### Changed

- The config flow probes the TV's UPnP descriptor once per setup instead of twice, which
  removes several seconds of delay when a TV is slow to answer or half asleep.

Thanks to @aidinmaxim for diagnosing and reporting the dynamic-auth MAC failure (#6).

## [2.0.4]

### Fixed

- Pairing holds a single connection open across showing the PIN and authenticating it. The TV
  binds the pairing session to that one connection, so authenticating on a fresh connection
  timed out.
- Device info is fetched on a token-authenticated reconnect after pairing succeeds, since the
  TV only serves `getdeviceinfo` on a token-authed session. This is what populates the model
  and firmware version on the newly created device.

## [2.0.3]

### Fixed

- The device now shows the TV's model, firmware version, IP, and MAC. The coordinator caches
  `getdeviceinfo` and the entities build their `DeviceInfo` from it; previously the info was
  fetched during the first refresh (before the device existed in the registry) and never
  applied, so model/firmware stayed blank.
- Pairing no longer re-prompts for a PIN when the TV is briefly slow to return device info
  after a successful authentication. `getdeviceinfo` is retried, and a miss is treated as
  non-fatal — the entry is created and device info is fetched after setup.
- The integration now sets up even when the TV is unreachable (e.g. in deep sleep). Previously
  setup failed with `ConfigEntryNotReady`, so the entities — including the power button that
  sends Wake-on-LAN — were never created and the TV couldn't be woken from Home Assistant.
  The coordinator reconnects on a later poll once the TV is on.
- Wake-on-LAN now also uses the TV's hardware MAC cached from `getdeviceinfo` (not just the
  config entry's `device_id`), so the power button can wake a TV that has been seen online this
  session even when the entry never stored a MAC. (If the entry has no MAC and the TV hasn't
  been reached since the last restart, set a `wol_mac` in the integration options.)

### Changed

- Pairing now distinguishes a rejected PIN ("Invalid PIN") from no response at all
  ("The TV did not respond to the PIN - it may have expired…"), so a timed-out PIN screen
  no longer just looks like a wrong PIN.
- Device info is re-fetched on reconnect, so a firmware update (which reboots the TV) is
  reflected in the device's firmware version without an integration reload.
- Diagnostics now include the coordinator's cached `device_data` (model, firmware, IP) so the
  device info the integration resolved is visible.

## [2.0.0]

Initial release of the Vidaa TV integration as a standalone repository, split out of the
`pyvidaa` project. The integration uses the `pyvidaa` library (from PyPI) for all TV
communication.

### Added

- Home Assistant integration for Hisense/Vidaa Smart TVs — domain `vidaa_tv`, display name
  "Vidaa TV": media player, remote, config flow (SSDP discovery + PIN pairing), diagnostics,
  and repair flows.
- VIDAA brand images (icon/logo) via the local `brand/` folder (HA 2026.3+).
- Remote: shows "Home" as the current activity when the TV is at the launcher.
