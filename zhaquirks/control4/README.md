# Control4 ZHA Quirks

ZHA device handler quirks for Control4 Zigbee devices. These quirks allow
Control4 dimmers, switches, fan controllers, outlets, scene controllers, and
IO modules to work with Home Assistant's ZHA integration — no Control4
controller required.

> **Disclaimer:** These quirks were developed through independent reverse
> engineering of Control4's proprietary Zigbee protocol. They are not official
> Control4 software and are not endorsed by or affiliated with Control4
> Corporation. Protocol details are based on observed traffic and may be
> incomplete or inaccurate. This software is provided "as is" without warranty
> of any kind. Use at your own risk.

## Supported Devices

| Model | Type | HA Entities |
|-------|------|-------------|
| C4-APD120 | Adaptive Phase Dimmer | Light (dimmable) |
| C4-4SF120 | 4-Speed Fan Controller | Fan (off / low / med-low / med-high / high) |
| C4-SW120277 | On/Off Wall Switch | Switch |
| C4-KC120277 | 8-Button Scene Controller | 8 event entities (press, hold, release) |
| loz-5s1-w | Dual Switched Outlet | 2 switches (one per outlet) |
| C4-Z2IO-ZP | Zigbee IO Module | 2 switches (relays), 5 binary sensors (contacts), temperature, humidity |
| C4-SR260 | IR/Zigbee Remote (50 buttons + LCD) | 50 event entities (press, release), battery |

All Control4 Zigbee devices use a proprietary text-based serial protocol
layered on top of ZigBee APS instead of standard ZCL clusters. These quirks
translate between that protocol and the ZCL interfaces that ZHA expects.

## Installation

These quirks live inside the `zha-device-handlers` package. To install:

1. Copy the entire `zhaquirks/control4/` directory into your Home Assistant
   custom quirks folder. The default location is:

   ```
   <config>/custom_zha_quirks/control4/
   ```

2. Make sure custom quirks are enabled in your ZHA configuration. In
   `configuration.yaml`:

   ```yaml
   zha:
     custom_quirks_path: /config/custom_zha_quirks
   ```

3. Restart Home Assistant.

ZHA will auto-discover the quirks on startup — no additional configuration is
needed.

## Pairing a Control4 Device

Control4 devices do not use standard Zigbee pairing. They require specific
"magic button press" sequences to reset and identify, and the coordinator must
be physically close to the device during the process.

For a complete reference of button sequences across all Control4 Zigbee
products, see the
[Genesis Technologies definitive guide](https://technet.genesis-technologies.ch/control4-zigbee-the-definitive-guide/).

### Before You Start

- **Move your Zigbee coordinator close to the device.** Control4 devices
  broadcast their identity at low power during pairing. If the coordinator is
  across the house, the device may try to route through another Control4 device
  instead of talking directly to the coordinator, and it will fail to join.

- **If multiple Control4 devices share the same electrical box, pair them all
  at the same time.** A Control4 device that has already joined the network
  will act as a Zigbee router. If you pair one device and then try to pair its
  neighbor, the second device will route through the first rather than
  communicating directly with the coordinator. This causes the join to fail
  because the already-joined device speaks Control4's proprietary protocol to
  the new device instead of relaying standard Zigbee join frames. Pair all
  devices in the same box in a single session to avoid this.

### Button Sequences by Device Type

Control4 devices use two key sequences during pairing: a **factory reset**
(leave Zigbee mesh and clear all settings) and an **identify** (broadcast
identity to the coordinator). The exact button presses depend on the device
type.

**In-wall lighting (dimmers, switches, fan controllers) and 2–3 button
keypads:**

| Action | Sequence |
|--------|----------|
| Identify | 4 x top button |
| Reboot | 15 x top button |
| Reset defaults | 9 x top, 4 x bottom, 9 x top |
| Leave mesh + factory reset | 13 x top, 4 x bottom, 13 x top |

**6-button keypads (e.g. C4-KC120277):**

| Action | Sequence |
|--------|----------|
| Identify | 4 x top-left button |
| Reboot | 15 x top-left button |
| Reset defaults | 9 x top-left, 4 x bottom-left, 9 x top-left |
| Leave mesh + factory reset | 13 x top-left, 4 x bottom-left, 13 x top-left |

**1-button products, relay/contact sensors (e.g. C4-Z2IO-ZP, outlets):**

| Action | Sequence |
|--------|----------|
| Identify | 4 x button |
| Reset defaults | 9 x button (or 15 x for some models) |
| Leave mesh + factory reset | 13 x button |

### Pairing Steps

1. **Factory-reset the device (13-4-13).** For in-wall lighting devices: tap
   the top button 13 times, then the bottom button 4 times, then the top
   button 13 times. For 6-button keypads use top-left and bottom-left. For
   single-button devices tap the button 13 times. The LEDs will flash to
   confirm the reset. This clears any previous network association and puts
   the device into join mode.

2. **Start the join process in Home Assistant.** Go to
   **Settings → Devices & Services → ZHA → Add Device** (or click the
   "Add Zigbee Device" button). ZHA will open the network for new devices.

3. **Trigger a 4-click identify on the device.** Tap the top button (or
   top-left button on 6-button keypads, or the single button on 1-button
   devices) 4 times quickly. The device will broadcast its identity to the
   coordinator. ZHA should discover the device within a few seconds.

4. **Wait for ZHA to finish configuring the device.** The quirk will
   automatically handle the Control4 provisioning handshake (key exchange,
   button/LED configuration, transition times, etc.). This typically takes
   5–15 seconds. The device will appear in ZHA once provisioning completes.

5. **Repeat for any remaining devices in the same box** before moving the
   coordinator away.

### Other Useful Sequences

- **Reboot (15 x top):** Restarts the device without clearing settings.
  Useful if a device becomes unresponsive.

- **Reset defaults (9-4-9):** Resets configuration to factory defaults but
  does not leave the Zigbee mesh. Tap the top button 9 times, bottom 4 times,
  top 9 times.

- **Channel blink (7-4-7):** Makes the device blink its current Zigbee
  channel number on its LEDs. Tap top 7 times, bottom 4 times, top 7 times.
  Useful for verifying a device is on the expected channel.

### Troubleshooting Pairing

- **Device not discovered:** Make sure the coordinator is within a meter or two
  of the device. Try the 13-4-13 factory reset again, wait a few seconds, then
  repeat the 4-click identify.

- **Device joins but shows as "unknown" or has no entities:** The quirk may not
  have matched. Check the ZHA logs for the device's model string (it should be
  something like `c4:control4_light:C4-APD120`). Restart Home Assistant to
  force re-discovery.

