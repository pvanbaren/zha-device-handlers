# C4-Z2IO-ZP Zigbee IO Module — Protocol Analysis Summary

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

## Goal

Document the Control4 **C4-Z2IO-ZP** Zigbee IO module (garage door / relay controller)
from Wireshark captures and build a Home Assistant custom component handler for it.

---

## Protocol

### Transport

Custom ASCII serial tunnel over APS — no ZCL framing.

| Parameter | Value |
|---|---|
| APS Profile | `0xC25C` |
| APS Cluster | `0x0001` |
| Endpoint | `197` (`0xC5`) |

### Command Format

```
0s[4hex-seq] [prop] [args]\r\n          ← Set
0g[4hex-seq] [prop] [args]\r\n          ← Get (args optional)
0r[4hex-seq] [status] [prop] [val]\r\n  ← Response
0t[4hex-seq] sa [prop] [val]\r\n        ← Unsolicited announce
```

**Response status codes:**
- `000` — success
- `n01` — property not supported on this hardware

---

## Property Table

| Property | R/W | Description |
|---|---|---|
| `c4.z2x.cts` | R | Contact input bitmask — 5 inputs, bits 0–4. Unconnected inputs float high (pull-up → `0x1f`) |
| `c4.z2x.rls` | R/W | Relay output bitmask — 2 relays, bits 0–1 |
| `c4.z2x.opt` | R/W | Options — `1` = momentary relay mode |
| `c4.z2x.ctd` | W | Contact debounce — args: `ch ms-in-hex` (e.g. `1 1f4` = ch1, 500ms) |
| `c4.z2x.rlp N` | R/W | Relay N pulse duration — response: `ch ticks`. Both relays default to `01 02` |
| `c4.z2x.ana0` | R | Analog input 0 — always returns `n01` (not present on ZP variant) |
| `c4.z2x.ana1` | R | Analog input 1 — always returns `n01` |
| `c4.z2x.ldm` | R | Link density metric (diagnostic, read-only) |
| `c4.z2x.zmac` | R | IEEE MAC address — space-separated bytes, e.g. `00 0f ff XX XX XX XX 14` |
| `c4.z2x.zpid` | R | Zigbee PAN ID — space-separated bytes, e.g. `XX XX` → `0xXXXX` |
| `c4.z2x.zepid` | R | Extended PAN ID — 8 space-separated bytes |
| `c4.z2x.znid` | R | Device's own short address — space-separated bytes, e.g. `XX XX` → `0xXXXX` |
| `c4.z2x.zchan` | R | Zigbee channel in hex — e.g. `11` hex = channel **17** decimal |
| `c4.z2x.tmpi` | R | Internal timer period — hex, e.g. `70a2` ≈ 28.8s |
| `c4.z2x.tmpe` | R | External timer period — hex, e.g. `5b13` ≈ 23.3s |
| `c4.z2x.thumi` | R/annc | Uptime counter — announced unsolicited ~every 5s, also gettable |
| `c4.sy.fwv` | R | Firmware version string, e.g. `1.2.0` |
| `c4.sy.blv` | R | Bootloader version, e.g. `3` |
| `c4.sy.zpc` | W | Zigbee poll counter — set to `348` during provisioning |

---

## Hardware

| Feature | Detail |
|---|---|
| Relay outputs | 2 |
| Contact inputs | 5 (bits 0–4 of `cts` bitmask) |
| Analog inputs | 0 (ZP variant — `n01` response) |
| Default contact wiring | NC (unconnected → bit=1 → open) |
| Momentary mode pulse | Auto-releases after `rlp` ticks — no explicit `rls=0` needed |
| ZCL identity | `c4:ZigbeeIO:C4-Z2IO-ZP`, fw `1.2.0` |

---

## Provisioning Sequence (3 passes)

### Pass 1 — Configuration & Discovery
1. `opt = 1` — enable momentary relay mode
2. `ctd 1 1f4` — contact 1 debounce = 500 ms
3. `rlp 1`, `rlp 2` — read relay pulse durations
4. `ana0`, `ana1` — queried, `n01` expected
5. Full device info read: `ldm`, `fwv`, `blv`, `zmac`, `zpid`, `zepid`, `znid`, `zchan`, `tmpi`, `tmpe`, `thumi`
6. `zpc = 348` — set Zigbee poll counter

### Pass 2 — Verification
7. `ctd 1 1f4` — re-apply debounce
8. `opt = 1` — re-apply momentary mode
9. Read `cts`, `rls`, `opt` to verify state

### Pass 3 — Final re-query
10. `rlp 1`, `rlp 2` — re-read pulse durations
11. `ana0` — re-queried (`n01` expected)

---

## Contact Wiring Polarity

| Wiring | bit=0 | bit=1 |
|---|---|---|
| NC (default) | CLOSED | OPEN |
| NO | OPEN | CLOSED |

---

## HA Integration

### File
`custom_components/zha_c4_patch/c4_z2io_zp.py`

### Class
`C4Z2IOZPHandler` — per-device handler, one instance per IEEE address.

### Key Methods

| Method | Description |
|---|---|
| `configure_device()` | Full 3-pass provisioning sequence |
| `handle_packet(data)` | Dispatch inbound raw APS frame |
| `trigger_relay(channel)` | Pulse relay 1 or 2 to actuate door/load |
| `poll_state()` | Query current `cts` + `rls` |
| `read_device_info()` | Query all read-only device properties |

### HA Events

Fires `zha_event` on the HA bus with `event_type: C4-Z2IO-ZP`:

| `sub_type` | Trigger |
|---|---|
| `contact_state_changed` | Any contact input changes state |
| `relay_state_changed` | Any relay changes state |

### HA Services (domain: `zha_c4_patch`)

| Service | Data | Description |
|---|---|---|
| `garage_door_trigger` | `ieee`, `relay` (1 or 2) | Pulse a relay |
| `garage_door_poll` | `ieee` | Force a state poll |

### Template Cover (example)

```yaml
cover:
  - platform: template
    covers:
      garage_door_1:
        device_class: garage
        value_template: "{{ states('input_text.c4_garage_1_state') }}"
        open_cover:
          service: zha_c4_patch.garage_door_trigger
          data:
            ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
            relay: 1
        close_cover:
          service: zha_c4_patch.garage_door_trigger
          data:
            ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
            relay: 1
```

---

## Integration Snippet (`__init__.py`)

```python
from .c4_z2io_zp import (
    C4Z2IOZPHandler, C4_PROFILE, C4_CLUSTER, C4_ENDPOINT,
    async_setup_services,
)

_C4_Z2IO_HANDLER_MAP: dict[str, C4Z2IOZPHandler] = {}

# In patched packet_received, after model is resolved:
if model == "C4-Z2IO-ZP":
    ieee_str = str(device.ieee)
    if ieee_str not in _C4_Z2IO_HANDLER_MAP:
        handler = C4Z2IOZPHandler(ieee_str, device, hass, app)
        _C4_Z2IO_HANDLER_MAP[ieee_str] = handler
        hass.async_create_task(handler.configure_device())
    if (profile == C4_PROFILE and cluster_id == C4_CLUSTER
            and src_ep == C4_ENDPOINT):
        _C4_Z2IO_HANDLER_MAP[ieee_str].handle_packet(data)
        return

# In async_setup:
await async_setup_services(hass, _C4_Z2IO_HANDLER_MAP)
```
