# Control4 C4-4SF120 Fan Controller — ZigBee Protocol Documentation

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

Documented from two Wireshark packet captures:

- **Commanded speeds** (`control4-fan-controller-commanded-speeds-1-2-3-4-3-2-1-off.txt`): Operational speed change commands sent from coordinator (0x0000) to fan controller (0xCCCC), cycling through speeds 1→2→3→4→3→2→1→0 (off).
- **After 9-4-9 reset** (`control4-fan-controller-after-9-4-9-reset.txt`): Full provisioning/pairing sequence after a factory reset (9-click, 4-click, 9-click), followed by the device ramping through speeds 4→3→2→1→0 autonomously.

## Device Identity

The SF120 identifies itself via a ZCL Report Attributes broadcast on the C4 manufacturer cluster (captured at t=24.8s in the reset file):

| Attribute | Value |
|-----------|-------|
| Model string | `c4:control4_light:C4-4SF120` |
| Firmware version | `5.1.1` |
| ZigBee short address | 0xCCCC |
| Coordinator address | 0x0000 |

The device type token is `C4-4SF120`. Despite the `control4_light` classification in the model string, this is a 4-speed fan controller and does not use standard ZCL OnOff or LevelControl clusters.

## Network Topology

| Address | Role |
|---------|------|
| 0x0000 | Control4 coordinator (sends commands) |
| 0xCCCC | C4-4SF120 fan controller (receives commands, sends state) |

Communication uses the Control4 proprietary protocol layered on ZigBee, with two key profiles:

- **EP 1→1, profile 0xC25C** (`C4_PROFILE_BUTTON`): Set/Get commands and responses
- **EP 197, profile 0xC25C**: State announcements (device→coordinator, often broadcast)

## Message Format

All Control4 messages use ASCII-encoded commands embedded in the ZigBee payload. The decrypted payload structure is:

**Set command (coordinator→device):**
```
40 01 01 00 5C C2 01 [seq] 30 "s" [chan4] " " [command] 0D 0A
```
- `40` = command frame type (Set)
- `01 01` = source EP 1, destination EP 1
- `00 5C C2` = profile marker (0xC25C = C4_PROFILE_BUTTON)
- `01 [seq]` = sequence byte
- `30` = ASCII '0' (frame prefix)
- `"s"` = Set operation
- `[seq4]` = 4-char hex command sequence number (e.g., "c328")
- `[command]` = ASCII command string
- `0D 0A` = CR+LF terminator

**Response (device→coordinator):**
```
40 C5 01 00 5C C2 C5 [seq] 30 "r" [seq4] " " [response_code]
```
- `C5` = response flag
- `"r"` = Response operation
- `[seq4]` = echoed sequence number from the corresponding Set command
- Response code `000` = success

**State announcement (device→coordinator/broadcast):**
```
... 30 "t" [seq4] " " "sa" " " [namespace] " " [data...] 0D 0A
```
- `"t"` = Tell/announce operation
- `"sa"` = state announcement prefix

## Capture 1 — Operational Speed Commands

This capture shows the coordinator commanding fan speeds through the sequence 1→2→3→4→3→2→1→0. The command/response pattern is consistent throughout.

### Speed Command: `c4.dmx.fsc`

The primary fan speed control command.

**Format:** `c4.dmx.fsc 00 [speed]`

| Speed byte | Fan mode | Description |
|------------|----------|-------------|
| 00 | Off | Fan stopped |
| 01 | Low | Speed 1 |
| 02 | Medium-Low | Speed 2 |
| 03 | Medium-High | Speed 3 |
| 04 | High | Speed 4 (max) |

The `00` byte before the speed is a channel/group index (always 00 for single-output devices).

### Command/Response/Announce Sequence

Each speed change follows this pattern:

1. **Coordinator sends** `c4.dmx.fsc 00 [speed]` (Set, repeated 1–4 times for reliability)
2. **Device sends** Route Record + APS Ack
3. **Device responds** with `000` (success)
4. **Device announces** `sa c4.dmx.fs 00 00 [speed] [rpm_fields...]` confirming the new speed

### Fan State Announcement: `c4.dmx.fs`

**Format:** `sa c4.dmx.fs [ch] [unk] [speed] [field3] [field4] [field5] [field6] [field7] [field8] [field9]`

| Field | Offset | Description |
|-------|--------|-------------|
| ch | 0 | Channel index (always `00`) |
| unk | 1 | Unknown (always `00`) |
| speed | 2 | Current fan speed 00–04 — **this is the key field** |
| field3 | 3 | Fixed value `0087` (device model/capability identifier?) |
| field4 | 4 | RPM or power reading — decreases as fan decelerates toward steady-state |
| field5 | 5 | RPM or power reading — related metric |
| field6 | 6 | RPM or power reading — related metric |
| field7 | 7 | RPM or power reading — increases with speed (highest at speed 4: `03d0`) |
| field8 | 8 | Always `0000` |
| field9 | 9 | Always `0000` |