- **Second device in the same box won't pair:** This is the routing problem
  described above. Remove the first device from ZHA, then pair both devices at
  the same time. Alternatively, temporarily power off the already-joined device
  by pulling the air-gap switch (if it has one) while pairing the second.

## Device-Specific Notes

### C4-APD120 Dimmer

Exposes a dimmable light entity with on/off and brightness control. The dimmer
uses adaptive phase dimming and reports real-time power telemetry. Physical
button presses on the device are handled locally and the state is synced back
to Home Assistant automatically.

Default transition times: 800 ms ramp-on, 2000 ms ramp-off, default on-level
of ~75%.

### C4-4SF120 Fan Controller

Exposes a fan entity with five speeds: off, low, medium-low, medium-high, and
high. Despite identifying itself as a `control4_light` in the model string,
this is a fan-only device. The quirk maps speed commands to the Control4
`c4.dmx.fsc` protocol. The vestigial `c4.dmx.ls` (light state) announcements
are ignored.

### C4-SW120277 Switch

Exposes a simple on/off switch entity. Physical button presses sync state back
to Home Assistant.

### C4-KC120277 Scene Controller

Exposes 8 event entities (one per button). Each button supports press, hold,
and release actions. The scene controller does not control any load directly —
use Home Assistant automations to map button events to actions.

The keypad has 12 LEDs (buttons 1–12) whose colors can be customized. See
the LED Configuration section below.

### loz-5s1-w Dual Outlet

Exposes two independent switch entities, one per outlet. Each outlet can be
toggled individually.

### C4-SR260 Remote

A 50-button IR / Zigbee remote with an LCD screen. Battery-powered (sleepy
end-device).

The quirk exposes one HA Event entity per physical key (50 entities total)
and a battery sensor. Each press emits a `remote_button_short_press` action
on key-down (the C4 `c4.zr.bb` "button begin" event) followed by a
`remote_button_short_release` on key-up (`c4.zr.be` "button end"). The
device has no separate hold / click-count protocol — to detect a long
press, measure the gap between the two events in your HA automation.

The Event entity names follow the physical layout:

- Top soft / activity row: `room_off`, `watch`, `control4`, `listen`,
  `list`, `i`, `ii`, `iii`
- Nav extras: `guide`, `page_up`, `page_down`, `prev`
- D-pad + rockers: `up` / `down` / `left` / `right` / `select`,
  `volume_up` / `volume_down` / `channel_up` / `channel_down`
