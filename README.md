# Vidaa TV Integration for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/v/release/warrenrees/ha_vidaatv)](https://github.com/warrenrees/ha_vidaatv/releases)
[![License](https://img.shields.io/github/license/warrenrees/ha_vidaatv)](LICENSE)

Control your Hisense/Vidaa Smart TV from Home Assistant. This integration provides full control including power, volume, input sources, apps, and remote key presses.

> **Disclaimer:** This is an independent, community-maintained project and is not affiliated with, endorsed by, or sponsored by Hisense or VIDAA. "VIDAA" and "Hisense" are trademarks of their respective owners; their use here is purely descriptive.

## Features

- **Auto-discovery** via SSDP - TVs are automatically detected on your network
- **PIN Pairing** - Secure authentication via PIN displayed on TV
- **Media Player** - Full media player entity with power, volume, sources, and apps
- **Remote** - Send any remote key command
- **Services** - Custom services for advanced control
- **Wake-on-LAN** - Turn on TV even when in standby (if supported by your TV)

## Supported TVs

This integration supports Hisense and Vidaa Smart TVs that use MQTT for remote control. This includes:

- Hisense Smart TVs (2018+)
- Vidaa U platform TVs
- Some Toshiba TVs with Vidaa OS

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Click the three dots menu in the top right corner
3. Select "Custom repositories"
4. Add the repository URL: `https://github.com/warrenrees/ha_vidaatv`
5. Select "Integration" as the category
6. Click "Add"
7. Search for "Vidaa TV" in HACS and install it
8. Restart Home Assistant

### Manual Installation

1. Download the latest release from [GitHub](https://github.com/warrenrees/ha_vidaatv/releases)
2. Extract the `custom_components/vidaa_tv` folder
3. Copy it to your Home Assistant's `custom_components` directory
4. Restart Home Assistant

## Configuration

### Automatic Discovery

1. Go to **Settings** → **Devices & Services**
2. If your TV is on the network, it should appear as a discovered device
3. Click **Configure** on the discovered TV
4. A PIN will appear on your TV screen
5. Enter the PIN in Home Assistant
6. Your TV is now set up!

### Manual Setup

1. Go to **Settings** → **Devices & Services**
2. Click **+ Add Integration**
3. Search for "Vidaa TV"
4. Enter your TV's IP address
5. A PIN will appear on your TV screen
6. Enter the PIN in Home Assistant
7. Your TV is now set up!

## Entities

### Media Player

The media player entity provides:

| Feature | Description |
|---------|-------------|
| Power On/Off | Turn TV on or off |
| Volume Up/Down | Adjust volume |
| Volume Set | Set specific volume level (0-100) |
| Mute | Toggle mute |
| Source Select | Switch between HDMI inputs, TV, and apps |
| Play/Pause/Stop | Media playback controls |
| Launch App | Start streaming apps |

### Remote

The remote entity allows sending any key command:

```yaml
service: remote.send_command
target:
  entity_id: remote.living_room_tv_remote
data:
  command:
    - power
    - up
    - ok
```

#### Supported Commands

| Command | Description |
|---------|-------------|
| `power` | Toggle power |
| `up`, `down`, `left`, `right` | Navigation |
| `ok`, `select`, `enter` | Confirm selection |
| `back`, `return` | Go back |
| `home` | Home screen |
| `menu` | Open menu |
| `exit` | Exit current screen |
| `volume_up`, `volume_down` | Volume control |
| `mute` | Toggle mute |
| `play`, `pause`, `stop` | Playback |
| `fast_forward`, `rewind` | Skip forward/back |
| `channel_up`, `channel_down` | Channel control |
| `0`-`9` | Number keys |
| `red`, `green`, `yellow`, `blue` | Color buttons |
| `info` | Show info |
| `subtitle` | Toggle subtitles |

You can also use the raw key codes (e.g., `KEY_POWER`, `KEY_VOLUMEUP`).

## Services

### vidaa_tv.send_key

Send a remote key press to the TV.

```yaml
service: vidaa_tv.send_key
data:
  key: KEY_OK
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `key` | string | Yes | Key code to send (e.g., `KEY_POWER`, `KEY_UP`) |

### vidaa_tv.launch_app

Launch an application on the TV.

```yaml
service: vidaa_tv.launch_app
data:
  app: netflix
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `app` | string | Yes | App name (netflix, youtube, amazon, disney, hulu, tubi) |

### vidaa_tv.type_text

Type text into the TV's focused on-screen keyboard. A text field must already be
focused on the TV (its on-screen keyboard showing) — for example a search box in
an app. Each character is sent as its own keystroke, because the TV has no
bulk-text command.

```yaml
service: vidaa_tv.type_text
data:
  text: the walking dead
  clear_first: 0
  delay: 0.2
```

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `text` | string | Yes | The text to type |
| `delay` | float | No | Seconds between keystrokes (default `0.2`); increase if characters are dropped |
| `clear_first` | int | No | Backspaces to send before typing, to clear an existing value (default `0`) |

## Example Automations

### Turn off TV at midnight

```yaml
automation:
  - alias: "Turn off TV at midnight"
    trigger:
      - platform: time
        at: "00:00:00"
    action:
      - service: media_player.turn_off
        target:
          entity_id: media_player.living_room_tv
```

### Launch Netflix when pressing a button

```yaml
automation:
  - alias: "Netflix button"
    trigger:
      - platform: device
        device_id: your_remote_device
        type: button_short_press
        subtype: button_1
    action:
      - service: vidaa_tv.launch_app
        data:
          app: netflix
```

### Volume control with a dimmer

```yaml
automation:
  - alias: "TV volume from dimmer"
    trigger:
      - platform: state
        entity_id: sensor.dimmer_brightness
    action:
      - service: media_player.volume_set
        target:
          entity_id: media_player.living_room_tv
        data:
          volume_level: "{{ trigger.to_state.state | float / 100 }}"
```

### Remote control macro

```yaml
script:
  tv_navigate_to_settings:
    alias: "Navigate to TV Settings"
    sequence:
      - service: remote.send_command
        target:
          entity_id: remote.living_room_tv_remote
        data:
          command: home
      - delay: 1
      - service: remote.send_command
        target:
          entity_id: remote.living_room_tv_remote
        data:
          command:
            - right
            - right
            - right
            - ok
```

## Lovelace Cards

### Basic Media Control Card

```yaml
type: media-control
entity: media_player.living_room_tv
```

### Custom Button Card for Apps

```yaml
type: horizontal-stack
cards:
  - type: button
    name: Netflix
    icon: mdi:netflix
    tap_action:
      action: call-service
      service: vidaa_tv.launch_app
      data:
        app: netflix
  - type: button
    name: YouTube
    icon: mdi:youtube
    tap_action:
      action: call-service
      service: vidaa_tv.launch_app
      data:
        app: youtube
  - type: button
    name: Prime
    icon: mdi:amazon
    tap_action:
      action: call-service
      service: vidaa_tv.launch_app
      data:
        app: amazon
```

### Custom Remote Card

For a full remote control experience, consider using the [TV Remote Card](https://github.com/marrobHD/tv-card) custom card:

```yaml
type: custom:tv-card
entity: media_player.living_room_tv
remote: remote.living_room_tv_remote
```

## Troubleshooting

### TV not discovered

1. Make sure the TV is powered on (not in standby)
2. Ensure the TV and Home Assistant are on the same network/VLAN
3. Check that SSDP/UPnP is enabled on your router
4. Try manual setup with the TV's IP address

### PIN not appearing on TV

1. Make sure the TV screen is on (not in screensaver mode)
2. Try pressing a button on the TV remote to wake it
3. Restart the TV and try again
4. Check if there's a "Remote App" or "Mobile App" setting in TV settings

### Setup fails with "not authorized" (MQTT code 5)

The TV rejected the credentials. Because this happens *after* the TLS handshake,
the certificates themselves are fine.

The usual cause is **older TV firmware**, which predates the credential scheme
newer models use. Setup detects this and falls back automatically, but you can
force it: pick **Authentication mode → Static** on the certificates step, or
switch an existing TV over in **Settings → Devices & Services → Vidaa TV →
Configure**. These TVs issue no auth token — they authorise Home Assistant
directly once you enter the PIN.

If your TV is not on old firmware, check its **date, time, timezone and DST**
settings: the newer credentials are time-based, and a TV whose clock disagrees
with Home Assistant's rejects them.

### Connection lost after reboot

The integration stores authentication tokens that should persist across reboots. If you experience issues:

1. Check that the TV's IP address hasn't changed (consider setting a static IP or DHCP reservation)
2. Remove and re-add the integration if tokens have expired

### Wake-on-LAN not working

1. Enable Wake-on-LAN in your TV's network settings
2. Make sure the TV is connected via Ethernet (not WiFi) for best WoL support
3. Some TV models don't support WoL

### Commands not working

1. Make sure the TV is fully booted (wait 30 seconds after power on)
2. Try refreshing the entity state
3. Check Home Assistant logs for error messages

## Debug Logging

Enable debug logging for more detailed information:

```yaml
logger:
  default: info
  logs:
    custom_components.vidaa_tv: debug
    pyvidaa: debug
```

## Known Limitations

- Some older Hisense TVs may not be compatible
- Wake-on-LAN depends on TV model and network configuration
- App IDs may vary between TV models/regions
- Some TVs may disconnect after period of inactivity

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Credits

- Protocol reverse-engineered from the Vidaa mobile app
- Inspired by various Hisense TV integration projects
- Brand images derived from the [VIDAA logo](https://commons.wikimedia.org/wiki/File:VIDAA_LOGO.jpg) © VIDAA USA, used under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/), via Wikimedia Commons

## Support

- [GitHub Issues](https://github.com/warrenrees/ha_vidaatv/issues) - Bug reports and feature requests
- [GitHub Discussions](https://github.com/warrenrees/ha_vidaatv/discussions) - General questions and community support
