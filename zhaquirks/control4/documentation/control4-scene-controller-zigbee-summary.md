# Control4 Master Bedside Scene Controller - ZigBee Communication Summary

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

**Source file:** `control4-master-bedside-scene-controller-9-4-9-reinit-plus-button-presses.txt`
**Capture duration:** ~189 seconds (8.9s to 189.0s)
**Total packets:** 1,401
**Device address:** `0x****` (scene controller) communicating to coordinator `0x0000`

---

## Protocol Stack

All traffic flows one direction: from the scene controller (`0x****`) to the ZigBee coordinator (`0x0000`). The communication uses three protocol layers:

1. **IEEE 802.15.4** - Physical/MAC framing (PAN ID `0x****`)
2. **ZigBee NWK/APS** - Network and application support, encrypted payloads
3. **Control4 (C4)** - Proprietary application-layer protocol running over ZigBee endpoint 1, using a custom "DMX" service namespace (`c4.dmx.*`)

All C4 messages use **Channel 0** (`Ch0`).

---

## Message Types

### 1. ZigBee Infrastructure (461 packets)

| Type | Count | Purpose |
|------|-------|---------|
| APS Ack (Endpt 1 to 1) | 431 | Application-layer acknowledgments for C4 messages |
| Route Record | 28 | Multi-hop route advertisements to coordinator |
| Link Status | 10 | Neighbor table broadcasts (~every 15-20s) |

### 2. C4 Announce Messages (464 packets)

Service announcements using the `sa` (service announce) command. Each carries a monotonically increasing sequence ID (in brackets) and advertises a specific `c4.dmx.*` service with a numeric parameter.

### 3. C4 Response Messages (429 packets)

Responses with incrementing sequence IDs. Most carry payload `000` (acknowledgment). One special variant includes keying material:

- **`c4.dmx.key` response** (5 occurrences): Carries a cryptographic key exchange payload `00000000 ******** ******** 00000000 00000000`
- **`c4.dmx.amb` response** (8 occurrences): Ambient light sensor or similar status, value `00`

---

## C4 DMX Service Types

| Service | Meaning (inferred) | Announce Count | Typical Parameters |
|---------|-------------------|----------------|-------------------|
| `c4.dmx.bp` | Button press | 296 | 00, 02, 04 |
| `c4.dmx.sc` | Scene control | 292 | 00, 02, 04 |
| `c4.dmx.hc` | Hold/continuous | 194 | 00, 02, 04 |
| `c4.dmx.cc` | Component/channel config | 118 | Two-param: e.g., `02 01`, `04 03` |
| `c4.dmx.he` | Hold end | 24 | 00, 02, 04 |
| `c4.dmx.key` | Encryption key | 10 | (in response payloads) |
| `c4.dmx.amb` | Ambient sensor | 8 | 00 |
| `c4.dmx.sbt` | Status/battery | 4 | 00 |

The first parameter (00, 02, 04) appears to encode the **button ID** on the scene controller. The `cc` service uses a second parameter that appears to be a sub-index or action count (01, 02, 03, etc.).

---

## Capture Phases

### Phase 1: Reinitialization (~8.9s - 33.4s)

**Sequence IDs: `[81c8]` through `[81f7]`** (high-bit set = init flag)

The controller performs a full reinitialization, announcing all its services from scratch:

1. **Button pair enumeration** (81c8-81d9): Alternating `bp 00` / `sc 00` announces for 9 consecutive pairs - enumerating all scene button endpoints at address 00
2. **Active button group** (81da-81e2): Switches to parameter `04`, announces `bp 04`, `cc 00 09`, `sc 04` pairs - registering a specific button group
3. **Fallback enumeration** (81e3-81f5): Returns to parameter `00`, more `bp`/`sc` pairs
4. **Final config** (81f6-81f7): `cc 00 09` and `sbt 00` - component config and status/battery announcement

This is followed by ~430 `Response` messages (sequence `[15e6]` through `[165f]`) carrying `000` acknowledgments, plus 5 `c4.dmx.key` responses at sequence `[15ea]`.

**Gap: ~52.8 seconds of silence** (33.4s to 86.2s)

### Phase 2: Button Presses (~86.2s - 152.3s)

**Sequence IDs: `[3ddf]` through `[3e21]`** (normal range, no high-bit)

Three distinct button groups are exercised, each following a consistent pattern:

#### Button 00 (parameter `00`, ~86s-102s):
```
bp 00 → sc 00 → cc 00 01 → bp 00 → sc 00 → bp 00 → sc 00 → cc 00 02 →
bp 00 → sc 00 → bp 00 → sc 00 → bp 00 → sc 00 → cc 00 03 →
bp 00 → hc 00 (x8) → he 00
```

#### Button 02 (parameter `02`, ~102s-120s):
```
bp 02 → sc 02 → cc 02 01 → bp 02 → sc 02 → bp 02 → sc 02 → cc 02 02 →
bp 02 → hc 02 (x7) → he 02
```

#### Button 04 (parameter `04`, ~120s-152s):
```
bp 04 → sc 04 → cc 04 01 → bp 04 → sc 04 → bp 04 → sc 04 → cc 04 02 →
... → cc 04 03 →
bp 04 → hc 04 (x8) → he 04
```

Each button's sequence follows the same pattern: short presses (bp/sc pairs with cc counting the press number), followed by a long hold (repeated hc messages terminated by a single he).

**Gap: ~29.8 seconds** (152.3s to 182.1s)

### Phase 3: Additional Button 02 Activity (~182.1s - 186.5s)

**Sequence IDs: `[3e22]` through `[3e32]`**

Additional presses on button 02, following the same bp/sc/cc pattern. The capture ends with `cc 02 01` announces at ~186.5s.

### Final: Link Status (~189.0s)

A single Link Status broadcast closes the capture, showing the controller's neighbor table with 5 neighbors.

---

## Key Observations

1. **Redundant transmission:** Every C4 message is sent 3-6 times (retransmissions for reliability on the wireless link), each with the same sequence ID but a new 802.15.4 frame counter.

2. **Button press encoding:** Button presses are encoded as paired `bp`/`sc` announces. The `bp` (button press) and `sc` (scene control) always travel together. The `cc` (component/channel) message acts as a press counter, incrementing its second parameter with each successive press.

3. **Hold detection:** A button hold is encoded as a burst of `hc` (hold continuous) messages (~7-8 in a row), terminated by a single `he` (hold end). This allows the receiver to track hold duration.

4. **Initialization vs. runtime:** Reinit messages use sequence IDs with bit 15 set (`0x81xx`), while normal button operation uses lower sequence IDs (`0x3dxx`-`0x3exx`). This likely allows the receiver to distinguish fresh joins from normal operation.

5. **Decrypted payload structure:** Each C4 announce payload begins with a fixed header byte sequence, followed by a flags/length byte, then the ASCII command string (e.g., `0t<seqID> sa c4.dmx.cc 02 01\r\n`). The `0t` prefix carries the hex sequence ID.

6. **Three-button device:** The capture reveals a device with at least 3 button endpoints (IDs 00, 02, 04 - even numbers), each capable of short press, multi-press, and hold gestures.

7. **Encryption:** All payloads are ZigBee-encrypted. The `c4.dmx.key` response during initialization suggests a proprietary key exchange layer on top of standard ZigBee security.