- UI cluster: `volume_mute`, `info`, `menu`, `cancel`
- Transport: `reverse` (Rewind), `dvr`, `forward` (Fast Forward),
  `skip_back`, `play`, `skip_forward`, `record`, `pause`, `stop`
- Color buttons: `red` / `green` / `yellow` / `blue`
- Numeric keypad: `digit_0` … `digit_9`, `star`, `hash`

**LCD display message** — the quirk exposes a writable string attribute
(cluster `0xFC47`, attribute `0x0000` = `display_message`) on EP 1.
Writing to this attribute pushes the string to the SR260's LCD via
`c4.ln.dm`; writing an empty string clears the LCD via `c4.ln.le`. The
icon byte (attribute `0x0001` = `display_icon`, default `0x5A`) is
configurable by writing it before the message.

The cached value persists across HA restarts. On every HA / ZHA startup
the quirk re-pushes the cached message to the LCD so the display always
matches whatever was last set, even after the remote sleeps and reboots.
On the very first start the cache is seeded with the device's model
string (`"C4-SR260"`) as a sensible default; write the attribute once to
override it and the new value sticks.

> **Note — there is no UI text entity for this attribute.** ZHA does not
> have a `text` platform, so a writable `CharacterString` attribute on a
> custom cluster does *not* surface as a text input on the device card,
> and no amount of re-pairing will produce one. To write the attribute,
> either call `zha.set_zigbee_cluster_attribute` from an automation /
> script (see below), or bridge an `input_text` helper to that service
> call.

Direct service call:

```yaml
service: zha.set_zigbee_cluster_attribute
data:
  ieee: "00:0f:ff:XX:XX:XX:XX:XX"   # SR260 IEEE
  endpoint_id: 1
  cluster_id: 0xFC47
  cluster_type: in
  attribute: 0                       # display_message
  value: "Doorbell ringing"
```

`input_text` helper bridge (gives you a text input on dashboards):

```yaml
# configuration.yaml
input_text:
  sr260_display:
    name: SR260 LCD message
    initial: "C4-SR260"
    max: 64
```

```yaml
# automations.yaml
- alias: SR260 → push display message
  trigger:
    - platform: state
      entity_id: input_text.sr260_display
  action:
    - service: zha.set_zigbee_cluster_attribute
      data:
        ieee: "00:0f:ff:XX:XX:XX:XX:XX"   # SR260 IEEE
        endpoint_id: 1
        cluster_id: 0xFC47
        cluster_type: in
        attribute: 0
        value: "{{ states('input_text.sr260_display') }}"
```

The remote does not echo the displayed message back, so the cached
value is the only ground truth available for read-back.

**LCD menu / list selection** — the same cluster also exposes two
ZHA cluster commands that drive the SR260's full menu protocol:

| Command id | Name | Args |
|---|---|---|
| `0` | `show_list` | `title` (string), `items` (`|`-separated string), `selected_index` (uint16) |
| `1` | `close_list` | (none) |

`show_list` pushes a paged menu to the LCD: the controller sends
`c4.ln.sl <list_id> <count> <sel> "<title>"`, the remote pages through
the items by sending `c4.ln.gi` requests, and the quirk answers each
page from the cached item list. When the user navigates with the d-pad
and presses **Select**, the quirk fires a `zha_event` of type
`menu_select` carrying the chosen item, then auto-dismisses the menu
with `c4.ln.le`. Pressing any list-dismissing key (Cancel, Control4)
also clears the menu.

Call `show_list` from a HA service:

```yaml
service: zha.issue_zigbee_cluster_command
data:
  ieee: "00:0f:ff:XX:XX:XX:XX:XX"
  endpoint_id: 1
  cluster_id: 64583                  # 0xFC47
  cluster_type: in
  command: 0                          # show_list
  command_type: server
  args: ["What now?", "Watch|Listen|Settings", 0]
```

Listen for the selection in an automation:

```yaml
- alias: SR260 menu → handle selection
  trigger:
    - platform: event
      event_type: zha_event
      event_data:
        device_ieee: "00:0f:ff:XX:XX:XX:XX:XX"
        command: menu_select
  action:
    - service: system_log.write
      data:
        message: >
          SR260 menu_select:
          item={{ trigger.event.data.args.item }},
          index={{ trigger.event.data.args.selected_index }},
          title={{ trigger.event.data.args.title }}
```

`close_list` (command id `1`, no args) dismisses the active menu
without waiting for a user choice.

For the common case of "show a menu and run a different action depending
on which item the user picks", import the
[`c4_sr260_menu_dispatcher.yaml`](blueprints/c4_sr260_menu_dispatcher.yaml)
blueprint. It exposes up to 8 paired item/action slots and a
user-supplied "show trigger", and handles both the show side
(`show_list`) and the dispatch side (`menu_select` event) in one
automation.

