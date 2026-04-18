# Control4 C4-APD120 Adaptive Phase Dimmer — ZigBee Protocol Documentation

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

Documented from two Wireshark packet captures:

- **After 9-4-9 reset** (`control4-dining-room-light-after-reset-full.txt`): Full provisioning/pairing sequence after a factory reset (9-click, 4-click, 9-click), including identity broadcast, key exchange, button/LED configuration, transition time queries, and state announcements.
- **Physical button press** (`dining-room-lights-physical-button-press.txt`): Operational behavior when the physical buttons on the dimmer are pressed (on, off), showing button events, light state announcements, and click confirmations.

## Device Identity

The APD120 identifies itself via a ZCL Report Attributes broadcast on the C4 manufacturer cluster (0xC25D) immediately after reset. The broadcast is repeated across multiple network relays for reliability.

| Attribute ID | Type | Value | Description |
|-------------|------|-------|-------------|
| 0x0007 | String | `c4:control4_light:C4-APD120` | Model identification string |
| 0x0004 | String | `5.1.1` | Firmware version |
| 0x0005 | uint8 | `6` | Device capability flags |
| 0x0006 | uint16 | `0x003F` (63) | Device type / feature mask |

The device type token is `C4-APD120`. The model string prefix `control4_light` indicates this is a lighting device (dimmer), distinguishing it from `control4_fan` devices like the SF120.

## Network Topology

| Address | Role |
|---------|------|
| 0x0000 | Control4 coordinator (sends commands) |
| 0xAAAA | C4-APD120 dimmer (receives commands, sends state) |

Communication uses the Control4 proprietary protocol layered on ZigBee, with two profiles:

- **EP 1→1, profile 0xC25C** (`C4_PROFILE_BUTTON`): Set/Get commands and responses
- **EP 197, profile 0xC25C**: State announcements (device→coordinator)

## Message Format

All Control4 messages use ASCII-encoded commands embedded in the ZigBee payload. The decrypted payload structure is:

**Set command (coordinator→device):**
```
40 01 01 00 5C C2 01 [seq] 30 "s" [seq4] " " [command] 0D 0A
```
- `40` = command frame type (Set)
- `01 01` = source EP 1, destination EP 1
- `00 5C C2` = profile marker (0xC25C)
- `01 [seq]` = sequence byte
- `30` = ASCII `0` (frame prefix)
- `"s"` = Set operation
- `[seq4]` = 4-char hex command sequence number (e.g., `88f1`)
- `[command]` = ASCII command string
- `0D 0A` = CR+LF terminator

**Get command (coordinator→device):**
```
40 01 01 00 5C C2 01 [seq] 30 "g" [seq4] " " [command] 0D 0A
```
- `"g"` = Get operation (queries a value; device returns data in the response)

**Interrupt/immediate command (coordinator→device):**
```
40 01 01 00 5C C2 01 [seq] 30 "i" [seq4] " " [command] 0D 0A
```
- `"i"` = Interrupt/immediate operation (e.g., `c4.dmx.off`)

**Response (device→coordinator):**
```
40 C5 01 00 5C C2 C5 [seq] 30 "r" [seq4] " " [response_code] [data...] 0D 0A
```
- `C5` = response flag
- `"r"` = Response operation
- `[seq4]` = echoed sequence number from the corresponding command
- Response code `000` = success
- `[data...]` = optional response payload for Get commands

**State announcement / Tell (device→coordinator):**
```
40 C5 01 00 5C C2 C5 [seq] 30 "t" [seq4] " " "sa" " " [namespace] " " [data...] 0D 0A
```
- `"t"` = Tell/announce operation
- `"sa"` = state announcement prefix
- Sent on EP 197, often duplicated for reliability

## Capture 1 — Provisioning After 9-4-9 Reset

After a factory reset, the coordinator initializes the dimmer with a multi-phase sequence. The full provisioning takes approximately 13 seconds of active communication (t=29.9s to t=43.4s), followed by periodic heartbeats.

### Phase 1: Identity Broadcast (t≈29.9s)

The device broadcasts its identity via ZCL Report Attributes, repeating across multiple network paths. The identity broadcast occurs approximately 16 times across various relay nodes.

| Step | Direction | Description |
|------|-----------|-------------|
| 1 | Device→Broadcast | Report Attributes: model `c4:control4_light:C4-APD120`, FW `5.1.1` |
| 2 | (repeated ×16) | Same identity broadcast via different relay nodes |

### Phase 2: Initial Off Command + System Config (t≈30.0s)

