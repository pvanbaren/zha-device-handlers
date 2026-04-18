# C4-Z2IO-ZP Zigbee Capture Summary

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

## Device Identity

| Property | Value |
|---|---|
| Model | `c4:ZigbeeIO:C4-Z2IO-ZP` |
| Short address | `0xXXXX` |
| IEEE address | `00:0f:ff:XX:XX:XX:XX:XX` |
| Extended PAN ID | `XX:XX:XX:XX:XX:XX:XX:XX` |
| PAN ID | `0xXXXX` |
| Channel | 17 (decimal; `c4.z2x.zchan` reports `11` hex) |
| Firmware | 1.2.0 |
| Bootloader | v3 |
| APS Profile | `0xC25C` (C4 proprietary) |
| APS Cluster | `0x0001` |
| Endpoint | 197 (`0xC5`) |

---

## Protocol

Commands are ASCII strings tunnelled over APS — no ZCL framing.

```
0s[4hex-seq] [prop] [args]\r\n          ← Set
0g[4hex-seq] [prop] [args]\r\n          ← Get (args optional)
0r[4hex-seq] [status] [prop] [val]\r\n  ← Response
0t[4hex-seq] sa [prop] [val]\r\n        ← Unsolicited announce
```

Response status codes: `000` = success, `n01` = property not supported on this variant.

---

## Hardware

| Feature | Detail |
|---|---|
| Contact inputs | 5 (bits 0–4 of `cts` bitmask) |
| Relay outputs | 2 |
| Analog inputs | 0 — ZP variant returns `n01` for `ana0` and `ana1` |
| Default contact wiring | NC (unconnected input floats high via pull-up → bit = 1 → reports OPEN) |
| Momentary relay mode | Relay auto-releases after `rlp` ticks; no explicit `rls=0` needed |

---

## Contact Status Protocol

Door/input events are carried in the `c4.z2x.cts` attribute as an 8-bit bitmask covering 5 contact inputs. Unconnected inputs float high due to internal pull-ups, so the idle (all-closed) value reflects whichever inputs are actually wired NC vs NO.

```
bit:  7  6  5  4  3  2  1  0
            unused  [  contact inputs 4–0  ]
```

NC wiring polarity (default):

| Bit state | Contact state |
|---|---|
| `1` | OPEN |
| `0` | CLOSED |

In this capture, only inputs 1 and 2 are used (garage doors). The remaining inputs are unconnected (floating high). The three observed values are:

| Value | Binary      | Input 1 | Input 2 | Notes |
|-------|-------------|---------|---------|-------|
| `0x19` | `0001 1001` | closed | closed | idle; bits 0, 3, 4 permanently set by pull-ups |
| `0x1B` | `0001 1011` | **OPEN** | closed | door 1 triggered |
| `0x1D` | `0001 1101` | closed | **OPEN** | door 2 triggered |

Messages are announced as `sa c4.z2x.cts <value>` sent unicast to the coordinator.

---

## Property Reference

| Property | R/W | Description |
|---|---|---|
| `c4.z2x.cts` | R | Contact input bitmask — 5 inputs, bits 0–4 |
| `c4.z2x.rls` | R/W | Relay output bitmask — 2 relays, bits 0–1 |
| `c4.z2x.opt` | R/W | Options — `1` = momentary relay mode |
| `c4.z2x.ctd` | W | Contact debounce — args: `ch ms-in-hex` (e.g. `1 1f4` = ch1, 500 ms) |
| `c4.z2x.rlp N` | R/W | Relay N pulse duration — response: `ch ticks`; default `01 02` |
| `c4.z2x.ana0` | R | Analog input 0 — always `n01` on ZP variant |
| `c4.z2x.ana1` | R | Analog input 1 — always `n01` on ZP variant |
| `c4.z2x.ldm` | R | Link density metric (diagnostic) |
| `c4.z2x.zmac` | R | IEEE MAC address — space-separated bytes |
| `c4.z2x.zpid` | R | Zigbee PAN ID — space-separated bytes (little-endian) |
| `c4.z2x.zepid` | R | Extended PAN ID — 8 space-separated bytes |
| `c4.z2x.znid` | R | Device short address — space-separated bytes |
| `c4.z2x.zchan` | R | Zigbee channel in hex (e.g. `11` hex = channel 17 decimal) |
| `c4.z2x.tmpi` | R | Internal temperature — centikelvins; °C = (raw × 0.01) − 273.15 |
| `c4.z2x.tmpe` | R | External probe temperature — same encoding; `0x5b13` (233.15 K = −40 °C) is the sentinel for probe not connected |
| `c4.z2x.thumi` | R/annc | Relative humidity — raw / 100 = % RH; announced unsolicited ~every 5 min, also gettable |
| `c4.sy.fwv` | R | Firmware version string, e.g. `1.2.0` |
| `c4.sy.blv` | R | Bootloader version, e.g. `3` |
| `c4.sy.zpc` | W | Zigbee poll counter — set to `348` during provisioning |