**Motion / wake event** — every time the SR260 wakes from sleep
because the user picked it up or moved it, the quirk fires a
`zha_event` with `command: motion_wake`. Hook an automation to it to,
say, turn on a lamp when the remote is grabbed:

```yaml
trigger:
  - platform: event
    event_type: zha_event
    event_data:
      device_id: <SR260 device id>
      command: motion_wake
action:
  - service: light.turn_on
    target:
      entity_id: light.living_room
```

`motion_wake` corresponds to the SR260's `c4.zr.mot` announce — the
remote also sends one shortly after a cold boot / rejoin, so expect an
event right after the device comes online.

### C4-Z2IO-ZP IO Module

A versatile IO module with 2 relay outputs and 5 contact inputs, commonly used
as a garage door controller. Exposes relay switches, contact binary sensors,
and temperature/humidity sensors (if probes are connected).

The module supports multiple IO modes that determine how the relays and
contacts are configured. Mode 1 (2 relays + contacts) is the default for
garage door use.

The external temperature probe returns −40 °C as a sentinel when no probe is
connected. The quirk handles this automatically.

## LED Configuration

Control4 dimmers, switches, and scene controllers have per-button RGB LED
indicators. You can customize the on-color, off-color, and behavior of each
LED using the included Home Assistant scripts.

### Installing the LED Scripts

Copy `ha-scripts/control4_led_scripts.yaml` into your Home Assistant scripts
configuration, or paste its contents into **Settings → Automations & Scenes →
Scripts → Add Script → Edit in YAML**.

### Available Scripts

- **control4_set_led_color** — Set the on/off colors for a single button's
  LED. Takes a target device, button ID (1–12), and two RGB color values.

- **control4_set_all_leds** — Set all button LEDs to the same on/off colors
  in a single command.

- **control4_set_led_mode** — Set the behavioral parameters (mode, behavior,
  color mode) for a single button's LED.

- **control4_set_all_leds_individual** — Set colors for all 12 buttons
  individually in one call.

Colors are specified as 24-bit RGB hex values (e.g., `0x0000FF` for blue,
`0xFF0000` for red, `0x000000` for off).

## Architecture

The quirks are organized as follows:

```
control4/
├── control4_dimmer.py           C4-APD120 quirk
├── control4_fan.py              C4-4SF120 quirk
├── control4_switch.py           C4-SW120277 quirk
├── control4_scene_controller.py C4-KC120277 quirk
├── control4_outlet.py           loz-5s1-w quirk
├── control4_z2io_zp.py          C4-Z2IO-ZP quirk
├── control4_remote.py           C4-SR260 quirk
├── c4_z2io_zp.py                Z2IO-ZP state machine & protocol handler
├── c4_basic_cluster.py          Model/manufacturer resolution for C4 devices
├── c4_button_cluster.py         Button event parsing & state sync
├── c4_display_cluster.py        SR260 LCD-message cluster (0xFC47)
├── c4_led_cluster.py            LED color/mode control (cluster 0xFC43)
├── c4_helpers.py                Constants, frame builders, shared utilities
├── c4_hooks.py                  Monkey-patches for quirk discovery & routing
├── ha-scripts/
│   └── control4_led_scripts.yaml
├── blueprints/                  HA automation blueprints
│   ├── c4_sr260_media_player.yaml      SR260 → media-player + remote
│   └── c4_sr260_menu_dispatcher.yaml   Show a menu, dispatch by selection
└── documentation/               Protocol documentation (from packet captures)
```

### How It Works

Control4 devices communicate using a proprietary ASCII serial protocol
tunneled over ZigBee APS frames on custom profiles (`0xC25C`, `0xC25D`,
`0xC25E`). They do not use standard ZCL clusters for device control.

The quirks work by:

1. **Intercepting packets** — `c4_hooks.py` patches zigpy's packet handling to
   recognize Control4 profiles and route them to the correct quirk clusters.

2. **Translating protocols** — The button and LED clusters parse incoming C4
   ASCII frames (like `sa c4.dmx.ls 00 00 64 ...`) and update the standard
   ZCL attribute caches (OnOff, LevelControl) that ZHA reads.

3. **Sending commands** — When you toggle a light or change brightness in HA,
   the quirk builds C4-format ASCII command frames and sends them over the
   proprietary APS profile.

4. **Caching identity** — `c4_basic_cluster.py` maintains a persistent
   IEEE-to-model map so devices are correctly identified across restarts,
   stored in `/config/.storage/c4_quirk_data.json`.