| Seq | Command | Direction | Description |
|-----|---------|-----------|-------------|
| 88f1 | `c4.dmx.off 0000` | Coord→Device (Set) | Initialize dimmer to off state |
| 88f1 | `000` | Device→Coord (Resp) | Acknowledged |
| 88f2 | `c4.sy.zpw 00` | Coord→Device (Set) | System ZigBee power setting |
| 88f2 | `000` | Device→Coord (Resp) | Acknowledged |
| 88f3 | `c4.dmx.plm 00` | Coord→Device (Set) | Phase load mode = 0 (auto-detect) |
| 88f3 | `000` | Device→Coord (Resp) | Acknowledged |

### Phase 3: Key Exchange (t≈30.4–36.6s)

The coordinator requests the device's encryption key twice (first attempt at t=30.4s appears to fail or get no data response; second attempt at t=36.4s succeeds).

| Seq | Command | Direction | Description |
|-----|---------|-----------|-------------|
| 88f4 | `c4.dmx.key` | Coord→Device (Get) | Request encryption key (1st attempt) |
| — | (no data response observed) | — | Identity broadcasts continue |
| 88f5 | `c4.dmx.key` | Coord→Device (Get) | Request encryption key (2nd attempt) |
| 88f5 | `000 c4.dmx.key 00 00 [REDACTED] [REDACTED] [REDACTED] 00000000 00000000` | Device→Coord (Resp) | Returns key data |

The key response format: `000 c4.dmx.key [ch] [unk] [word1] [word2] [word3] [word4] [word5]`
- Words 1–3: 32-bit key segments (128-bit key total, 96 bits active)
- Words 4–5: Always `00000000` (padding/reserved)

### Phase 4: Button-to-Load Control Configuration (`c4.dmx.blc`) (t≈36.7–38.2s)

Configures how physical button presses map to dimmer levels. Two channels (00 = primary, 01 = secondary/companion) are configured.

**Channel 00 (primary dimmer) button mappings:**

| Seq | Button | Value | Interpretation |
|-----|--------|-------|----------------|
| 88f6 | 01 | `ff` (255) | Top button → full brightness |
| 88f7 | 03 | `ff` (255) | Button 3 → full brightness |
| 88f8 | 04 | `fe` (254) | Button 4 → near-max level |
| 88f9 | 00 | `1e` (30) | Button 0 → minimum dim level |
| 88fa | 04 | `55` (85) | Button 4 → 33% level (preset) |
| 88fb | 01 | `e7` (231) | Top button → 91% level (adjusted) |
| 88fc | 02 | `1e` (30) | Button 2 → minimum dim level |
| 88fd | 03 | `d2` (210) | Button 3 → 82% level (preset) |
| 8905 | 05 | `000000` | Bottom button → off (extended format) |

During this phase, the device asynchronously sends:

| Seq | Announcement | Description |
|-----|-------------|-------------|
| eb4e | `sa c4.dmx.warn 00 0000` | Warning status: no warnings |
| eb4f | `sa c4.dmx.ls 00 00 00 007c 0000 0000 0000 0000 0000 0000` | Light state: off, device model `007c` |
| eb50 | `sa c4.dmx.dim 02` | Dim mode = 02 (adaptive phase) |

**Channel 01 (companion) button mappings:**

| Seq | Button | Value |
|-----|--------|-------|
| 88fe | 01 | `ff` |
| 88ff | 03 | `ff` |
| 8900 | 00 | `1e` |
| 8901 | 01 | `c8` (200) |
| 8902 | 02 | `1e` |
| 8903 | 03 | `d2` |
| 8904 | 04 | `55` |

### Phase 5: LED Configuration (`c4.dmx.led`) (t≈38.3–42.0s)

Configures LED appearance for 12 button indicators (indices 0x00–0x0B). Each button has three sub-parameters initially set, followed by extended color parameters.

**Sub-parameters 00–02 (mode/behavior/color-mode):**

| Button Range | Sub 00 (Mode) | Sub 01 (Behavior) | Sub 02 (Color Mode) |
|-------------|---------------|-------------------|---------------------|
| 00–03 | 00 (normal) | 00 (off) | 00 (default) |
| 04 | 00 | 01 (on indicator) | 02 (custom color) |
| 05 | 00 | 00 | 00 |
| 06–0B | 00 | 00 | 00 |

**Sub-parameters 03–04 (on-color / off-color), 6-byte extended format:**

| Button | Sub 03 (On-Color) | Sub 04 (Off-Color) | Notes |
|--------|-------------------|---------------------|-------|
| 00 | `000000` | `000000` | LEDs off |
| 01 | `0000ff` | `000000` | Blue when on |
| 02 | `000000` | `000000` | |
| 03 | `000000` | `000000` | |
| 04 | `000000` | `0000ff` | Blue when off |
| 05 | `000000` | `000000` | |

