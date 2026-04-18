# Control4 Switched Outlet — Zigbee Communication Analysis

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

## Device Identity

- **Device type**: `c4:outlet_switch:loz-5s1-w`
- **Firmware version**: `03.19.49`
- **Network address**: `<device_addr>`
- **Coordinator address**: `0x0000` (standard)
- **ZigBee profile**: Control4 proprietary (custom cluster `0xC25C`)
- **Endpoints used**: Endpoint 1 (Set/Response commands), Endpoint 197 (`0xC5`) (Ack/Announce messages)

## Protocol Overview

Control4 uses a **text-based command protocol** layered on top of ZigBee APS-encrypted frames. All payloads in the decrypted data follow a human-readable ASCII format with `\r\n` line termination. The protocol rides on a manufacturer-specific ZigBee cluster (`0xC25C` / `0x005D`).

## Message Types

### 1. Set Command (Coordinator → Device)

Sent from the coordinator to the outlet to control relay state.

**Payload structure** (34 bytes decrypted):
```
40 01 01 00 5C C2 01 <seq> 30 73 <session_hex> 20 63 34 2E 64 6D 2E 74 76 20 <param1> 20 <param2> 20 <level> 0D 0A
```

**ASCII interpretation**:
```
0s<session> c4.dm.tv <param1> <param2> <level>\r\n
```

- `0s` = "Set" command prefix
- `<session>` = 4-character hex session/transaction ID (incrementing)
- `c4.dm.tv` = Control4 Device Manager variable name (likely "toggle value")
- `<param1>` = Outlet selector or channel indicator
- `<param2>` = Always `00` in captures
- `<level>` = Target level: `64` (decimal 100) = ON, `00` = OFF

**Observed Set commands**:

| File | Session | Payload | Meaning |
|------|---------|---------|---------|
| cycle-on-off | — | `c4.dm.tv 01 00 64` | Outlet ON (level 100) |
| cycle-on-off | — | `c4.dm.tv 01 00 00` | Outlet OFF (level 0) |
| outlet-1-on-and-off | — | `c4.dm.tv 01 00 64` | Outlet 1 ON |
| outlet-1-on-and-off | — | `c4.dm.tv 01 00 00` | Outlet 1 OFF |
| outlet-2-on-and-off | — | `c4.dm.tv 00 00 64` | Outlet 2 ON |
| outlet-2-on-and-off | — | `c4.dm.tv 00 00 00` | Outlet 2 OFF |

**Key observation**: The first parameter (`01` vs `00`) selects which outlet is being controlled on this dual-outlet device. The third parameter is the target level.

### 2. Response (Device → Coordinator)

Sent by the outlet back to the coordinator to acknowledge a Set command.

**Payload structure** (18 bytes decrypted):
```
40 C5 01 00 5C C2 C5 <seq> 30 72 <session_hex> 20 30 30 30
```

**ASCII interpretation**:
```
0r<session> 000
```

- `0r` = "Response" prefix
- `<session>` = Echoes the session ID from the Set command
- `000` = Success/acknowledgment code

### 3. Announce (Device → Coordinator)

Sent by the outlet to announce its current state, typically after processing a Set command.

**Payload structure** (35 bytes decrypted):
```
40 C5 01 00 5C C2 C5 <seq> 30 74 <session_hex> 20 73 61 20 63 34 2E 64 6D 2E 74 63 20 <state> 20 36 34 20 0D 0A
```

**ASCII interpretation**:
```
0t<session> sa c4.dm.tc <state> 64 \r\n
```

- `0t` = "Telemetry/announce" prefix
- `<session>` = Unique announce session ID (incrementing)
- `sa` = "State announce"
- `c4.dm.tc` = Control4 Device Manager variable (state/toggle change notification)
- `<state>` = Reported outlet state (`01` = on, `00` = off)
- `64` = Current level (decimal 100 = fully on)

**Observed Announce messages**:

| File | Session | Payload | Meaning |
|------|---------|---------|---------|
| cycle-on-off | — | `sa c4.dm.tc 01 64` | Outlet now ON at level 100 |
| outlet-1-on-and-off | — | `sa c4.dm.tc 01 64` | Outlet 1 now ON at level 100 |
| outlet-2-on-and-off | — | `sa c4.dm.tc 00 64` | Outlet 2 now ON at level 100 |

### 4. APS Acknowledgments

Standard ZigBee APS-layer acknowledgments are exchanged between endpoints:

- **Endpoint 1 ↔ 1**: Acknowledges Set commands
- **Endpoint 197 ↔ 197** (`0xC5`): Acknowledges Response and Announce messages

These contain a short 8-byte decrypted payload:
```
02 <endpt> 01 00 5C C2 <endpt> <seq>
```

## Communication Flow

### Typical Set Command Sequence

```
Coordinator                                          Outlet
     |                                                     |
     |--- C4 Set [sessionID]: c4.dm.tv ... ------------->  |  (sent 2-3x for reliability)
     |                                                     |
     |  <---------- APS Ack (Endpoint 1 → 1) ------------ |  (sent via multiple relays)
     |                                                     |
     |  <--- C4 Response [sessionID]: 000 ---------------- |  (success acknowledgment)
     |                                                     |
     |--- APS Ack (Endpoint 197 → 197) ----------------->  |
     |                                                     |
     |  <--- C4 Announce [newSessionID]: sa c4.dm.tc ... - |  (state change notification)
     |                                                     |
     |--- APS Ack (Endpoint 197 → 197) ----------------->  |
     |                                                     |
```

### Timing

From the captures, a typical Set→Response→Announce cycle completes within approximately **200ms**. The coordinator often sends the Set command 2-3 times through different mesh relay paths for reliability.

## Rejoin Behavior

When the outlet rejoins the network (captured in `control4-switched-outlet-rejoin.txt`):

1. The device broadcasts **ZCL Report Attributes** messages (Seq: 62) repeatedly to all neighbors in the mesh.

2. The Report Attributes payload (62 bytes decrypted) contains device identification on a manufacturer-specific cluster:
   - **Attribute 0x0007** (String): `c4:outlet_switch:loz-5s1-w` — device model identifier
   - **Attribute 0x0004** (String): `03.19.49` — firmware version
   - **Attribute 0x0005** (Uint8): `03` — unknown (possibly hardware revision or device class)
   - **Attribute 0x0006** (Uint16): `0x004B` (75) — unknown (possibly a capability bitmask)

3. These broadcasts are sent to multiple relays/neighbors, indicating a mesh network with multiple routing paths.

4. After the rejoin completes, the coordinator sends Set commands to the device (e.g., `c4.dm.tv 01 00 00`) to synchronize the outlet state.

## Mesh Network Details

- Messages are frequently sent through 2+ relay hops, with the coordinator and device using different relay paths for redundancy
- Duplicate messages are commonly seen arriving through different mesh paths

## Summary of Protocol Commands

| Prefix | Direction | Endpoint | Purpose |
|--------|-----------|----------|---------|
| `0s` | Coordinator → Device | 1 | Set variable value |
| `0r` | Device → Coordinator | 197 | Response/acknowledgment |
| `0t` | Device → Coordinator | 197 | State announce/telemetry |

## Key Variables

| Variable | Usage | Values |
|----------|-------|--------|
| `c4.dm.tv` | Set command target | `<outlet> 00 <level>` where outlet=`00`/`01`, level=`00`(off)/`64`(on=100) |
| `c4.dm.tc` | State announce | `<state> 64` where state=`00`(off)/`01`(on) |