The device typically sends two `c4.dmx.fs` announcements per speed change: an initial one immediately after the command is acknowledged, and a second ~0.4–0.5s later once the motor has stabilized. Fields 4–7 appear to be real-time motor telemetry (possibly RPM or current measurements) that settle between the two announcements.

#### Steady-State Telemetry by Speed

| Speed | field4 | field5 | field6 | field7 |
|-------|--------|--------|--------|--------|
| 0 (Off) | 0000 | 0000 | 0000 | 0000 |
| 1 (Low) | 0052–0056 | 0003–0004 | 000b–000c | 0129–013d |
| 2 (Med-Low) | 006c–006e | 0007–0008 | 000f | 01f4–0201 |
| 3 (Med-High) | 0094 | 000e | 0014 | 02ab–02ae |
| 4 (High) | 00c0–00c2 | 0019–001a | 001a | 03d0–03d4 |

## Capture 2 — Provisioning After 9-4-9 Reset

After a factory reset, the coordinator initializes the fan controller with a multi-phase sequence. The coordinator address is 0x0000, the fan is 0xCCCC, and a secondary device at 0xDDDD also participates in routing.

### Phase 1: Identity and Reset

| Step | Command | Direction | Description |
|------|---------|-----------|-------------|
| 1 | ZCL Report Attributes (broadcast) | Device→All | Device announces identity: `c4:control4_light:C4-4SF120`, FW `5.1.1` |
| 2 | `c4.dmx.off 0000` | Coord→Device | Initialize fan to off state |
| 3 | Response `000` | Device→Coord | Acknowledged |

### Phase 2: Key Exchange

| Step | Command | Direction | Description |
|------|---------|-----------|-------------|
| 4 | `c4.dmx.key` (Get) | Coord→Device | Request device encryption key |
| 5 | Response: `000 c4.dmx.key 00 00 [REDACTED] [REDACTED] [REDACTED] 00000000 00000000` | Device→Coord | Returns 128-bit key (3 active 32-bit words + 2 zero words) |

### Phase 3: Transition Time Configuration (`c4.dm.tv`)

Nine transition-time values are set, indexed 01 through 09, plus a 10th entry with extended parameters:

| Command | Parameter | Description |
|---------|-----------|-------------|
| `c4.dm.tv 00 01 00` | Index 1, value 00 | Transition time 1 |
| `c4.dm.tv 00 02 00` | Index 2, value 00 | Transition time 2 |
| `c4.dm.tv 00 03 00` | Index 3, value 00 | Transition time 3 |
| `c4.dm.tv 00 04 00` | Index 4, value 00 | Transition time 4 |
| `c4.dm.tv 00 05 00` | Index 5, value 00 | Transition time 5 |
| `c4.dm.tv 00 06 00` | Index 6, value 00 | Transition time 6 |
| `c4.dm.tv 00 07 00` | Index 7, value 00 | Transition time 7 |
| `c4.dm.tv 00 08 00` | Index 8, value 00 | Transition time 8 |
| `c4.dm.tv 00 09 00` | Index 9, value 00 | Transition time 9 |
| `c4.dm.tv 00 0000000a 000007d0` | Extended: param=10, value=2000 | Ramp rate or timeout (0x7D0 = 2000 decimal) |

All return response `000`.

### Phase 4: Initial State Announcements

After transition time configuration, the device sends several state announcements:

| Announcement | Description |
|-------------|-------------|
| `sa c4.dmx.ls 00 00 00 0087 0000 0000 0000 0000 0000 0000` | Light state — all zeros (fan has no light load) |
| `sa c4.dmx.dim 00` | Dim level = 0 (not applicable for fan) |
| `sa c4.dmx.warn 00 0000` | Warning status — no warnings |

### Phase 5: Power Monitoring Timer Interval

| Command | Description |
|---------|-------------|
| `c4.dmx.pmti 0000000a 0000000a` | Power monitoring timer interval: both values = 10 (0x0A) |

### Phase 6: Button/LED Configuration (`c4.dmx.blc`)

Configures button-to-load control mappings. Two channels (00 and 01) are configured with matching patterns:

**Channel 00 button mappings:**