### Phase 6: Second Provisioning Round (t≈199–225s)

After ~3 minutes of idle heartbeats, the coordinator initiates a second provisioning round, likely after the device has been fully accepted into the network.

**Off command + Key re-exchange:**

| Seq | Command | Direction | Description |
|-----|---------|-----------|-------------|
| 8937 | `c4.dmx.off` | Coord→Device (Interrupt) | Ensure dimmer is off |
| 8937 | `000` | Device→Coord (Resp) | Acknowledged |
| 8938 | `c4.dmx.key` | Coord→Device (Get) | Re-query encryption key |
| 8938 | `000 c4.dmx.key 00 00 [REDACTED] [REDACTED] [REDACTED] 00000000 00000000` | Device→Coord (Resp) | Key data (note: word 1 and 3 differ from first query — key has been rotated) |

**Transition time queries (`c4.dm.tv`):**

The coordinator queries 9 transition time parameters (indices 01–0A). Unlike the fan controller where these are Set commands, for the dimmer these are Get queries and the device returns current values:

| Seq | Index | Response Value | Decimal | Description |
|-----|-------|---------------|---------|-------------|
| 8939 | 02 | `02ee` | 750 | Transition time 2 (750ms) |
| 893a | 03 | `07d0` | 2000 | Transition time 3 (2000ms) |
| 893b | 01 | `0064` | 100 | Transition time 1 (100ms) — ramp-on time |
| 893c | 04 | `1388` | 5000 | Transition time 4 (5000ms) |
| 893d | 05 | `1388` | 5000 | Transition time 5 (5000ms) |
| 893e | 08 | `0000` | 0 | Transition time 8 (disabled) |
| 893f | 06 | `0064` | 100 | Transition time 6 (100ms) |
| 8940 | 09 | `0000` | 0 | Transition time 9 (disabled) |
| 8941 | 0a | `0000` | 0 | Transition time 10 (disabled) |

**Dim mode + monitoring + ambient queries:**

| Seq | Command | Direction | Description |
|-----|---------|-----------|-------------|
| 8942 | `c4.dmx.dim` | Coord→Device (Get) | Query dim mode |
| 8942 | `000 c4.dmx.dim 02` | Device→Coord (Resp) | Dim mode = 02 (adaptive phase) |
| 8943 | `c4.dmx.pmti 0002 0002` | Coord→Device (Set) | Power monitoring timer: both intervals = 2 |
| 8943 | `000` | Device→Coord (Resp) | Acknowledged |
| 8944 | `c4.dmx.amb 01` | Coord→Device (Get) | Query ambient light sensor, channel 01 |
| 8944 | `000 c4.dmx.amb 00` | Device→Coord (Resp) | Ambient sensor: value 0 (no sensor present) |

### Phase 7: Idle Heartbeats (t≈228s onward)

After provisioning completes, the device sends periodic heartbeats approximately every 15–17 seconds via ZCL Report Attributes broadcasts. These are shorter than the initial identity broadcast and serve as keep-alive messages.

### Phase 8: Final Off Command (t≈362s)

A final `c4.dmx.off` interrupt command is sent, likely as part of the provisioning completion or state synchronization.

| Seq | Command | Direction |
|-----|---------|-----------|
| 8945 | `c4.dmx.off` | Coord→Device (Interrupt) |
| 8945 | `000` | Device→Coord (Resp) |

## Capture 2 — Physical Button Press Operations

This capture shows the dimmer's behavior when buttons are physically pressed. The sequence demonstrates an on-press followed by an off-press.

### Button Press → On (t≈13.3s)

When the top button (button 07 = "on" button) is pressed:

| Seq | Event | Description |
|-----|-------|-------------|
| b5f4 | `sa c4.dmx.bp 07` | Button press event: button ID 07 (on) |
| b5f5 | `sa c4.dmx.sc 07` | Scene activation: button index 07 |
| b5f6 | `sa c4.dmx.ls 00 00 22 007d 0081 0006 0010 0183 0000 0000` | Light state: ramping up (level 0x22 = 34/255 ≈ 13%) |
| — | `c4.dmx.pmti 00000004 00000004` | Coordinator sets power monitoring interval |
| b5f7 | `sa c4.dmx.ls 00 00 64 007d 0116 0021 0023 03bf 0000 0000` | Light state: reached target (level 0x64 = 100/255 ≈ 39%) |
| b5f8 | `sa c4.dmx.cc 07 01` | Click confirmation: button 07, count 1 |