---

## Event Timeline

### Phase 1 — Pre-reset

| Time (s) | Event |
|---|---|
| 3 | Link Status broadcasts begin |
| 46.7 | `c4.z2x.cts 0x1B` — door 1 **open** |
| 75.4 | `c4.z2x.cts 0x19` — door 1 closed (+28.7 s) |
| 80.8 | `c4.z2x.cts 0x1D` — door 2 **open** |
| 109.2 | `c4.z2x.cts 0x19` — door 2 closed (+28.4 s) |
| 131–171 | Periodic `thumi`, `tmpi`, `tmpe` broadcasts |

### Phase 2 — 9-Click Reset (~192–197 s)

Identified by a Link Status with a zero-entry neighbour payload, immediately followed by a ZCL Report Attributes identity burst and the full 3-pass coordinator provisioning exchange.

### Phase 3 — Post-reset

| Time (s) | Event |
|---|---|
| 197 | ZCL Report Attributes broadcast × 3 radio paths — announces model and firmware |
| 197–200 | Full 3-pass attribute poll by coordinator (see sequence below) |
| 198–200 | Init-state `sa` announcements: `cts 0x19`, `rls 00` × 5 messages |
| 235 | `c4.z2x.cts 0x1B` — door 1 **open** |
| 246 | `c4.z2x.cts 0x19` — door 1 closed (+11 s) |
| 252 | `c4.z2x.cts 0x1D` — door 2 **open** |
| 263 | `c4.z2x.cts 0x19` — door 2 closed (+11 s) |
| 264+ | Periodic sensor broadcasts resume |

---

## Sensor Encoding

```
temperature (°C) = (raw_hex × 0.01) − 273.15
humidity (% RH)  = raw_hex / 100
tmpe = 0x5b13 (233.15 K / −40 °C) → external probe not connected
```

The external probe (`tmpe`) returned `0x5b13` (233.15 K = −40 °C) constantly across all captures — the standard sentinel value for a disconnected probe.

---

## Extended Sensor Log (separate idle capture, ~3.5 hours)

This capture contains no door 1 activity and two door 2 events. Sensor broadcasts follow a fixed pattern: `thumi` → `tmpi` → `tmpe`, repeating approximately every 5 minutes.

### Door events

| Time (s) | Event |
|---|---|
| 2130 | `c4.z2x.cts 0x1D` — door 2 **open** |
| 2189 | `c4.z2x.cts 0x19` — door 2 closed (+59 s) |

### External probe (`tmpe`)

`0x5b13` (= 23,315 centikelvins = 233.15 K = **−40 °C**) throughout both captures. This is the standard sentinel value returned by temperature ICs when no probe is connected. The external probe is absent on this unit.

### Broadcast interval

Each sensor group (`thumi` + `tmpi` + `tmpe`) broadcasts approximately every 299–300 seconds (~5 minutes). Individual messages within a group are spaced ~35 ms apart.

---

## Post-Reset Provisioning Sequence (3 passes)

After the 9-click reset, the coordinator performs a structured 3-pass provisioning exchange. Numbers below are the C4 protocol transaction sequence numbers. Each exchange is unicast and acknowledged at the APS layer.

