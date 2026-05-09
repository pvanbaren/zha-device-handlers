# Control4 SR260 Remote — ZigBee Communication Summary

> **Disclaimer:** This document was produced through independent analysis
> of ZigBee packet captures. It is not official Control4 documentation and is not
> endorsed by or affiliated with Control4 Corporation. All protocol details,
> command names, and behavioral descriptions are based on observed traffic and may
> be incomplete or inaccurate. This information is provided "as is" without warranty
> of any kind. Use at your own risk.

**Source captures (`sniff/`):**
- `control4-sr260-remote-button-presses.txt` — broad capture, all 50 button codes plus list/menu rendering
- `control4-sr260-remote-dpad-volume-channel.txt` — definitive nav-cluster mapping
- `control4-sr260-remote-transport-controls.txt` — definitive transport-block mapping
- `control4-sr260-remote-initialization.txt` — power-on / rejoin sequence
- `control4-sr260-remote-watch-menu-with-item-selected.txt` — list selection (`is`/`ise`) flow

**Devices:** `0xc88b` (SR260 remote) ↔ `0x0000` (Control4 controller / Director)
**Transport:** standard Zigbee security; Control4 channel-0 framing
`0t<seqID> <verb> c4.<ns>.<cmd> <args>\r\n`

---

## Verbs

| Verb | Direction | Meaning |
|---|---|---|
| `sa` (Announce) | remote → controller | unsolicited event from remote |
| `Set` | controller → remote | imperative command |
| `Initialize` | either way | UI / list-rendering protocol |
| `Response` | answers a request | first token `000` = OK, then optional quoted strings |

Sequence IDs are independent per direction (in this capture the remote uses
`[b4e7]…[b55f]`, controller uses `[7357]…[7360]`). A `Response` echoes the
sequence ID of the request it answers.

---

## Two namespaces

### `c4.zr.*` — the remote itself

| Cmd | Direction | Form | Purpose |
|---|---|---|---|
| `mot` | remote → ctrl (announce) | `sa c4.zr.mot` | motion / pickup wake. First thing emitted on a wake-from-sleep (the *cold-boot* path uses standard ZigBee orphan / rejoin instead — see Initialization). |
| `tm` | ctrl → remote (Set) | `c4.zr.tm <hh> <mm> <ss> <dow>` | time-of-day push (4 hex bytes — observed `13 0f 12 01` ≈ 19:15:18 Mon, `14 0a 05 01` ≈ 20:10:05 Mon). Sent in response to `mot` and as the first C4-layer command after a rejoin. |
| `loc` | ctrl → remote (Set) | `c4.zr.loc <p1:u8> <p2:u8> "<locale>"` | UI locale push (e.g. `c4.zr.loc 00 0f "en_US"`). `p1`/`p2` semantics unconfirmed — `p2 = 0x0f` does not match the string length (5), so probably region/feature flags. Observed only during initialization. |
| `bb` | remote → ctrl (announce) | `sa c4.zr.bb <btn> <p1:u16> <p2:u16> <u32>` | **button begin** (key down). |
| `be` | remote → ctrl (announce) | `sa c4.zr.be <btn> <p1:u16> <p2:u16> <u32>` | **button end** (key up). Same trailing tuple as the matching `bb`. |
| `bl` | remote → ctrl (announce) | `sa c4.zr.bl <id>` | backlight / wake event (one-shot, no `be` partner). Seen at the end of init and around screen-off transitions; the only observed value is `0x3a`. |

**Button IDs observed:** `0x00` – `0x31` (50 distinct codes) — every physical
key on the SR260, fully mapped below. A short tap emits exactly one `bb`
followed by one `be`. A held button emits `bb`, then a stream of
`c4.zr.bh <btn> 0000 0000` re-sent every ~100ms while the key is held,
then a single `be` on release — observed e.g. for held Vol+ as
`sa c4.zr.bh 0c 0000 0000`. (Unlike the Master Bedside scene controller
the SR260 has no `c4.dmx.hc`/`he`; `bh` is the only hold signal and there
is no separate release-after-hold marker.)

**Complete key bindings** (from operator logs of this capture, every code
exercised at least once):

