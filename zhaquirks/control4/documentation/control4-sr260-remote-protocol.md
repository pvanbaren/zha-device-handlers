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
| `tm` | ctrl → remote (Set) | `c4.zr.tm <hh> <mm> <ss> 01` | time-of-day push (observed `13 0f 12 01` ≈ 19:15:18 Mon, `0b 31 1c 01` ≈ 11:49:28 Fri). The fourth byte is `01` on both days, so it is **not** day-of-week; the SR-250 gets only the three time bytes. Sent in response to `mot` and as the first C4-layer command after a rejoin. |
| `loc` | ctrl → remote (Set) | `c4.zr.loc <p1:u8> <p2:u8> "<locale>"` | UI locale push (e.g. `c4.zr.loc 00 0f "en_US"`). `p1`/`p2` semantics unconfirmed — `p2 = 0x0f` does not match the string length (5), so probably region/feature flags. Observed only during initialization. |
| `bb` | remote → ctrl (announce) | `sa c4.zr.bb <btn> <p1:u16> <p2:u16> <u32>` | **button begin** (key down). |
| `be` | remote → ctrl (announce) | `sa c4.zr.be <btn> <p1:u16> <p2:u16> <u32>` | **button end** (key up). Same trailing tuple as the matching `bb`. |
| `bl` | remote → ctrl (announce) | `sa c4.zr.bl <pct>` | **battery percent** (one-shot, no `be` partner), not backlight — see "Remote settings" below. Seen at the end of init and around screen-off transitions. |