### Pass 1 — Configuration & Discovery (seq `0048`–`005d`)

| Seq | Operation | Response |
|---|---|---|
| `0048` | — | `000` |
| `0049` | — | `000` |
| `004a` | — | `000  c4.z2x.cts 19` |
| `004b` | — | `000  c4.z2x.rls 00` |
| `004c` | — | `000` |
| `004e` | — | `000` |
| `004f` | — | `000` |
| `0050` | — | `000  c4.z2x.opt 02` |
| `0051` | — | `000  c4.z2x.cts 19` |
| `0052` | — | `000  c4.z2x.rls 00` |
| `0053` | — | `n01` — `ana0` not present on ZP variant |
| `0054` | — | `n01` — `ana1` not present on ZP variant |
| `0055` | — | `000  c4.z2x.ldm 00` |
| `0056` | — | `000  c4.sy.fwv 1.2.0` |
| `0057` | — | `000  c4.sy.blv 3` |
| `0058` | — | `000  c4.z2x.zmac 00 0f ff XX XX XX XX XX` |
| `0059` | — | `000  c4.z2x.zpid XX XX` |
| `005a` | — | `000  c4.z2x.zepid XX XX XX XX XX XX XX XX` |
| `005b` | — | `000  c4.z2x.znid XX XX` |
| `005c` | — | `000  c4.z2x.zchan 11` (= channel 17) |
| `005d` | — | `000` |
| `005e` | — | `000  c4.z2x.tmpi 71f8` |
| `005f` | — | `000  c4.z2x.tmpe 5b13` |
| `0060` | — | `000  c4.z2x.thumi 1993` (uptime counter) |

### Pass 2 — Verification (seq `0061`–`007a`)

| Seq | Operation | Response |
|---|---|---|
| `0061` | — | `000` |
| `0062` | — | `000` |
| `0063` | — | `000  c4.z2x.cts 19` |
| `0064` | — | `000  c4.z2x.rls 00` |
| `0065` | — | `000` |
| `0066` | — | `000` |
| `0067` | — | `000` |
| `0068` | — | `000` |
| `0069` | — | `000  c4.z2x.opt 02` |
| `006a` | — | `000  c4.z2x.cts 19` |
| `006b` | — | `000  c4.z2x.rls 00` |
| `006c` | — | `n01` — `ana0` (confirmed absent) |
| `006d` | — | `n01` — `ana1` (confirmed absent) |
| `006e` | — | `000  c4.z2x.ldm 00` |
| `006f` | — | `000  c4.sy.fwv 1.2.0` |
| `0070` | — | `000  c4.sy.blv 3` |
| `0071` | — | `000  c4.z2x.zmac 00 0f ff XX XX XX XX XX` |
| `0072` | — | `000  c4.z2x.zpid XX XX` |
| `0073` | — | `000  c4.z2x.zepid XX XX XX XX XX XX XX XX` |
| `0074` | — | `000  c4.z2x.znid XX XX` |
| `0075` | — | `000  c4.z2x.zchan 11` (= channel 17) |
| `0076` | — | `000` |
| `0077` | — | `000  c4.z2x.tmpi 71fb` |
| `0078` | — | `000  c4.z2x.tmpe 5b13` |
| `0079` | — | `000  c4.z2x.thumi 19a7` (uptime counter) |
| `007a` | — | `000` |

### Pass 3 — Final re-query (seq `007b`–`007f`)

| Seq | Operation | Response |
|---|---|---|
| `007b` | — | `000  c4.z2x.cts 19` |
| `007c` | — | `000  c4.z2x.rls 00` |
| `007d` | — | `000` |
| `007f` | — | `000  c4.z2x.rls 00` |

The `n01` responses at `0053`/`0054` and `006c`/`006d` fall at the same structural position in passes 1 and 2 (`ana0`/`ana1` queries), confirming these analog inputs are architecturally absent from the ZP variant rather than transiently unavailable.

Following the poll, the device sends five unsolicited `sa` announcements covering the current `cts`, `rls`, `thumi`, `tmpi`, and `tmpe` values, then resumes normal periodic reporting.