| ID | Key |
|---|---|
| `0x00` | Room Off |
| `0x01` | Watch |
| `0x02` | Control4 (home / menu) |
| `0x03` | Listen |
| `0x04` | List |
| `0x05` | I (custom button) |
| `0x06` | II (custom button) |
| `0x07` | III (custom button) |
| `0x08` | Guide |
| `0x09` | Page Up |
| `0x0a` | Page Down |
| `0x0b` | Prev |
| `0x0c` | Vol+ |
| `0x0d` | Up |
| `0x0e` | Ch+ |
| `0x0f` | Left |
| `0x10` | Select / OK |
| `0x11` | Right |
| `0x12` | Vol− |
| `0x13` | Down |
| `0x14` | Ch− |
| `0x15` | Mute |
| `0x16` | Info |
| `0x17` | Menu |
| `0x18` | Cancel |
| `0x19` | Rewind |
| `0x1a` | DVR |
| `0x1b` | Fast Forward |
| `0x1c` | Skip Back |
| `0x1d` | Play |
| `0x1e` | Skip Forward |
| `0x1f` | Record |
| `0x20` | Pause |
| `0x21` | Stop |
| `0x22` | Red (color button) |
| `0x23` | Green (color button) |
| `0x24` | Yellow (color button) |
| `0x25` | Blue (color button) |
| `0x26` | digit `1` |
| `0x27` | digit `2` |
| `0x28` | digit `3` |
| `0x29` | digit `4` |
| `0x2A` | digit `5` |
| `0x2B` | digit `6` |
| `0x2C` | digit `7` |
| `0x2D` | digit `8` |
| `0x2E` | digit `9` |
| `0x2F` | `*` |
| `0x30` | digit `0` |
| `0x31` | `#` |

**Physical layout (top → bottom, inferred from contiguous code blocks):**

| Range | Cluster |
|---|---|
| `0x00..0x07` | top row of soft / activity keys (Room Off, Watch, Control4, Listen, List, I, II, III) |
| `0x08..0x0b` | nav-extras row (Guide, Page Up, Page Down, Prev) |
| `0x0c..0x14` | central navigation: d-pad + Select + Vol± / Ch± rockers, arranged as a 3×3 grid (see below) |
| `0x15..0x18` | UI cluster (Mute, Info, Menu, Cancel) |
| `0x19..0x21` | transport controls (Rewind, DVR, FastForward, SkipBack, Play, SkipForward, Record, Pause, Stop — sequential except Pause/Stop are swapped) |
| `0x22..0x25` | color buttons (Red, Green, Yellow, Blue) |
| `0x26..0x31` | numeric keypad in dial-pad order (`1 2 3` / `4 5 6` / `7 8 9` / `* 0 #`) |

The button-ID space is dense — codes are assigned sequentially within each
physical cluster, with no holes between clusters.

**Navigation cluster layout** (`0x0c..0x14`, definitively confirmed from
`sniff/control4-sr260-remote-dpad-volume-channel.txt`): the 9 codes form a
clean 3×3 grid where every vertical neighbour is `+6` apart, with Vol± in
the left column, the d-pad up/down in the middle column, and Ch± in the
right column:

```
0x0c Vol+    0x0d Up      0x0e Ch+
0x0f Left    0x10 OK      0x11 Right
0x12 Vol-    0x13 Down    0x14 Ch-
```

(In the dpad capture the user pressed Vol+, Up, Ch+, Left, Select, Right,
Vol−, Down, Ch− in that order, and the bb/be events fired in monotonic
order `0x0c → 0x14`, locking the mapping unambiguously.)


**Trailing tail (`p1:u16 p2:u16 u32`)** carries the **on-screen menu state
at the moment of the event**, not a per-button press counter:

- `p1` = currently displayed list ID (matches the `<listID>` from the
  controller's last `c4.ln.sl`). `0000` when no list is shown.
- `p2` = currently selected index within that list (matches the `<selIdx>`
  from `sl`). `0000` when no list is shown.
- last 32 bits are always `00000000` in this capture.

Verification: when list 1 ("Screen Porch", `sl 0001 … 0001`) was on screen,
the tail was `0001 0001`. When list 2 ("Watch", `sl 0002 … 0000`) was on
screen, tail was `0002 0000`. When list 3 ("Listen", `sl 0003 … 0000`) was
on screen, tail was `0003 0000`. After `c4.ln.le` (list close) the tail
drops to `0000 0000`.

There is one revealing edge case at seq `[b559]`/`[b55a]`: the user pressed
the Control4 button (id `0x02`) while list 3 was still displayed; `bb`
fired with tail `0003 0000`, the controller responded by pushing
`c4.ln.le`, and the matching `be` 200 ms later carried `0000 0000` because
the list had already closed by then. So `bb` and `be` for the **same**
press can carry **different** tails if the screen state changes between
press and release.

**Cancel-while-menu-up — direct capture** (`sniff/test-case-press-cancel-while-menu-up.txt`,
frame 38785): pressing Cancel (`0x18`) with a list on screen does **not**
emit `bb 18`/`be 18`. Instead the SR260 announces a dedicated
`c4.ln.cn <listID:u16> <selIdx:u16>` — analogous to how `is`/`ise`
replace `bb 10`/`be 10` for Select. There is no `cne` companion; `cn` is
a one-shot announce (the duplicate frame 38788 is a retransmission with
the same `0t01ab` sequence number). The controller is then expected to
push `c4.ln.le` to actually close the menu — the SR260 keeps the LCD up
until that arrives.

```
sa c4.ln.cn 000c 0000              ← user pressed Cancel; listID=0x000c
(controller sends Init c4.ln.le)   ← actual dismiss
```

The SR260 quirk handles `c4.ln.cn` directly: it fires a `menu_cancel`
zha_event (so dispatcher automations waiting on the menu can exit
immediately) and sends `c4.ln.le` to clear the LCD. It does **not**
also fire SHORT_PRESS on the Cancel virtual EP: when a menu is up the
user's intent is to dismiss it, not to send a Back keystroke to the
underlying media device — firing both would cause double-actions
(menu dismissed AND Back sent to the TV). The same applies to
`is`/`ise`: Select-while-menu-up fires `menu_select` only, not a
parallel SHORT_PRESS on the Select EP. Bind menu logic to the
`menu_select` / `menu_cancel` zha_events; bind device-control logic
to `bb`/`be` (which fire only when no menu is up).

### `c4.ln.*` — the on-screen list / menu

A small windowed-list protocol for the SR260's LCD menu. The controller seeds
a list, the remote pages items as the user scrolls.

| Cmd | Direction | Form | Purpose |
|---|---|---|---|
| `ri` | ctrl → remote (Set) | `c4.ln.ri "<room>" "<active source>"` | set room / zone title and currently-active source. Second arg is empty (`""`) when no source is active, populated when one is (e.g. `c4.ln.ri "Screen Porch" "YouTube TV"`). |
| `dm` | ctrl → remote (Init) | `c4.ln.dm <iconByte> "<message>"` | "display message" — render a single-line splash on the LCD. Observed during init as `c4.ln.dm 5a "Loading Room..."`. Closed with `c4.ln.le`. The leading byte appears to be a glyph code from the same icon table used by list-item label prefixes. |
| `sl` | ctrl → remote (Init) | `c4.ln.sl <listID> <count> <selIdx> "<icon><title>"` | "set list": establish a list with N items, selected index, header title (Watch / Listen / Settings). The first byte inside the quoted title is a 1-byte glyph code from the same icon table used by `c4.ln.dm` and list-item labels — observed values include `0x81` for the "Watch" header. |
| `gi` | remote → ctrl (Init) | `c4.ln.gi <listID> <offset> <count> 00` | "get items": page request — give me `count` items starting at `offset`. |
| (`gi` Response) | ctrl → remote | `000 "<item0>" "<item1>" …` | the requested labels. |
| `is` | remote → ctrl (announce) | `sa c4.ln.is <listID:u16> <selIdx:u16> <p3:u16>` | **item-select begin** — user pressed OK/Select on the highlighted list item. `listID`/`selIdx` match the controller's last `c4.ln.sl`. `p3` is `0001` in every observation; semantics unconfirmed (possibly a press-type / source code). |
| `ise` | remote → ctrl (announce) | `sa c4.ln.ise <listID:u16> <selIdx:u16> <p3:u16>` | **item-select end** — release of the matching `is`. Same trailing tuple as the matching `is`. |
| `cn` | remote → ctrl (announce) | `sa c4.ln.cn <listID:u16> <selIdx:u16>` | **cancel** — user pressed Cancel (`0x18`) while a list was on screen. Replaces `bb 18`/`be 18` (no parallel HID-layer event); one-shot, no `cne` companion. Controller is expected to respond with `c4.ln.le` to dismiss the menu. Captured in `sniff/test-case-press-cancel-while-menu-up.txt` frame 38785. |
| `le` | ctrl → remote (Init) | `c4.ln.le` | "list end" / leave list / message view, screen closes. |

**Item-label encoding:** each quoted item starts with a 1-byte glyph code:
`\x01` = media tile (Netflix, YouTube, Prime Video, TIDAL); other high-bit
bytes (`\xa6`/`\xb1`-ish) encode menu icons (Settings, Watch, Listen,
Comfort, Security, Now Playing, contact names like Patricia / Clarissa /
Lucas).

The same glyph-prefix convention applies to the **`c4.ln.sl` header
title** — the first byte inside the quoted `<title>` argument is a 1-byte
icon code, not a printable character. Observed value: `0x81` for the
"Watch" header (e.g. `c4.ln.sl 0007 0004 0000 "\x81Watch"`). The hex dump
of frame `[7370]` from the watch-menu capture shows the byte sequence
`22 81 57 61 74 63 68 22` — i.e. `"` `\x81` `W` `a` `t` `c` `h` `"`.

**`is`/`ise` flow** (from `control4-sr260-remote-watch-menu-with-item-selected.txt`):

```
[7370] Set       c4.ln.sl 0007 0004 0000 "…Watch"           ← seed list 7
[b572] Init      c4.ln.gi 0007 0000 0004 00                 ← page request
[b572] Response  000 "\x01YouTube TV" "\x01YouTube" "\x01Netflix"
... (user d-pads to idx 3; selection is tracked locally on the LCD) ...
[b573] Init      c4.ln.gi 0007 0003 0001 00                 ← fetch idx 3 label
[b573] Response  000 "\x01Prime Video"
[b574] Announce  sa c4.ln.is  0007 0003 0001                ← OK pressed (down)
[7371] Set       c4.ln.ri "Screen Porch" "Prime Video"      ← controller acts
[b575] Announce  sa c4.ln.ise 0007 0003 0001                ← OK released
[7373] Init      c4.ln.le                                   ← close list
```

While a list is on screen, pressing Select/OK does **not** emit `bb 10`/`be 10`
— `is`/`ise` replace the HID-layer event, scoped to the list-protocol layer
and carrying the selected item rather than a button id. The d-pad navigation
between items is also not visible at the C4 layer (the SR260 tracks selIdx
locally and only reports the final landed-on index in `is`/`ise`); the
controller can still infer the active selection from the `gi` page-request
pattern that immediately precedes the press.

---

## End-to-end flow in the capture

1. **Wake (~0–1 s):** remote `sa c4.zr.mot` → controller
   `Set c4.zr.tm 13 0f 12 01` → controller
   `Set c4.ln.ri "Screen Porch" ""`. (All three retransmit a few times —
   typical C4 redundancy.)
2. **Button stress (~1–43 s):** ~50 `bb`/`be` pairs walking IDs `0x00`–`0x31`
   with no UI side-effect. Pure HID-style traffic.
3. **Menu render (~44–52 s):** controller pushes successive lists
   (`sl 0001 "Screen Porch"` / `sl 0002 "Watch"` / `sl 0003 "Listen"`, the
   last being 16 items long); the remote requests pages with `gi` and the
   controller answers each window of items; `c4.ln.le` closes each view.
4. **Sleep tail (~67–70 s):** more `c4.ln.le` plus one `c4.zr.bl 3a` as the
   screen powers down.

---

## Initialization sequence

(Source: `sniff/control4-sr260-remote-initialization.txt`, ~12 s from
power-on to operational state.)

A power-cycle does **not** trigger a fresh Trust-Center pairing — the
remote retains its network credentials and re-attaches via standard
ZigBee orphan/rejoin. The sequence layers cleanly:

```
power-on
  ├─ 802.15.4 orphan recovery        (Orphan Notification → Coord Realignment)
  ├─ 802.15.4 active scan            (Beacon Request → Beacon)
  ├─ ZigBee NWK rejoin               (Rejoin Request → Rejoin Response)
  ├─ ZDP Device Announcement         (broadcast, ×2)
  ├─ ZCL bootstrap                   (Read Attributes / Reports / Power Config; ~5s)
  └─ C4 application bootstrap
       ├─ Set       c4.zr.tm  <hh mm ss dow>          ← clock first
       ├─ Init      c4.ln.dm  5a "Loading Room..."    ← LCD splash
       ├─ Init      c4.ln.le                          ← close splash
       ├─ Set       c4.zr.loc 00 0f "en_US"           ← locale
       ├─ Set       c4.ln.ri  "<room>" "<active src>" ← room + source
       └─ Announce  sa c4.zr.bl 3a                    ← backlight transition → idle
```

Notes:

- **No `c4.zr.mot` on cold-boot.** `mot` is only emitted on a wake-from-sleep
  (when the remote is already on the network and just re-establishing a
  parent / waking the screen). On a real power cycle the application-layer
  bootstrap is initiated by the controller as soon as the rejoin completes,
  not by the remote.
- **`tm` is sent before any UI command.** The clock-sync packet in the
  init capture (`73a5`) fires immediately after the ZCL bootstrap and is
  acked before the loading splash appears. A quirk that wants to keep the
  clock honest should therefore answer `tm` even outside of `mot`.
- **`dm` + `le` is the splash idiom.** `c4.ln.dm <icon> "<message>"`
  pushes a one-line message; `c4.ln.le` closes it. This is the same `le`
  used to close lists — it doesn't distinguish between the two cases.
- **`ri` second arg = active source.** The previous capture had `ri
  "Screen Porch" ""` (idle); this capture shows `ri "Screen Porch"
  "YouTube TV"` after the controller resolved the active room state. A
  quirk that displays this on HA can treat the second arg as the
  room's currently-playing source.
- **Sequence counters persist across power cycles.** The remote-side
  announce seq jumped from `[b55f]` (last `bl 3a` of the previous capture)
  to `[c727]` here for the next `bl 3a` — a delta of `+0x11C8` rather than
  resetting to a low value. Useful as a freshness signal if a quirk needs
  to ignore replays.

---

## Implications for a quirk

- **Button events:** only `c4.zr.bb`/`c4.zr.be` matter. Map each `<btn>`
  (`0x00`–`0x31`) to a ZHA `command_id` (or convert to `press`/`release` ZCL
  events). Hold = compute `be.t − bb.t`; this device does not emit `hc`/`he`.
  The trailing tail can be ignored for plain button-event purposes — it
  describes the screen, not the press.
- **Wake / clock handshake:** `mot` → `tm` on a sleep wake; the controller
  also fires `tm` unprompted as the first C4-layer message after a cold-boot
  rejoin. If you don't answer with `tm` the remote's clock will drift; the
  payload is 4 raw hex bytes that decode cleanly as `hh mm ss dow` (where
  dow `01` = Monday in observed captures).
- **Locale and splash:** `c4.zr.loc <flags> "<locale>"` and the
  `c4.ln.dm` / `c4.ln.le` splash idiom only appear during initialization.
  A quirk that just surfaces button events can ignore them; one that drives
  the LCD must reproduce them so the remote leaves "Loading Room…" and
  knows what locale to render.
- **Screen support is optional but non-trivial:** to keep the LCD usable you
  would need to implement at minimum `ri` (title), `sl` (list header), `gi`
  request handling with paged `Response` returns, `le` (close), plus the
  icon-byte prefix on each label. For pure Home Assistant button-event use,
  most of `c4.ln.*` can be ignored — but note that `is`/`ise` are
  remote-originated *list-item-select* events and behave like a `bb`/`be`
  pair; surfacing them as ZHA events lets HA react to LCD selections even if
  the quirk doesn't drive the rest of the list protocol.
- Framing / sequence / encryption is the same C4-over-Zigbee envelope the
  rest of `zhaquirks/control4/` already handles.