**Button IDs observed:** `0x00` – `0x31` (50 distinct codes) — every physical
key on the SR260, fully mapped below. A short tap emits exactly one `bb`
followed by one `be`. A held button emits `bb`, then a stream of
`c4.zr.bh <btn> 0000 0000` re-sent every **~501 ms** while the key is held
(measured off the remote's own hold counter; see "The `bb`/`bh`/`be` tail" below),
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
       ├─ Set       c4.zr.tm  <hh mm ss> 01           ← clock first
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

## Measured on the wire — a live director driving real remotes

Everything above this point was written from sniffer transcripts. This section
is read directly off decrypted on-air captures of a Control4 director driving
real SR-260s (and, where noted, an SR-250). Where it contradicts the prose
above, it wins. Room and source names below are placeholders.

### Endpoints are set by direction, not by request/response

Across every channel-0 frame, with no exceptions:

```
remote     -> controller    EP 197 -> 197
controller -> remote        EP 1   -> 1
```

A `c4.ln.gi` request arrives on **197 -> 197** and the director answers it on
**1 -> 1**. The reply endpoint is a property of the direction, not an echo of
the request. A reply sent back to EP 197 is silently discarded. Profile
`0xc25c`, cluster `0x0001`, APS ack requested on every frame. The same holds
for the SR-250, including the remote's own `0r` acks.

(The separate `0xc25d` profile carries ZCL telemetry on EP 2 and EP 196: model
string, firmware, battery and attribute reports. It is interleaved with list
traffic and unrelated to it.)

### `bb 01` (Watch) is what opens the list

The Watch key press is the trigger, and the director answers it in ~60 ms:

```
1017.524  0t0843 sa c4.zr.bb 01 0000 0000 00000000      <- Watch pressed, no list up
1017.585  0i6233 c4.ln.sl 0009 0006 0000 "\x81Watch"    <- director seeds the list
```

### A complete list render has NO finalise frame

```
1017.585  ctrl -> rem   0i6233 c4.ln.sl 0009 0006 0000 "\x81Watch"
1017.646  rem  -> ctrl  0i0844 c4.ln.gi 0009 0000 0006 00
1017.772  ctrl -> rem   0r0844 000 "\x01Source A" "\x01Source B" "\x01Source C"
1017.851  rem  -> ctrl  0i0845 c4.ln.gi 0009 0003 0003 00
1018.397  ctrl -> rem   0r0845 000 "\x01Source D" "\x01Source E" "\x01Source F"
1018.378  rem  -> ctrl  0t0846 sa c4.zr.be 01 0009 0000 00000000   <- Watch released
   ...    (nothing addressed to the remote for 26.5 s)
```

**The controller side of a working list is exactly: `sl`, then one `0r` per
`gi`.** No commit, no finalise, no `le`. A 6-item list arriving as 3 + 3 is a
**transport** chunk (the ~70-byte frame limit), not a UI page; the user sees
all six at once. The third `sl` field is the **initial cursor index**.

Turnaround is not a constraint the SR-260 enforces: page 2 above was answered
546 ms late, behind ~20 retransmissions, and the remote rendered anyway. The
director itself sometimes leaves a page unanswered. Do not treat any single
timing as a requirement.

### Never use list ID `0x0001`

The SR-260 accepts it but draws row 1 of that list centered instead of
left-aligned. Every other row, and every other ID, renders normally; the HA
side sent byte-identical frames in the good and bad cases, only the ID
differed. No director capture has ever used 1 (seen: `0002`, `0003`, `0006`,
`0007`, `0009`). A counter that restarts at 1 shows this as "the first list
after a restart looks wrong". `_next_list_id_value` skips 1 for both models.

### The `bb`/`bh`/`be` tail

`c4.zr.bb <btn> <listID> <selIdx> <holdMs>`. The second and third fields are
the list on screen and its cursor (`0000 0000` with no list up). The **fourth
is a hold timer in milliseconds**: a held Vol+ ran `0x1a2`=418, `0x58c`=1420,
`0x781`=1921, `0x976`=2422, `0xb6b`=2923, release `0xd0b`=3339. So `bh`
repeats about every **501 ms** on both models, not ~100 ms. (The SR-250 sends
three arguments, `bb 02 0000 0000`, with no hold field.)

Keys pressed while a list is up still arrive as normal `bb`/`be` with the
list id in the tail. The director ignores them, except **Info**, which sends
`c4.ln.ii <list> <idx>` instead of `bb 16`.

### Wake handshake: the director answers the pickup, it does not push

```
1014.309  rem  -> ctrl  0t0842 sa c4.zr.mot
1014.367  ctrl -> rem   0s6231 c4.zr.tm 12 01 3b 01
1014.519  ctrl -> rem   0s6232 c4.ln.ri "Living Room" "Media Player"
```

`mot` -> `tm` -> `ri`, ~60 ms and ~210 ms behind the wake, on **every** pickup.
A source changed from the app while the remote sleeps is **not** pushed to it;
the director waits for the next `mot` and answers it, so the remote briefly
shows its stale row 2 and then corrects. A quirk should do the same: cache the
room and source, and answer `mot` with `tm` + `ri`. A send to a sleeping
remote fails `MAC_NO_ACK_RECEIVED`, which is expected.

`tm <hh> <mm> <ss> 01` goes to the SR-260 and `tm <hh> <mm> <ss>` (three bytes)
to the SR-250. The fourth byte is `01` on every weekday seen, so it is not day
of week.

### The EP-2 check-in must be answered

The remote checks in on EP 2 every `ast` seconds (60 s by default). The
director answers **`01 <tsn> 04` on EP 196 -> 196**, profile `0xC25D`, cluster
`0x0001`, about 10 ms later, using its own tsn counter. Left unanswered, a
remote gives up after about three check-ins and **rejoins** — a rejoin every
few minutes is this missing reply, not normal behaviour. The framing differs:

- SR-250: cluster-specific, `<fc> <tsn> 03 ...`.
- SR-260: manufacturer-specific, `1d 40 10 <tsn> 03 ...` (manufacturer code
  `0x1040` between frame control and tsn).

The same 3-byte frame also follows almost every completed exchange.

### Wake and rejoin queries

A battery pull showed three more questions, all on EP 2:

| Remote asks | Director answers |
|---|---|
| Read Attributes 0x0008, 0x0009, 0x000A (profile 0xC25D) | EP 196 → 1: 0x0008 = 0, 0x0009 = controller IEEE, 0x000A = 0 |
| Read Attributes 0x000C, 0x0001 (profile 0xC25D) | EP 196 → 1, frame control 0x10: 0x000C = 0x14, 0x0001 = 0x0123 |
| `19 <tsn> 00` with attrs 0x0003 = 1, 0x0001 = `"2.2.50"` (profile 0xC25E, cluster 0x0003) | EP 240 → 240: `01 <tsn> 01 00 00 20 01` |

The answers never vary across remotes or captures, and the director never
echoes the tsn. 0x000C = 0x14 matched the director network's own channel;
answering with the coordinator's own channel works, so don't copy `0x14`. The
0x000C/0x0001 read repeats about every 10 minutes even when answered: it is a
timed poll riding on a check-in wake, not a retry.

### The display is two rows, and that is a ceiling

The complete set of outbound `c4.ln.*` verbs a director emits for the idle
screen and its overlays is **`ri`, `sl`, `dm`, `le`, `sc`** (plus `si`, `slc`
in the menus below). `c4.ln.ri` carries exactly two fields; there is no verb
for a third persistent line.

| verb | what it does to the screen |
|---|---|
| `ri "<room>" "<source>"` | the idle screen. Row 1 room, row 2 active source. **The only persistent text.** |
| `dm <icon> "<msg>"` | a **transient full-screen splash** that replaces the idle screen and clears itself. |
| `le` | dismisses an overlay (splash or list). **Does not touch either row.** |
| `sl` / `gi` | the list overlay. |
| `sc <flag> <min> <max> <val> "<label>"` | a **self-clearing gauge overlay** drawn under rows 1/2, which stay legible. The volume bar. |

Consequences for a quirk:

- **Clearing row 2 needs `ri`.** Writing an empty display message sends `le`,
  which leaves the rows alone. `C4SR260DisplayCluster.set_room_info` (command
  `0x03`, `room` + `source`) exists for this.
- **Row 2 must be re-asserted, or it will not survive.** The bootstrap re-runs
  on every `Device_annce`, which the remote emits every few minutes; if it
  re-sends an empty source it wipes row 2 during normal use. Cache the source
  *unconditionally, including when empty* (empty means the room is off), and
  cache the room only when non-empty.
- **The second `ri` field** is the room's active source as the controller
  names it: `""` with nothing on, the source device's name otherwise (a music
  service reads as the source device, e.g. `"Digital Media"`). Picking the
  source that is already on sends no `ri`; `ri` lands when the choice does,
  `le` only when the submenus close.