| Command | Button | Value | Interpretation |
|---------|--------|-------|----------------|
| `c4.dmx.blc 00 00 1e` | Button 0 | 0x1E (30) | Min brightness/threshold |
| `c4.dmx.blc 00 01 ff` | Button 1 | 0xFF (255) | Max brightness — top button |
| `c4.dmx.blc 00 02 1e` | Button 2 | 0x1E (30) | Level preset |
| `c4.dmx.blc 00 03 d2` | Button 3 | 0xD2 (210) | Level preset |
| `c4.dmx.blc 00 03 ff` | Button 3 | 0xFF (255) | Max level (alternate) |
| `c4.dmx.blc 00 04 55` | Button 4 | 0x55 (85) | Level preset |
| `c4.dmx.blc 00 04 fe` | Button 4 | 0xFE (254) | Level preset (alternate) |
| `c4.dmx.blc 00 01 e7` | Button 1 | 0xE7 (231) | Adjusted level |
| `c4.dmx.blc 00 05 000000` | Button 5 | Extended | Bottom button off config |

**Channel 01** receives a similar set of mappings with the same button indices but shifted values.

### Phase 7: LED Configuration (`c4.dmx.led`)

Configures LED appearance for 12 buttons (indices 0x00–0x0B). Each button has up to 5 sub-parameters:

| Sub-param | Meaning |
|-----------|---------|
| 00 | LED mode (00 = normal) |
| 01 | LED behavior code (07, 06, 05, 04, 03 for buttons 0–5; 00 for buttons 6–11) |
| 02 | LED color mode (00 = default) |
| 03 | LED on-color (0x0000CC = blue for buttons 0–3; 0x0000FF = bright blue for buttons 4–5) |
| 04 | LED off-color (0x000000 = black/off for all) |
| 05 | LED extended param (0x000000 for buttons 6–11 only) |

Buttons 0–5 (the main speed/control buttons) get full LED configuration with on-colors. Buttons 6–11 (auxiliary/unused) get minimal configuration with all-zero values.

### Phase 8: Ambient Light Query

| Command | Description |
|---------|-------------|
| `c4.dmx.amb 01` (Get) | Query ambient light sensor, channel 01 |

## State Announcement Summary

All state announcements use the `sa` (state announce) prefix and the `0t[chan]` tell frame format:

| Namespace | Format | Description |
|-----------|--------|-------------|
| `c4.dmx.fs` | `00 00 [speed] [field3..9]` | Fan speed + telemetry (primary state) |
| `c4.dmx.ls` | `00 00 [level] [field3..9]` | Light state (always zero on fan) |
| `c4.dmx.dim` | `[level]` | Dim level (not applicable for fan) |
| `c4.dmx.warn` | `[ch] [code]` | Warning/fault status |
| `c4.dmx.bp` | `[button_id]` | Button press event |
| `c4.dmx.sc` | `[step]` | Speed change step number |
| `c4.dmx.cc` | `[button_id] [click_count]` | Click confirmation |

## Command Summary

All commands sent on C4_PROFILE_BUTTON (0xC25C), EP 1→1:

| Command | Type | Description |
|---------|------|-------------|
| `c4.dmx.fsc 00 [speed]` | Set | Set fan speed (00–04) |
| `c4.dmx.off 0000` | Set | Turn off / initialize to off |
| `c4.dmx.key` | Get | Query encryption key |
| `c4.dm.tv 00 [idx] [val]` | Set | Set transition time parameter |
| `c4.dmx.pmti [a] [b]` | Set | Set power monitoring timer interval |
| `c4.dmx.blc [ch] [btn] [val]` | Set | Configure button-to-load control mapping |
| `c4.dmx.led [btn] [sub] [val]` | Set | Configure LED appearance |
| `c4.dmx.amb [ch]` | Get | Query ambient light sensor |

## Key Implementation Notes

1. **Speed commands are idempotent** — the coordinator retransmits `c4.dmx.fsc` 1–4 times per speed change for reliability. The device responds to each with `000` and only changes speed once.

2. **Two announcements per speed change** — the first `c4.dmx.fs` announcement arrives immediately (~30ms after ack); a second arrives ~0.4–0.5s later with stabilized telemetry. The speed field (offset 2) is the same in both; only the telemetry fields change.

3. **Command sequence numbers increment monotonically** — each Set command uses an incrementing 16-bit sequence number (e.g., c328, c329, c32a...) embedded in the ASCII frame. The device echoes this sequence number in its response.

4. **The `c4.dmx.ls` announcement is vestigial** — the fan controller shares its firmware lineage with the dimmer (`control4_light`) and sends `c4.dmx.ls` with all-zero values. This should be ignored for fan-mode operation.

5. **The 9 `c4.dm.tv` parameters plus the extended 10th** configure transition timing behavior. Setting them all to 00 (with the extended value at 0x7D0 = 2000) gives instant speed transitions. These parameters are set once during provisioning and do not need to be sent for operational speed changes.

6. **Button events (`bp`, `sc`, `cc`) during the speed ramp-down** suggest the device internally simulates button presses as it self-tests. The `bp` value corresponds to the button index (0–4 maps to speeds 4→0 in descending order), and `cc` confirms each "click" was processed.