### Button Press → Off (t≈15.4s)

When the bottom button (button 08 = "off" button) is pressed:

| Seq | Event | Description |
|-----|-------|-------------|
| b5f9 | `sa c4.dmx.bp 08` | Button press event: button ID 08 (off) |
| b5fa | `sa c4.dmx.ls 00 00 61 007d 0112 0021 0022 03bb 0000 0000` | Light state: beginning ramp down (level 0x61 = 97) |
| b5fb | `sa c4.dmx.ls 00 00 60 007d 0113 0020 0022 03ad 0000 0000` | Light state: ramping down (level 0x60 = 96) |
| b5fc | `sa c4.dmx.sc 08` | Scene activation: button index 08 |
| b5fd | `sa c4.dmx.ls 00 00 55 007d 0113 0021 0022 03b8 0000 0000` | Light state: ramping down (level 0x55 = 85) |
| b5fe | `sa c4.dmx.ls 00 00 25 007d 0097 000b 0013 0244 0000 0000` | Light state: ramping down (level 0x25 = 37) |
| b5ff | `sa c4.dmx.cc 08 01` | Click confirmation: button 08, count 1 |
| b600 | `sa c4.dmx.ls 00 00 00 007d 0000 0000 0000 0000 0000 0000` | Light state: off (level 0x00) |

### Second On Press (t≈18.8s)

| Seq | Event | Description |
|-----|-------|-------------|
| b601 | `sa c4.dmx.bp 07` | Button press event: button 07 (on) |
| b602 | `sa c4.dmx.ls 00 00 04 007d 0006 0000 0001 01b6 0000 0000` | Light state: starting ramp (level 0x04) |
| b603 | `sa c4.dmx.sc 07` | Scene activation: button index 07 |
| b604 | `sa c4.dmx.ls 00 00 64 007d 0115 0021 0022 03bc 0000 0000` | Light state: target reached (level 0x64) |
| b605 | `sa c4.dmx.cc 07 01` | Click confirmation: button 07, count 1 |

## Light State Announcement: `c4.dmx.ls`

The primary state announcement for the dimmer. Sent whenever the light level changes.

**Format:** `sa c4.dmx.ls [ch] [unk] [level] [model] [field4] [field5] [field6] [field7] [field8] [field9]`

| Field | Offset | Description |
|-------|--------|-------------|
| ch | 0 | Channel index (always `00` for single-output) |
| unk | 1 | Unknown (always `00`) |
| level | 2 | Current brightness level 00–64 hex (0–100 decimal) — **this is the key field** |
| model | 3 | Device identifier (`007c` for APD120, `007d` also observed) |
| field4 | 4 | Power/current reading — varies with load |
| field5 | 5 | Power/current reading — secondary metric |
| field6 | 6 | Power/current reading — tertiary metric |
| field7 | 7 | Power/current reading — highest correlation with brightness |
| field8 | 8 | Always `0000` |
| field9 | 9 | Always `0000` |

### Brightness Level Encoding

The level field uses a 0–100 (0x00–0x64) scale in hexadecimal, not the ZCL 0–254 range. This is native to the Control4 protocol.

### Telemetry by Light Level

| Level (hex) | Level (dec) | field4 | field5 | field6 | field7 | State |
|------------|-------------|--------|--------|--------|--------|-------|
| 00 | 0 (off) | 0000 | 0000 | 0000 | 0000 | Off |
| 04 | 4 | 0006 | 0000 | 0001 | 01b6 | Just starting |
| 22 | 34 | 0081 | 0006 | 0010 | 0183 | Ramping up |
| 25 | 37 | 0097 | 000b | 0013 | 0244 | Ramping down |
| 55 | 85 | 0113 | 0021 | 0022 | 03b8 | Mid-dim |
| 64 | 100 (full) | 0115–0116 | 0021 | 0022–0023 | 03bc–03bf | Full brightness |

Fields 4–7 appear to be real-time electrical telemetry (current, power, or similar) that correlate with the load level. Field 7 shows the widest range (0x0000 at off to ~0x03BF at full) and is likely the primary power measurement.

## State Announcement Summary

All state announcements use the `sa` prefix and the `0t[seq4]` tell frame format:

| Namespace | Format | Description |
|-----------|--------|-------------|
| `c4.dmx.ls` | `[ch] [unk] [level] [model] [f4..f9]` | Light level + electrical telemetry |
| `c4.dmx.bp` | `[button_id]` | Button press event |
| `c4.dmx.sc` | `[button_index]` | Scene activation (button index that triggered it) |
| `c4.dmx.cc` | `[button_id] [click_count]` | Click confirmation |
| `c4.dmx.dim` | `[mode]` | Dim mode (02 = adaptive phase) |
| `c4.dmx.warn` | `[ch] [code]` | Warning/fault status |