### The volume overlay is `c4.ln.sc`

```
0i<seq> c4.ln.sc 0a 0 64 <value> "Volume"
```

| field | observed | reading |
|---|---|---|
| 1 | `0a` | **unknown** — constant across 27 frames |
| 2 | `0` | minimum, printed unpadded |
| 3 | `64` | maximum = 100, so `value` is a percentage |
| 4 | `2d`..`4c`, `25` | current value, hex |
| 5 | `"Volume"` | label |

Same transport as `dm`/`le`/`sl` (`0i`, EP 1 → 1), acked `0r<seq> 000`,
~40-110 ms after the button frame. It **composites** (rows stay legible) and
**self-clears** (no `le` follows). **Mute sends no `sc`**, so the bar tracks a
level *change*, not a keypress: drive it from the amplifier's reported volume,
not from the button. One `sc` per button frame, so a held key costs ~2 frames
per second. Exposed as `show_gauge` (command `0x04`).

**The SR-250 has no volume overlay.** A director-paired SR-250 was re-roomed
(from the remote) into a room where an SR-260 draws the bar; its volume keys
moved the volume with no bar on any source. With room, director and binding
held constant, the only variable was the model. `show_gauge` refuses on the
SR-250. (An SR-250's room can be changed from the remote, which is a useful
way to hold the room constant when comparing models.)

### Remote settings (`c4.zr.*`)

`0s<seq> c4.zr.<verb> <hex>` sets, `0g<seq> c4.zr.<verb>` reads, and the remote
answers `0r<seq> 000 c4.zr.<verb> <hex>`; a refused write answers `e00`. On
every wake with Composer's properties page open, the director reads blb, st,
ast, bl, kbl, ls, wom, bt and tc in that order (it may poll like that only
while the page is open, so don't copy it).

| Verb | Meaning | Values |
|---|---|---|
| `blb` | Screen level | percent |
| `kbl` | Keypad level | percent |
| `bl` | Battery level (read only) | percent |
| `ls` | Light sensor | 00 off, 01 on |
| `st` | Awake duration | seconds |
| `ast` | Check-in interval | seconds |
| `wom` | Wake on motion | 00 Off, 01 High, 02 Low, 03 Medium |
| `bt` | Battery type ("Enable Recharge Station") | 00 AA alkaline, 01 Li-Po pack |
| `tc` | Text colour | RGB332 byte, e.g. 7b Sky Blue (default) |

`bt` tells the remote which battery is fitted; with it off, a docked pack does
not charge. `c4.sy.fwv` reads the firmware version. The `bl` value announced
unprompted (`sa c4.zr.bl <pct>`) is **battery percent**, not backlight: the
director's Battery Level gauge draws exactly the `bl` it just read. The
`0xC25D` status report also carries a battery-like attribute 0x0015 (int8),
but it reads several points high, so use it only as a fallback.

**Beep** is `0i c4.zr.fr 01 <seconds>`: `ff` until a key is pressed (Find
Remote), `0a` 10 s, `1e` 30 s, `00` stop. Identical on both models. A remote
that is lost is usually asleep, so hold a beep that fails delivery and re-send
it after the next check-in ack or pickup.

### List > Settings is drawn by the director

Every screen comes from the controller: Settings (Config, About), then Config
(Display Brightness, Keypad Brightness, Motion Detect, Light Sensor, Text
Color, Battery Level, Recharge Station, Factory Defaults).
`c4_settings_menu.py` rebuilds it, minus Factory Defaults.

- **Item glyphs:** `0x82` opens a submenu, `0x01` is a leaf, `0x03` is the
  leaf holding the current value (drawn as a check mark). The remote draws the
  cursor and the "n/m" counter itself.
- **`is <list> <idx> <flag>`:** `0001` is Select, `0000` is Right. `ise` with
  the same fields is the release; `ish` appears between them on a hold.
- **`c4.ln.lb <list> <idx>`** is Back (Left). The director redraws the parent
  list with its cursor on the item left.
- **Pick lists:** the director sends the `0s` setting, reads it back with `0g`,
  then redraws the same list with the new check mark.
- **Updating one row in place:** `0s c4.ln.si <list> <idx> "<glyph>label"`,
  without reopening the list (used after a toggle, and for a now-playing row).
- **Sliders:** `0i c4.ln.slc 04 <id> "<title>"`, id 00 Display, 01 Keypad. The
  remote reports `cc 04 <v>` as the level moves and `cs 04 <v>` (Select) or
  `cn 0004 <v>` (Cancel) on exit. **No `blb`/`kbl` write follows:** the remote
  stores the level itself.
- **Battery gauge:** `0i c4.ln.sc 64 00 64 <pct> "Battery Level"`, dismissed
  with `cs 64 <pct>`. A different flag from the volume bar's `0a 0 64`.
- **Text Color** is two levels: six groups, then the colours in each.
- **About** is a read-only list: Director version, Firmware version, MAC.
- **Room change:** Right/Left on the top list (`is 0001 0000 0000` /
  `lb 0001 0000`) steps rooms; the director answers `dm 5a "Loading Room..."`,
  then `ri "<room>" ""`, then that room's top list.
- **The red Control4 key** (`bb 02`) closes any list with `le`, then sends
  `ri "<room>" "<controller HDMI output>"`.
- **Status-only rows** use a `-` glyph and are cut ~4 characters shorter than
  leaf rows. **Unsupported screens** are a plain list titled `Unsupported`.
- **Long lists** end in a `More...` leaf. Labels are raw UTF-8 and cut by the
  director at about 20-23 characters (titles ~21-26); the screen itself clips
  by pixel width, so an exact limit is not needed.
- **Now playing:** the current track is marked with `♫ ` (U+266B, raw UTF-8
  `e2 99 ab 20`) on the SR-260 and with the escape `\4` on the SR-250. The
  director re-pushes whatever list is open on every track change as a fresh
  `sl`; send the final state, not each step.

### SR-250 dialect differences

- **List replies carry a per-frame item count and no glyph:**
  `0r<seq> 000 0003 "a" "b" "c"`. The list title takes no glyph either.
  Glyphs are text escapes instead: `\c` opens a submenu, `\<` in a title draws
  a back arrow, `\0` marks the current item or a toggle that is on.
- `is` and `ise` carry no trailing `0001`. **Right sends nothing inside a
  list**, so Select is the only way into a submenu.
- **Settings:** the remote answers `ast` and `ls` with `e00`; `st` reads back
  two bytes (`0f 0f`); `wom` takes only 00 Off / 01 On; there is no `tc`. The
  firmware reads back from `c4.sy.fwv` as e.g. `03.26.17`. Config sends
  `c4.ln.sc 0e 0 0 0 ""`, which hands over to the remote's built-in setup
  screens.
- **Rooms** change through a Change Rooms menu (floor, then room), or from
  the top list as above.

### Pairing an SR-260 to a non-Control4 coordinator

A freshly joined SR-260 never broadcasts its model until it has been
bootstrapped, and the quirk cannot bind (and so cannot bootstrap it) until it
knows the model. The SR-250 escapes this loop by announcing itself unprompted;
the SR-260 does not, and re-pairing alone does not help. Break the loop once by
seeding the IEEE → model store (`.storage/c4_quirk_data.json`) with
`"<ieee>": "C4-SR260"` **with Home Assistant stopped** (the store is rewritten
from memory on every model sniff). A director-joined SR-260 renders list row
text without being sent the glyph bitmaps (`c4.ln.ci`), so none are needed.

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
  payload is `hh mm ss` as raw hex bytes, plus a constant `01` for the SR-260
  only. Answer every `mot` with `tm` then `ri`, as the director does.
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
- **Answer the EP-2 check-in** (`01 <tsn> 04` on EP 196), or the remote
  rejoins every few minutes. See "Measured on the wire".
- Framing / sequence / encryption is the same C4-over-Zigbee envelope the
  rest of `zhaquirks/control4/` already handles.