### Button Event Sequence

Each physical button press triggers a predictable three-event sequence:

1. **`c4.dmx.bp [button_id]`** — Immediate button press notification
2. **`c4.dmx.sc [button_index]`** — Scene activation (value is the button index that triggered it)
3. **`c4.dmx.ls ...`** — One or more light state updates as dimmer ramps
4. **`c4.dmx.cc [button_id] 01`** — Click confirmation after action completes

### Dimmer Button IDs

| Button ID | Function |
|-----------|----------|
| 07 | On / Brighten (top button) |
| 08 | Off / Dim (bottom button) |

Note: Button IDs 00–06 are likely used for intermediate preset levels on multi-button keypads, but were not observed in this capture of a 2-button dimmer operation.

## Command Summary

All commands sent on C4_PROFILE_BUTTON (0xC25C), EP 1→1:

| Command | Type | Description |
|---------|------|-------------|
| `c4.dmx.off [params]` | Set/Interrupt | Turn off dimmer / initialize to off |
| `c4.dmx.key` | Get | Query encryption key |
| `c4.dmx.blc [ch] [btn] [val]` | Set | Configure button-to-load control mapping |
| `c4.dmx.led [btn] [sub] [val]` | Set | Configure LED indicator appearance |
| `c4.dmx.plm [mode]` | Set | Set phase load mode (00 = auto-detect) |
| `c4.dmx.pmti [interval_a] [interval_b]` | Set | Set power monitoring timer intervals |
| `c4.dmx.dim` | Get | Query current dim mode |
| `c4.dmx.amb [ch]` | Get | Query ambient light sensor |
| `c4.dm.tv [ch] [idx]` | Get | Query transition time parameter |
| `c4.sy.zpw [level]` | Set | Set ZigBee radio power level |

## Key Differences from SF120 Fan Controller

While the APD120 dimmer and SF120 fan share the same Control4 protocol framework and model string prefix (`control4_light` vs `control4_fan`), there are important differences:

| Aspect | APD120 Dimmer | SF120 Fan Controller |
|--------|---------------|---------------------|
| Level command | Not observed (uses button-mapped levels) | `c4.dmx.fsc 00 [speed]` |
| Level range | 0x00–0x64 (0–100 decimal) | 0x00–0x04 (5 discrete speeds) |
| Primary state | `c4.dmx.ls` (light state) | `c4.dmx.fs` (fan speed) |
| Transition times | Queried via Get (device has stored defaults) | Set via Set commands (all initialized to 0) |
| Dim mode | `02` (adaptive phase dimming) | Not applicable |
| Model identifier | `007c` / `007d` in state announcements | `0087` in state announcements |
| Button IDs | 07 (on), 08 (off) observed | 00–04 (speed mapped) |

## Key Implementation Notes

1. **Commands are retransmitted for reliability** — the coordinator sends each command 2–4 times. The device responds to each with `000` but only acts once.

2. **State announcements are duplicated** — each `sa` message is sent 2–3 times, likely for transmission reliability on the mesh network.

3. **Button presses are autonomous** — when a physical button is pressed, the dimmer handles the ramp internally and reports state changes via `c4.dmx.ls` announcements. The coordinator does not need to send level commands.

4. **The coordinator sends `c4.dmx.pmti` after a button press** — this appears to be an adjustment to the power monitoring interval in response to the state change (observed value `00000004` = 4, shorter than the provisioning value of `0002`).

5. **Transition times are device-stored** — unlike the fan controller where transition times are pushed during provisioning, the dimmer stores its own default transition times and the coordinator queries them. Values range from 100ms (quick ramp) to 5000ms (slow fade).

6. **The brightness level 0x64 (100 decimal) represents full brightness** — this is a percentage-based scale, not the ZCL 0–254 range. ZHA integration must scale between these ranges.

7. **Command sequence numbers increment monotonically** — each command uses an incrementing 16-bit sequence number (e.g., `88f1`, `88f2`, ...) embedded in the ASCII frame. The device echoes this in its response. State announcement sequences use a separate counter (e.g., `eb4e`, `eb4f`, ...).

8. **The encryption key rotates** — two key queries during provisioning returned different values for words 1 and 3 (word 2 was unchanged), suggesting the key is regenerated between provisioning phases or on each query.

9. **Heartbeat interval is ~15–17 seconds** — after provisioning, the device broadcasts periodic ZCL Report Attributes frames as keep-alive messages.
