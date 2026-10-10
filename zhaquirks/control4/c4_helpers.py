"""Shared constants, store, helper utilities, and shared clusters for Control4 ZHA quirks.

Shared clusters defined here (used by 2+ device files):
  C4DimmerManufCluster  — EP 1 manufacturer cluster (all devices)
  C4ConfigCluster       — EP 2 / EP 196 config cluster (dimmer, switch, scene controller)
"""

import asyncio
import datetime
import json
import logging
import os
import re
import struct
import sys
import time

# Make this directory importable by sibling modules regardless of load order.
_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import zigpy.exceptions
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import Basic, LevelControl, OnOff

from zhaquirks.const import (
    BUTTON,
    BUTTON_1, BUTTON_2, BUTTON_3, BUTTON_4,
    BUTTON_5, BUTTON_6, BUTTON_7, BUTTON_8,
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    DIM_DOWN,
    DIM_UP,
    DOUBLE_PRESS,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    LONG_PRESS,
    LONG_RELEASE,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SHORT_PRESS,
    SHORT_RELEASE,
    TRIPLE_PRESS,
    TURN_OFF,
    TURN_ON,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profile / cluster IDs
# ---------------------------------------------------------------------------
C4_PROFILE_NETWORK  = 0xC25D
C4_PROFILE_BUTTON   = 0xC25C
C4_PROFILE_OUTLET   = 0xC25E   # EP 198 profile on LOZ-5S1-W
C4_PROFILES         = {C4_PROFILE_NETWORK, C4_PROFILE_BUTTON, C4_PROFILE_OUTLET}
C4_IEEE_PREFIX      = "00:0f:ff"
C4_MANUF_CLUSTER    = 0xFFFF
C4_CLUSTER_ID       = 0x0001   # C4 serial-over-ZigBee cluster (wire ID)
C4_CONFIG_CLUSTER_ID = 0xFC41  # ZHA-side virtual cluster for C4 config (avoids PowerConfiguration clash)
C4_BUTTON_CLUSTER_ID = 0xFC42  # ZHA-side virtual cluster for button events
C4_DISPLAY_CLUSTER_ID = 0xFC47  # ZHA-side virtual cluster for SR260 LCD message / menu

# ---------------------------------------------------------------------------
# Transition times and defaults (from Rev E provisioning capture)
# ---------------------------------------------------------------------------
C4_ON_TRANSITION   = 8    # 800 ms (1/10-s units) — nearest to 750 ms device on-ramp
C4_OFF_TRANSITION  = 20   # 2000 ms — matches device off-ramp exactly
C4_DEFAULT_ON_LEVEL = 191 # ~75 % of 254

# Delay between provisioning commands sent during bind()
C4_PROVISION_DELAY = 0.05  # seconds

# ---------------------------------------------------------------------------
# C4 operational attribute IDs
# ---------------------------------------------------------------------------
C4_ATTR_DIM_LEVEL = 0x0000
C4_ATTR_MODEL     = 0x0007
C4_ATTR_FIRMWARE  = 0x0004
# Increments once per power cycle; unmoved by a network rejoin or a factory
# reset. Decoded from a 0xC25D status-report capture.
C4_ATTR_POWER_CYCLE_COUNT = 0x0006
# The controller's address as the remote holds it: `02 00 00` once
# provisioned, zero-length while it has none.
C4_ATTR_CONTROLLER_ADDR = 0x0012

# ---------------------------------------------------------------------------
# C4 endpoint interview defaults (injected instead of Simple_Desc_req)
# ---------------------------------------------------------------------------
C4_ENDPOINT_DEFAULTS = {
    2: {
        "profile_id":  C4_PROFILE_NETWORK,
        "device_type": 0x0000,
        "in_clusters": [C4_CLUSTER_ID],
        "out_clusters": [],
    },
    196: {
        "profile_id":  C4_PROFILE_NETWORK,
        "device_type": 0x0000,
        "in_clusters": [C4_CLUSTER_ID],
        "out_clusters": [],
    },
    197: {
        "profile_id":  C4_PROFILE_BUTTON,
        "device_type": 0x0000,
        "in_clusters": [C4_CLUSTER_ID],
        "out_clusters": [],
    },
}

# ---------------------------------------------------------------------------
# Button / event maps
# ---------------------------------------------------------------------------

# Button IDs from c4.dmx.bp / c4.dmx.cc captures
DIMMER_BUTTON_MAP = {
    0x00: "top",
    0x01: "top",     # ON  button
    0x05: "bottom",  # OFF button
}

# C4-KC120277: 8 physical buttons, 0-indexed from top
KC120277_BUTTON_MAP = {
    0x00: BUTTON_1,
    0x01: BUTTON_2,
    0x02: BUTTON_3,
    0x03: BUTTON_4,
    0x04: BUTTON_5,
    0x05: BUTTON_6,
    0x06: BUTTON_7,
    0x07: BUTTON_8,
}

DIMMER_EVENT_MAP = {
    "hc": LONG_PRESS,
    "he": LONG_RELEASE,
    "cc": "click_count",
    # SR260 remote button-begin / button-end events (c4.zr.bb / c4.zr.be)
    "bb": SHORT_PRESS,
    "be": SHORT_RELEASE,
}

# Virtual endpoint IDs for KC120277 per-button Event entities (ZHA-side only)
KC120277_BUTTON_EP_MAP: dict[int, int] = {
    btn_id: 200 + btn_id for btn_id in range(8)
}

# C4-SR260: 50 button codes (0x00..0x31) — see
# documentation/control4-sr260-remote-protocol.md for the layout.
SR260_BUTTON_MAP: dict[int, str] = {
    0x00: "room_off",
    0x01: "watch",
    0x02: "control4",
    0x03: "listen",
    0x04: "list",
    0x05: "i",
    0x06: "ii",
    0x07: "iii",
    0x08: "guide",
    0x09: "page_up",
    0x0a: "page_down",
    0x0b: "prev",
    0x0c: "volume_up",
    0x0d: "up",
    0x0e: "channel_up",
    0x0f: "left",
    0x10: "select",
    0x11: "right",
    0x12: "volume_down",
    0x13: "down",
    0x14: "channel_down",
    0x15: "volume_mute",
    0x16: "info",
    0x17: "menu",
    0x18: "cancel",
    0x19: "reverse",
    0x1a: "dvr",
    0x1b: "forward",
    0x1c: "skip_back",
    0x1d: "play",
    0x1e: "skip_forward",
    0x1f: "record",
    0x20: "pause",
    0x21: "stop",
    0x22: "red",
    0x23: "green",
    0x24: "yellow",
    0x25: "blue",
    0x26: "digit_1",
    0x27: "digit_2",
    0x28: "digit_3",
    0x29: "digit_4",
    0x2a: "digit_5",
    0x2b: "digit_6",
    0x2c: "digit_7",
    0x2d: "digit_8",
    0x2e: "digit_9",
    0x2f: "star",
    0x30: "digit_0",
    0x31: "hash",
}

# Virtual endpoint IDs for SR260 per-button Event entities (EPs 100..149,
# all within Zigbee's 1..240 application range).
SR260_BUTTON_EP_MAP: dict[int, int] = {
    btn_id: 100 + btn_id for btn_id in SR260_BUTTON_MAP
}

# LOZ-5S1-W: outlet index → endpoint id
OUTLET_EP_MAP = {0x00: 1, 0x01: 11}

_INVALID_MODELS = {"", "unknown", "unk_model", "none", "None"}

# ---------------------------------------------------------------------------
# IEEE → model persistence store
# ---------------------------------------------------------------------------
_C4_STORE_PATH = "/config/.storage/c4_quirk_data.json"


def _load_store() -> dict:
    try:
        with open(_C4_STORE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_C4_IEEE_MODEL_MAP: dict[str, str] = _load_store()


def _save_store(data: dict) -> None:
    os.makedirs(os.path.dirname(_C4_STORE_PATH), exist_ok=True)
    _LOGGER.debug("C4: saving IEEE→model map to %s: %s", _C4_STORE_PATH, data)
    with open(_C4_STORE_PATH, "w") as f:
        json.dump(data, f)


def get_model_from_ieee(key: str) -> str | None:
    return _C4_IEEE_MODEL_MAP.get(key)


def set_model_for_ieee(key: str, value: str) -> None:
    _C4_IEEE_MODEL_MAP[key] = value
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Called from a sync context with no running loop — save synchronously.
        _save_store(dict(_C4_IEEE_MODEL_MAP))
        return
    loop.run_in_executor(None, _save_store, dict(_C4_IEEE_MODEL_MAP))


# ---------------------------------------------------------------------------
# IEEE → Z2IO device settings persistence
# ---------------------------------------------------------------------------
_C4_Z2IO_SETTINGS_PATH = "/config/.storage/c4_z2io_settings.json"


def _load_z2io_settings() -> dict:
    try:
        with open(_C4_Z2IO_SETTINGS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


_C4_Z2IO_SETTINGS: dict[str, dict] = _load_z2io_settings()


def _save_z2io_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(_C4_Z2IO_SETTINGS_PATH), exist_ok=True)
    _LOGGER.debug("C4: saving Z2IO settings to %s: %s", _C4_Z2IO_SETTINGS_PATH, data)
    with open(_C4_Z2IO_SETTINGS_PATH, "w") as f:
        json.dump(data, f)


def get_z2io_opt_mode(ieee: str) -> int | None:
    """Return the persisted opt_mode for a Z2IO device, or None if not set."""
    entry = _C4_Z2IO_SETTINGS.get(ieee)
    if isinstance(entry, dict):
        return entry.get("opt_mode")
    return None


def set_z2io_opt_mode(ieee: str, mode: int) -> None:
    """Persist the opt_mode for a Z2IO device."""
    entry = _C4_Z2IO_SETTINGS.setdefault(ieee, {})
    entry["opt_mode"] = mode
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _save_z2io_settings(dict(_C4_Z2IO_SETTINGS))
        return
    loop.run_in_executor(None, _save_z2io_settings, dict(_C4_Z2IO_SETTINGS))


# ---------------------------------------------------------------------------
# Frame / command helpers
# ---------------------------------------------------------------------------

def next_c4_seq(device) -> int:
    """Return the next 16-bit C4-transport sequence number for `device`.

    The C4 ASCII protocol embeds a 4-hex-digit sequence number after the
    `0s`/`0g`/`0r`/`0t` frame-type prefix and uses it as a de-duplication
    key.  All cluster modules sharing the same physical device must draw
    sequences from the same counter — colliding sequences are silently
    dropped by the device.

    Counter is lazily attached as `device._c4_seq` so it lives for the
    device's lifetime and is shared across every cluster on it.  The seed
    of 0x0040 matches what the original Control4 controller used.
    """
    seq = getattr(device, '_c4_seq', 0x0040)
    device._c4_seq = (seq + 1) & 0xFFFF
    return seq


def _build_c4_frame(seq_num, ascii_cmd: str) -> bytes:
    """Build a C4 serial-over-ZigBee APS payload (text command + CRLF).

    The APS header is generated automatically by device.request(); do NOT
    include it here.  seq_num is unused — zigpy manages the APS counter.

    Encoded as latin-1 so embedded 1-byte glyph codes in the 0x80–0xFF
    range (used as icon prefixes inside quoted args of `c4.ln.dm` /
    `c4.ln.sl` / `c4.ln.gi`-response) pass through unchanged.  Pure-ASCII
    commands encode identically to ASCII.
    """
    return (ascii_cmd + "\r\n").encode("latin-1")


# ---------------------------------------------------------------------------
# SR260 LCD: display-message helpers (c4.ln.dm / c4.ln.le)
# ---------------------------------------------------------------------------
#
# The SR260 remote shows a single-line message on its LCD when the controller
# sends:
#     0i<seq> c4.ln.dm <icon:u8> "<message>"\r\n
# and clears it (closes the splash) with:
#     0i<seq> c4.ln.le\r\n
#
# Observed in the init capture as `c4.ln.dm 5a "Loading Room..."`.  The icon
# byte is part of the same glyph table used for list-item label prefixes; 0x5a
# is the controller's default for transient splashes.
#
# Both verbs are sent on profile C4_PROFILE_BUTTON (0xC25C), cluster 0x0001,
# EP 1→1 — the same transport the dimmer / fan / LED quirks use for their
# 0s commands.  The remote does not return an Init response, so requests are
# fire-and-forget (expect_reply=False).

# Default icon byte for c4.ln.dm splashes.  0x5a is what the official C4
# controller used in the captured init sequence.
C4_DISPLAY_DEFAULT_ICON = 0x5A


async def _c4_send_display_message(
    device, message: str, icon: int = C4_DISPLAY_DEFAULT_ICON,
) -> None:
    """Push a one-line message to a Control4 device's LCD.

    Sends `0i<seq> c4.ln.dm <icon> "<message>"\r\n` on the C4 button profile.
    Embedded `"` is stripped and `\r` / `\n` are replaced with spaces so the
    framing isn't broken.  Empty `message` is rejected — call
    `_c4_send_clear_display` to dismiss an existing splash.
    """
    if not isinstance(message, str) or not message:
        raise ValueError("c4.ln.dm: message must be a non-empty string")

    # The frame is line-terminated with \r\n and quote-delimited, so any of
    # those three characters in the body would corrupt parsing.
    sanitised = (
        message.replace("\r", " ").replace("\n", " ").replace('"', "")
    )

    seq = next_c4_seq(device)
    cmd = f'0i{seq:04x} c4.ln.dm {icon:02x} "{sanitised}"'
    data = _build_c4_frame(seq, cmd)

    _LOGGER.debug(
        "C4 display: send dm icon=0x%02x msg=%r seq=0x%04x", icon, sanitised, seq,
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


async def _c4_send_clear_display(device) -> None:
    """Dismiss an active LCD splash / list view via `0i<seq> c4.ln.le\r\n`."""
    seq = next_c4_seq(device)
    cmd = f"0i{seq:04x} c4.ln.le"
    data = _build_c4_frame(seq, cmd)

    _LOGGER.debug("C4 display: send le (clear) seq=0x%04x", seq)
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


async def _c4_send_find_remote(device, seconds: int = 0xFF) -> None:
    """Beep the remote: `0i<seq> c4.zr.fr 01 <seconds>` at EP 1.

    MEASURED on both models (`01 ff`) and on the SR-260 for every duration
    Composer offers, each acked `000`:

        01 ff   beep until a key is pressed on the remote (Find Remote)
        01 0a   beep for 10 s
        01 1e   beep for 30 s
        01 00   stop beeping (Composer's "Stop Beep")

    So the last byte is a duration in seconds, 0xFF meaning "until a key",
    and 0 is the stop command. A key press cancels any of them on its own.
    The leading `01` never varied.
    """
    seq = next_c4_seq(device)
    cmd = f"0i{seq:04x} c4.zr.fr 01 {max(0, min(int(seconds), 0xFF)):02x}"
    data = _build_c4_frame(seq, cmd)

    _LOGGER.info(
        "C4: send find-remote %d bytes: %r", len(data), bytes(data),
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


# A lost remote is an asleep one, and a send to a sleeping remote fails with
# MAC_NO_ACK. So a beep that does not land is held on the device and re-sent
# right after its next check-in (~60 s) or pickup, the moments it is known to
# be awake. The TTL stops a remote that was flat or out of range from starting
# to beep hours after anyone asked.
C4_PENDING_BEEP_TTL_S = 600.0


async def c4_request_beep(device, seconds: int = 0xFF) -> bool:
    """Beep now if the remote is awake, else hold it for the next wake.

    Returns True if delivered now. A stop (0) is never held: a remote that is
    asleep is not beeping, and the stop still cancels a held beep.
    """
    device._c4_pending_beep = None
    try:
        await _c4_send_find_remote(device, seconds)
        return True
    except zigpy.exceptions.DeliveryError as exc:
        if seconds:
            device._c4_pending_beep = (int(seconds), time.monotonic())
            _LOGGER.info(
                "C4 [%s]: beep %s held for the next wake (%s)",
                device.ieee, seconds, exc,
            )
        return False


async def c4_flush_pending_beep(device) -> None:
    """Send a held beep, if any. Call only when the remote has just spoken."""
    pending = getattr(device, "_c4_pending_beep", None)
    if pending is None:
        return
    seconds, since = pending
    if time.monotonic() - since > C4_PENDING_BEEP_TTL_S:
        device._c4_pending_beep = None
        _LOGGER.warning("C4 [%s]: held beep expired undelivered", device.ieee)
        return
    try:
        await _c4_send_find_remote(device, seconds)
    except Exception as exc:  # still asleep, or busy: stay held for the next wake
        _LOGGER.info("C4 [%s]: held beep still undelivered (%s)", device.ieee, exc)
        return
    if getattr(device, "_c4_pending_beep", None) is pending:
        device._c4_pending_beep = None
    _LOGGER.info(
        "C4 [%s]: held beep delivered after %.0f s",
        device.ieee, time.monotonic() - since,
    )


# `c4.zr.*` remote settings, as Composer's SR-260 Properties page sends them:
# `0s<seq> c4.zr.<verb> <hex>` to set, `0g<seq> c4.zr.<verb>` to read; the
# remote answers a read with `0r<seq> 000 c4.zr.<verb> <hex>`. Measured
# off a director-paired SR-260 (blb screen %, kbl keypad %, ls light
# sensor, st awake s, ast check-in s, wom wake-on-motion, bt recharge station,
# tc RGB332 text colour). The verb and value are validated rather than passed
# through, so a typo cannot put an arbitrary frame on the air.
_C4_SETTING_VERB = re.compile(r"[a-z]{1,6}")
_C4_SETTING_VALUE = re.compile(r"[0-9a-f]{2}( [0-9a-f]{2}){0,3}")


def c4_setting_frame(device, verb: str, value: str | None = None, ns: str = "zr"):
    """Build a `0s` (value given) or `0g` (value None) settings frame.

    `ns` is `zr` for remote settings or `sy` for system reads (`c4.sy.fwv`).
    Returns `(seq, text)`. Raises ValueError on a malformed verb or value.
    """
    verb = (verb or "").strip().lower()
    if ns not in ("zr", "sy") or not _C4_SETTING_VERB.fullmatch(verb):
        raise ValueError(f"c4.{ns} setting: bad verb {verb!r}")
    seq = next_c4_seq(device)
    if value is None:
        return seq, f"0g{seq:04x} c4.{ns}.{verb}"
    value = value.strip().lower()
    if not _C4_SETTING_VALUE.fullmatch(value):
        raise ValueError(f"c4.{ns}.{verb}: bad value {value!r} (want hex bytes)")
    return seq, f"0s{seq:04x} c4.{ns}.{verb} {value}"


async def _c4_send_setting_frame(device, text: str) -> None:
    """Send a frame from `c4_setting_frame` at EP 1 -> 1, like `c4.zr.fr`."""
    _LOGGER.info("C4: send setting %r", text)
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=_build_c4_frame(0, text),
        expect_reply=False,
    )

# A reply to one of our reads: `0r<seq> 000 c4.zr.<verb> <hex>`, or `e00` when
# the remote refuses (the SR-250 for `ast`/`ls`, the SR-260 for a `bt` change
# it rejects). `c4.sy.fwv` replies with a dotted version, hence `\S+`.
_C4_SETTING_REPLY = re.compile(
    r"0r([0-9a-f]{4}) ([0-9a-z]{3})(?: c4\.(zr|sy)\.([a-z]+)((?: \S+)*))?"
)


def c4_parse_setting_reply(text: str):
    """Parse a settings reply into `(seq, status, verb, value)`, or None.

    `verb` and `value` are None for a bare ack (a write's `000`, or `e00`).
    """
    # The SR-260's c4.sy.fwv reply arrives as "...2.2.50\r\n\x00\x00".
    m = _C4_SETTING_REPLY.fullmatch((text or "").replace("\x00", "").strip())
    if not m:
        return None
    seq, status, _ns, verb, value = m.groups()
    return seq, status, verb, (value.strip() if verb else None)


# The 0xC25D status report a remote sends about hourly carries battery as
# attribute 0x0015 (int8, percent), right after 0x0013 and 0x0014. Anchored on
# all three so a stray 0x15 byte elsewhere in the frame cannot match. Only a
# fallback: it ran 93-100 while the director's gauge said 90-91, and the gauge
# is `c4.zr.bl`, read live.
_C4_STATUS_BATTERY = re.compile(
    rb"\x13\x00\x28.\x14\x00\x20.\x15\x00\x28(.)", re.DOTALL,
)


def c4_battery_from_status(msg: bytes):
    """Battery percent from a 0xC25D status report, or None."""
    m = _C4_STATUS_BATTERY.search(bytes(msg or b""))
    if not m:
        return None
    pct = m.group(1)[0]
    return pct if 0 <= pct <= 100 else None


async def _c4_send_slider(device, slider_id: int, title: str) -> None:
    """Open the remote's own level slider: `0i<seq> c4.ln.slc 04 <id> "<title>"`.

    The remote applies and stores the level itself, so nothing is written
    afterwards. Measured: id 00 is Display Brightness, 01 Keypad Brightness.
    """
    seq = next_c4_seq(device)
    safe = str(title).replace("\r", " ").replace("\n", " ").replace('"', "")
    await _c4_send_raw(device, f'0i{seq:04x} c4.ln.slc 04 {slider_id:02x} "{safe}"')


async def _c4_send_battery_gauge(device, percent: int) -> None:
    """Draw the battery screen: `0i<seq> c4.ln.sc 64 00 64 <pct> "Battery Level"`.

    A different flag (0x64) and a two-digit minimum from the volume bar's
    `0a 0 64`, exactly as the director sent it.
    """
    pct = max(0, min(100, int(percent)))
    seq = next_c4_seq(device)
    await _c4_send_raw(device, f'0i{seq:04x} c4.ln.sc 64 00 64 {pct:02x} "Battery Level"')


C4_GAUGE_DEFAULT_LABEL = "Volume"


async def _c4_send_gauge(
    device, value: int, label: str = C4_GAUGE_DEFAULT_LABEL,
) -> None:
    r"""Draw the remote's native bar-gauge overlay via `c4.ln.sc`.

    Sends `0i<seq> c4.ln.sc <flag> <min> <max> <value> "<label>"\r\n` at
    EP 1 -> 1, the same transport and `0i` family as `c4.ln.dm` / `c4.ln.le`.
    The remote acks `0r<seq> 000`; we do not wait for it.

    This is the volume bar a real Control4 director draws, measured off a
    director-paired SR-260. Two properties that make it the right verb and
    are easy to assume wrongly:

    * It **composites**. Rows 1 and 2 (`c4.ln.ri`) stay legible underneath, so
      this does not disturb the room/source the bootstrap owns. `c4.ln.dm`
      does NOT behave this way -- it replaces the idle screen.
    * It **self-clears**. Do not follow it with `c4.ln.le`: the director does
      not, and `le` would tear down an open list if one happened to be up.

    The director sends one `sc` per button frame and nothing at all on mute,
    so the overlay tracks a level CHANGE rather than a keypress.

    Raises `ValueError` for a model with no gauge form -- the SR-250, which
    has NO volume overlay at all. See `_C4_LIST_DIALECT`.
    """
    gauge = c4_list_dialect(device).get("gauge")
    if not gauge:
        raise ValueError(
            f"c4.ln.sc: no gauge form for model "
            f"{getattr(device, 'model', None)!r} "
            f"(the SR-250 has no volume overlay)"
        )

    lo, hi = gauge["minimum"], gauge["maximum"]
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"c4.ln.sc: value {value!r} is not an integer") from None
    value = max(lo, min(hi, value))

    # Same framing hazards as every other quoted C4 arg.
    sanitised = (
        str(label).replace("\r", " ").replace("\n", " ").replace('"', "")
    )

    seq = next_c4_seq(device)
    # Field widths reproduce the captured bytes exactly: flag zero-padded to
    # two hex digits, minimum unpadded (it is `0` on the wire), maximum and
    # value zero-padded to two. `0a 0 64 2d` is what the director sent.
    cmd = (
        f'0i{seq:04x} c4.ln.sc {gauge["flag"]:02x} {lo:x} {hi:02x} '
        f'{value:02x} "{sanitised}"'
    )
    data = _build_c4_frame(seq, cmd)

    _LOGGER.debug(
        "C4 gauge: send sc value=%d label=%r seq=0x%04x", value, sanitised, seq,
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


async def _c4_send_room_info(device, room: str, source: str = "") -> None:
    """Set the SR260 LCD's room title (row 1) and active source (row 2).

    Sends `0s<seq> c4.ln.ri "<room>" "<source>"\r\n` on the C4 button profile.
    Both args are sanitised the same way as `_c4_send_display_message`'s
    message body — embedded `"` is stripped and `\r` / `\n` are replaced
    with spaces so the framing isn't broken.

    `source` may be empty (`""`) when no source is active — observed in
    init captures as `c4.ln.ri "Screen Porch" ""`.
    """
    def _sanitise(s: str) -> str:
        return (
            (s or "").replace("\r", " ").replace("\n", " ").replace('"', "")
        )

    room_s = _sanitise(room)
    source_s = _sanitise(source)

    seq = next_c4_seq(device)
    # Remember the room so a later selection can refill row 2 without having
    # to thread the name through every call path. MEASURED: the director sends
    # `c4.ln.ri "<room>" "<source>"` - room in field 1, ACTIVE SOURCE in
    # field 2 - and re-sends it on a source change.
    if room_s:
        device._c4_room = room_s

    # The SOURCE is cached UNCONDITIONALLY, including when empty, and that
    # asymmetry with the room above is deliberate. "" is a real value here - it
    # is what an off room looks like on row 2 - whereas an empty room is just a
    # caller with nothing to say, which must not erase a good room name.
    #
    # Without this cache row 2 has no owner between writes and is wiped every
    # few minutes: the remote emits a ZDO Device_annce periodically, that
    # re-arms the bootstrap latch, and the bootstrap re-sends `ri`. A real
    # director re-sends `ri "<room>" "<source>"` on EVERY wake with the source
    # filled in.
    device._c4_source = source_s

    cmd = f'0s{seq:04x} c4.ln.ri "{room_s}" "{source_s}"'
    data = _build_c4_frame(seq, cmd)

    # INFO and byte-level, matching the sl and gi send sites: log what goes
    # on the wire.
    _LOGGER.info(
        "C4 display: send ri room=%r source=%r %d bytes: %r",
        room_s, source_s, len(data), bytes(data),
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


async def _c4_send_list_header(
    device, list_id: int, count: int, sel_idx: int, title: str,
    icon: int | None = 0x81, items=None, item_icon=None,
) -> None:
    """Send `0i<seq> c4.ln.sl <list_id> <count> <sel_idx> "<icon><title>"\r\n`.

    Establishes a menu / list on the SR260's LCD.  The remote will respond
    with one or more `c4.ln.gi` page requests asking for the actual item
    labels, which the controller answers with `_c4_send_list_items_response`.

    All three integer args are 16-bit (sent as 4 hex digits).  `title` is
    sanitised the same way `_c4_send_display_message` sanitises its message
    so the framing stays parseable.

    `icon` is a 1-byte glyph code prefixed inside the quoted title (default
    `0x81`, the byte the official Control4 controller uses for the "Watch"
    header — see documentation/control4-sr260-remote-protocol.md).  Same
    glyph table as `c4.ln.dm` / list-item labels.

    `items`, when given, appends the row labels as further quoted strings
    after the title. The remote accepts this with 000 and then still asks
    `gi` for the items, so it buys nothing; see `_C4_SL_CARRIES_ITEMS`.
    Never push items repeatedly from the `gi` handler instead: that is a
    feedback loop (measured ~50 ms polling, 582 requests in 110 s).

    Callers must keep the whole frame inside the ~70-byte ceiling; with a
    5-char title and four short labels it lands at ~68.
    """
    if not 0 <= list_id <= 0xFFFF:
        raise ValueError(f"list_id out of range: {list_id}")
    if not 0 <= count <= 0xFFFF:
        raise ValueError(f"count out of range: {count}")
    if not 0 <= sel_idx <= 0xFFFF:
        raise ValueError(f"sel_idx out of range: {sel_idx}")
    if icon is not None and not 0 <= icon <= 0xFF:
        raise ValueError(f"icon out of range: {icon}")

    sanitised = (
        (title or "").replace("\r", " ").replace("\n", " ").replace('"', "")
    )

    seq = next_c4_seq(device)
    cmd = (
        f'0i{seq:04x} c4.ln.sl {list_id:04x} {count:04x} {sel_idx:04x} '
        f'"{"" if icon is None else chr(icon)}{sanitised}"'
    )
    if items:
        _gl = "" if item_icon is None else chr(item_icon & 0xFF)
        for _it in items:
            _safe = (
                str(_it).replace("\r", " ").replace("\n", " ").replace('"', "")
            )
            _cand = f'{cmd} "{_gl}{_safe}"'
            if len(_cand) + 2 > 70:
                _LOGGER.warning(
                    "C4 display: sl item %r dropped, frame would be %d bytes",
                    _it, len(_cand) + 2,
                )
                break
            cmd = _cand
    data = _build_c4_frame(seq, cmd)

    # INFO and byte-level, because this frame establishes the list the whole
    # gi exchange hangs off. Compare against a real director's:
    #     0i620f c4.ln.sl 0002 0006 0000 "\x81Watch"\r\n
    _LOGGER.info(
        "C4 display: send sl on ep 1->1 profile=0x%04X cluster=0x%04X "
        "%d bytes: %r",
        C4_PROFILE_BUTTON, C4_CLUSTER_ID, len(data), bytes(data),
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1, dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


# --- Per-model list dialect -------------------------------------------------
#
# The two remotes do NOT speak the same list grammar, and both halves below are
# MEASURED off a real Control4 director driving that model:
#
#   SR-250   0r<seq> 000 <count> "Media Room" "Media Player" ...
#            c4.ln.sl 0002 0006 0000 "Watch"
#            -> explicit per-frame count, NO glyph anywhere
#
#   SR-260   0r<seq> 000 "\x01Media Room" "\x01Media Player" ...
#            c4.ln.sl 0002 0006 0000 "\x81Watch"
#            -> no count, glyph 0x01 on items and 0x81 on the title
#
# Neither dialect may be expressed as a global default: a constant that suits
# one model silently breaks the other. Select by model, and keep both
# measurements next to each other so a future edit cannot quietly overwrite
# one with the other.
#
# `reply_ep` is 1 for BOTH models and is not a guess. The endpoint is assigned
# by DIRECTION, not by request/response: the remote asks from EP 197 and the
# director answers on EP 1, on both models:
#   remote -> coordinator  src_ep=197 dst_ep=197  0i081a c4.ln.gi ...
#   coordinator -> remote  src_ep=1   dst_ep=1    0r081a 000 ...
# Deriving it from the receiving cluster sends the answer straight back to 197,
# where the remote silently discards it.
_C4_LIST_DIALECT = {
    "SR250": {"gi_form": "count",  "item_icon": None, "title_icon": None, "reply_ep": 1,
              # MEASURED: THE SR-250 HAS NO VOLUME OVERLAY. A director-paired
              # SR-250B was re-roomed (from the remote) into a room where an
              # SR-260 draws the bar; its volume keys moved the volume with no
              # bar on any source. Same room, director and binding, so the only
              # remaining variable was the model.
              #
              # So None here means "this model does not have the feature", and
              # `_c4_send_gauge` refuses. Do NOT fill it in with the SR-260
              # form: it would put frames on the air that nothing renders.
              "gauge": None},
    "SR260": {"gi_form": "status", "item_icon": 0x01, "title_icon": 0x81, "reply_ep": 1,
              # MEASURED, 27 frames, all identical in shape:
              #   0ib593 c4.ln.sc 0a 0 64 2d "Volume"
              # `0x64` = 100, so the value is a percentage. `flag` 0x0a was
              # constant across every frame, so its meaning is UNKNOWN -- icon,
              # control id and step all still fit. Do not document it as one.
              "gauge": {"flag": 0x0A, "minimum": 0, "maximum": 100}},
}
# The SR-260 is the safer fallback for an unknown model: it is the dialect the
# protocol documentation describes, and the one this quirk shipped with.
_C4_LIST_DIALECT_DEFAULT = _C4_LIST_DIALECT["SR260"]


# Row 1 of a remote's LCD, per remote. The bootstrap re-runs on every
# Device_annce and re-sends the room each time, so a runtime set_room_info
# cannot hold a room for long - the quirk has to know it. Rooms are read from
# a JSON object keyed by lower-case IEEE, e.g.
#     {"00:0f:ff:00:00:12:34:56": "Living Room"}
# A remote missing from it gets its model's DEFAULT_ROOM. Edit with HA stopped
# or restart afterwards; the file is read once at import.
_C4_ROOMS_PATH = "/config/.storage/c4_remote_rooms.json"


def _load_rooms() -> dict[str, str]:
    try:
        with open(_C4_ROOMS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


C4_REMOTE_ROOMS: dict[str, str] = _load_rooms()


def remote_room_for(device, default: str) -> str:
    """Return the configured row-1 room for this remote, or `default`."""
    return C4_REMOTE_ROOMS.get(str(getattr(device, "ieee", "")).lower(), default)


# Model-named aliases; an IEEE is unique, so both models share one map.
sr260_room_for = remote_room_for
sr250_room_for = remote_room_for


def c4_current_room(device, default: str = "") -> str:
    """Return the room name last sent to `device` via `c4.ln.ri`."""
    return getattr(device, "_c4_room", "") or default


def c4_current_source(device) -> str:
    """Return the active source last sent to `device` via `c4.ln.ri`.

    No `default` parameter, unlike `c4_current_room`: "" is a legitimate answer
    meaning the room is off, so there is nothing for a caller to substitute.
    A device that has never been sent `ri` also reads "", which renders the
    same and is the safe direction.
    """
    return getattr(device, "_c4_source", "")


def c4_list_dialect(device):
    """Return the measured list dialect for `device`'s model."""
    model = (getattr(device, "model", "") or "").upper().replace("-", "")
    for key, dialect in _C4_LIST_DIALECT.items():
        if key in model:
            return dialect
    return _C4_LIST_DIALECT_DEFAULT


async def _c4_send_list_items_response(
    device, request_seq: str, items, icon: int | None = 0x01,
    endpoint: int = 1, form: str = "status",
) -> bytes:
    """Reply to a `c4.ln.gi` request with the requested item labels.

    Two reply forms, one per model (see `c4_list_dialect`), both measured off
    a real director:

        "status"  0r<seq> 000 "<icon><item0>" "<icon><item1>" ...\r\n   (SR-260)
        "count"   0r<seq> 000 <n> "<item0>" "<item1>" ...\r\n          (SR-250)

    `<seq>` mirrors the request's seq. `<n>` is the number of items IN THIS
    FRAME as 4 hex digits, not the number requested. `icon=None` sends labels
    with no glyph prefix, which is what the SR-250 wants.

    `items` is an iterable of strings.  Embedded `"` is stripped so the
    quoting stays well-formed; `\r` / `\n` are replaced with spaces so the
    line terminator isn't broken.

    The encoded frame must fit in a single Zigbee APS payload — bellows
    raises `MESSAGE_TOO_LONG` (status 56) above ~75 bytes once NWK
    encryption overhead is added, so we greedily fit as many items as we
    can and leave the remote to re-page for the remainder. That is a size
    limit only: answering short on purpose is not required by either model.

    `endpoint` is both the source and destination endpoint of the reply, and
    defaults to **1**. Endpoint is set by DIRECTION, not by which endpoint the
    request arrived on: the `gi` arrives on EP 197, and a director answers
    EP 1 -> EP 1 on profile 0xC25C, cluster 0x0001. Replying to 197 is
    silently discarded.
    """
    icon_char = "" if icon is None else chr(icon & 0xFF)
    parts: list[str] = []
    for raw in items:
        s = str(raw if raw is not None else "")
        s = s.replace("\r", " ").replace("\n", " ").replace('"', "")
        parts.append(f'"{icon_char}{s}"')

    # Conservative cap — empirically, ~70 bytes fits reliably; bellows
    # rejects above ~75 once NWK security overhead is added.
    MAX_FRAME_LEN = 70

    # "count" uses a fixed-width placeholder, filled in once fit_count is
    # known; a 4-hex-digit field cannot change length, so the fit is unaffected.
    header = f"0r{request_seq} 000" + (" 0000" if form == "count" else "")
    # Pre-count: header + CRLF (2 bytes appended by _build_c4_frame).
    running_len = len(header) + 2
    fit_count = 0
    for part in parts:
        candidate_len = running_len + 1 + len(part)
        if candidate_len > MAX_FRAME_LEN:
            break
        running_len = candidate_len
        fit_count += 1

    if form == "count":
        header = f"0r{request_seq} 000 {fit_count:04x}"

    if fit_count > 0:
        body = " ".join(parts[:fit_count])
        cmd = f"{header} {body}"
    else:
        cmd = header

    data = _build_c4_frame(0, cmd)

    if fit_count < len(parts):
        # WARNING: a truncated reply is otherwise invisible at `info`.
        _LOGGER.warning(
            "C4 display: send gi response seq=%s items=%d/%d (chunked) "
            "icon=%r len=%d",
            request_seq, fit_count, len(parts), icon, len(data),
        )
    else:
        _LOGGER.debug(
            "C4 display: send gi response seq=%s items=%d icon=%r len=%d",
            request_seq, fit_count, icon, len(data),
        )

    # Logged here, from the values handed to device.request, rather than at
    # the caller: endpoint, form and icon are the fields a reply has silently
    # got wrong, and only these variables are evidence about the wire. `mod`
    # shows which copy of this module is loaded (a ZHA reload does not
    # re-import it; restart HA after editing).
    _LOGGER.info(
        "C4 display: gi response -> device.request src_ep=%d dst_ep=%d "
        "profile=0x%04X cluster=0x%04X len=%d form=%r icon=%r mod=%s",
        endpoint, endpoint, C4_PROFILE_BUTTON, C4_CLUSTER_ID, len(data),
        form, icon, __file__,
    )
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=endpoint, dst_ep=endpoint,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )

    # Returned so a caller can log the exact bytes under its own logger.
    return data


async def _c4_send_controller_identity(device, source="unknown", zcl_seq=None):
    """Send ZCL Read Attributes Response for attrs 0x0008/0x0009/0x000A on EP 2.

    APS payload (APS header generated by device.request()):
      08 [tsn] 01                   ZCL: global, server→client, Read Attr Rsp
      08 00 | 00 | 21 | 00 00       attr 0x0008 SUCCESS uint16 0x0000
      09 00 | 00 | f0 | [8 bytes]   attr 0x0009 SUCCESS EUI64  coordinator IEEE
      0a 00 | 00 | 20 | 02          attr 0x000A SUCCESS uint8  0x02
    """
    try:
        coordinator_ieee = device.application.state.node_info.ieee
    except AttributeError:
        coordinator_ieee = getattr(device.application, 'ieee', None)

    if coordinator_ieee is None:
        _LOGGER.error("C4 identity (%s): cannot determine coordinator IEEE", source)
        return

    # ZCL frame control 0x08: global, server→client, default response enabled
    zcl_hdr = struct.pack('<BBB', 0x08, zcl_seq & 0xFF, 0x01)

    body  = struct.pack('<HBBH', 0x0008, 0x00, 0x21, 0x0000)
    body += struct.pack('<HBB',  0x0009, 0x00, 0xF0)
    body += coordinator_ieee.serialize()
    body += struct.pack('<HBBB', 0x000A, 0x00, 0x20, 0x02)
    data = zcl_hdr + body

    _LOGGER.debug(
        "C4 identity (%s): Read Attr Rsp to %s ep2→2 zcl_seq=0x%02x IEEE=%s data=%s",
        source, device.ieee, zcl_seq, coordinator_ieee, data.hex(),
    )
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK, cluster=C4_CLUSTER_ID,
            src_ep=2, dst_ep=2,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
        _LOGGER.debug("C4 identity (%s): sent successfully", source)
    except Exception as e:
        _LOGGER.error("C4 identity (%s): failed — %s", source, e)


async def _c4_report_controller_identity(device, source="unknown", zcl_seq=None):
    """Send ZCL Report Attributes for attrs 0x0008/0x0009/0x000A on EP 2.

    Uses command 0x0A (Report Attributes) instead of 0x01 (Read Attr Rsp).
    Report Attributes records omit the status byte.
    """
    try:
        coordinator_ieee = device.application.state.node_info.ieee
    except AttributeError:
        coordinator_ieee = getattr(device.application, 'ieee', None)

    if coordinator_ieee is None:
        _LOGGER.error("C4 report identity (%s): cannot determine coordinator IEEE", source)
        return

    zcl_hdr = struct.pack('<BBB', 0x08, zcl_seq & 0xFF, 0x0A)

    body  = struct.pack('<HBH',  0x0008, 0x21, 0x0000)
    body += struct.pack('<HB',   0x0009, 0xF0)
    body += coordinator_ieee.serialize()
    body += struct.pack('<HBB',  0x000A, 0x20, 0x02)
    data = zcl_hdr + body

    _LOGGER.debug(
        "C4 report identity (%s): Report Attr to %s ep2→2 zcl_seq=0x%02x IEEE=%s data=%s",
        source, device.ieee, zcl_seq, coordinator_ieee, data.hex(),
    )
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK, cluster=C4_CLUSTER_ID,
            src_ep=2, dst_ep=2,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
        _LOGGER.debug("C4 report identity (%s): sent successfully", source)
    except Exception as e:
        _LOGGER.error("C4 report identity (%s): failed — %s", source, e)


async def _send_many_to_one_route_request(app) -> None:
    """Broadcast a ZigBee NWK Many-to-One Route Request from the coordinator.

    Tries bellows (EZSP) then zigpy-znp (TI ZNP).
    """
    if hasattr(app, '_ezsp'):
        try:
            await app._ezsp.sendManyToOneRouteRequest(
                concentratorType=0xFFF9, radius=5,
            )
            _LOGGER.debug("Many-to-One Route Request sent via EZSP")
            return
        except Exception as exc:
            _LOGGER.warning("EZSP sendManyToOneRouteRequest failed — %s", exc)

    if hasattr(app, '_znp'):
        try:
            import zigpy_znp.znp.commands as znp_c
            await app._znp.request(
                znp_c.ZDO.ExtRouteDisc.Req(Dst=0xFFFC, Options=0x08, Radius=5),
                RspSchema=znp_c.ZDO.ExtRouteDisc.Rsp,
            )
            _LOGGER.debug("Many-to-One Route Request sent via ZNP")
            return
        except Exception as exc:
            _LOGGER.warning("ZNP ExtRouteDisc failed — %s", exc)

    _LOGGER.warning(
        "_send_many_to_one_route_request: no supported radio backend found "
        "(tried EZSP, ZNP)"
    )


# ---------------------------------------------------------------------------
# Device / attribute state sync helpers
# ---------------------------------------------------------------------------

def _c4_persist_device(device, source="unknown"):
    """Trigger zigpy DB persistence for a device after model/manufacturer update."""
    app = device.application

    if hasattr(app, 'device_updated'):
        try:
            app.device_updated(device)
            _LOGGER.debug(
                "C4 persist (%s): device_updated() succeeded for %s model=%r",
                source, device.ieee, device.model,
            )
            return
        except Exception as e:
            _LOGGER.debug("C4 persist (%s): device_updated() raised %s", source, e)

    try:
        app.listener_event("device_updated", device)
        _LOGGER.debug(
            "C4 persist (%s): listener_event(device_updated) fired for %s model=%r",
            source, device.ieee, device.model,
        )
        return
    except Exception as e:
        _LOGGER.debug(
            "C4 persist (%s): listener_event(device_updated) raised %s", source, e
        )

    if hasattr(app, '_dblistener') and hasattr(app._dblistener, 'device_updated'):
        try:
            app._dblistener.device_updated(device)
            _LOGGER.debug(
                "C4 persist (%s): _dblistener.device_updated() succeeded for %s model=%r",
                source, device.ieee, device.model,
            )
            return
        except Exception as e:
            _LOGGER.debug(
                "C4 persist (%s): _dblistener.device_updated() raised %s", source, e
            )

    _LOGGER.error(
        "C4 persist (%s): ALL persistence attempts failed for %s — "
        "model=%r will be lost on restart",
        source, device.ieee, device.model,
    )


def _sync_ep1_level(device, level_raw: int, source="unknown"):
    """Push a dim level value to EP 1 LevelControl + OnOff attribute caches."""
    try:
        ep1 = device.endpoints.get(1)
        if ep1 is None:
            return
        level_cluster = ep1.in_clusters.get(LevelControl.cluster_id)
        onoff_cluster = ep1.in_clusters.get(OnOff.cluster_id)
        _LOGGER.debug("C4 sync (%s): level=%d", source, level_raw)
        if level_cluster is not None:
            level_cluster.update_attribute(
                LevelControl.AttributeDefs.current_level.id, level_raw
            )
        if onoff_cluster is not None:
            onoff_cluster.update_attribute(
                OnOff.AttributeDefs.on_off.id, level_raw > 0
            )
    except Exception:
        _LOGGER.error("C4 sync level (%s): failed", source, exc_info=True)


def _sync_ep1_onoff(device, is_on: bool, source="unknown"):
    """Push on/off state to EP 1 OnOff attribute cache (no level change)."""
    try:
        ep1 = device.endpoints.get(1)
        if ep1 is None:
            return
        onoff_cluster = ep1.in_clusters.get(OnOff.cluster_id)
        if onoff_cluster is not None:
            _LOGGER.debug("C4 sync (%s): on_off=%s", source, is_on)
            onoff_cluster.update_attribute(
                OnOff.AttributeDefs.on_off.id, is_on
            )
    except Exception:
        _LOGGER.error("C4 sync onoff (%s): failed", source, exc_info=True)


def _sync_ep1_model(device, model: str, source="unknown"):
    """Push model/manufacturer into the EP 1 Basic cluster attribute cache."""
    try:
        ep1 = device.endpoints.get(1)
        if ep1 is None:
            return
        basic = ep1.in_clusters.get(Basic.cluster_id)
        if basic is None:
            return
        basic._update_attribute(Basic.AttributeDefs.model.id, model)
        basic._update_attribute(Basic.AttributeDefs.manufacturer.id, "Control4")
        _LOGGER.debug("C4 sync (%s): basic model=%r", source, model)
    except Exception:
        _LOGGER.warning("C4 sync model (%s): failed", source, exc_info=True)


# ---------------------------------------------------------------------------
# Sniff model string from a ZCL Report Attributes payload
# ---------------------------------------------------------------------------

def _c4_schedule_handshake(device, source: str) -> None:
    """Send coordinator identity + MTORR to a C4 device, in the background."""
    async def _send_handshake(dev=device):
        try:
            await _c4_report_controller_identity(
                dev, source, zcl_seq=dev.get_sequence(),
            )
            _LOGGER.debug("C4 sniffer: identity sent for %s (%s)", dev.ieee, source)
        except Exception as e:
            _LOGGER.warning(
                "C4 sniffer: identity send failed for %s — %s", dev.ieee, e,
            )
        try:
            await _send_many_to_one_route_request(dev.application)
            _LOGGER.debug("C4 sniffer: MTORR sent for %s", dev.ieee)
        except Exception as e:
            _LOGGER.warning("C4 sniffer: MTORR failed for %s — %s", dev.ieee, e)
    asyncio.ensure_future(_send_handshake())


def _c4_sniff_model(device, inner: bytes) -> None:
    """Peek into a ZCL Report Attributes payload for attr 0x0007 (model string).

    Called from the broadcast intercept patch before any cluster routing.
    On success:
      - Caches the model in the IEEE→model store.
      - If device.model is not yet set, writes it and schedules DB persistence.
      - Schedules a coordinator identity (ReportAttributes) + MTORR handshake.
        This replaces the per-cluster bind() handshake: the handshake is now
        triggered reactively each time the device announces its model number,
        which happens on join, rejoin, and periodic keep-alive broadcasts.
    """
    try:
        hdr, remaining = foundation.ZCLHeader.deserialize(inner)
        if hdr.command_id != 0x0A:
            return
        while remaining:
            attr, remaining = foundation.Attribute.deserialize(remaining)
            if (
                attr.attrid == C4_ATTR_CONTROLLER_ADDR
                and isinstance(attr.value.value, (bytes, bytearray))
                and len(attr.value.value) == 0
            ):
                # The remote has no controller and is not asking for one, so
                # the model-report trigger below never fires. Offer it as the
                # Read Attr Rsp it would have got had it asked: the remote
                # rejects the Report form with UNSUPPORTED_ATTRIBUTE (0x82).
                # The handshake below still sends that Report, which is
                # harmless, because its MTORR is what builds the route back.
                _LOGGER.info(
                    "C4 sniffer: %s reports no controller (0x0012 empty) - "
                    "sending identity unprompted, raw=%s",
                    device.ieee, bytes(inner).hex(),
                )
                asyncio.ensure_future(_c4_send_controller_identity(
                    device, "empty_controller_addr", zcl_seq=hdr.tsn,
                ))
                _c4_schedule_handshake(device, "empty_controller_addr")
                continue
            if attr.attrid == C4_ATTR_MODEL and isinstance(attr.value.value, str):
                raw = attr.value.value
                parts = raw.split(":")
                model = parts[2] if len(parts) >= 3 else raw
                if model and model not in _INVALID_MODELS:
                    _LOGGER.debug(
                        "C4 sniffer: caching ieee=%r -> model=%r",
                        device.ieee, model,
                    )
                    set_model_for_ieee(str(device.ieee), model)

                    # Send coordinator identity + MTORR in response to every
                    # model-bearing ReportAttributes from this device.
                    _c4_schedule_handshake(device, f"model_report_{model}")

                if not device.model or device.model in _INVALID_MODELS:
                    device.model = model
                    device.manufacturer = "Control4"
                    _LOGGER.info(
                        "C4 sniffer: set device.model=%r manufacturer=%r on 0x%04X",
                        model, device.manufacturer, device.nwk,
                    )
                    _c4_persist_device(device, "sniffer_immediate")

                    async def _deferred_persist(dev=device):
                        await asyncio.sleep(5)
                        _LOGGER.debug(
                            "C4 sniffer: deferred persist for %s model=%r",
                            dev.ieee, dev.model,
                        )
                        _c4_persist_device(dev, "sniffer_deferred")
                    asyncio.ensure_future(_deferred_persist())
                else:
                    _LOGGER.debug(
                        "C4 sniffer: device.model already=%r on 0x%04X — not overwriting",
                        device.model, device.nwk,
                    )
                return
    except Exception:
        pass  # non-ZCL or malformed payload — ignore silently


# ---------------------------------------------------------------------------
# Shared clusters
# ---------------------------------------------------------------------------

class C4DimmerManufCluster(CustomCluster):
    """Manufacturer-specific cluster 0xFFFF on EP 1 (all C4 devices)."""

    cluster_id  = C4_MANUF_CLUSTER
    name        = "Control4 Manufacturer Specific"
    ep_attribute = "c4_dimmer_manuf"

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.debug("C4 manuf: hdr=%s args=%s", hdr, args)
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    def _update_attribute(self, attrid, value):
        _LOGGER.debug("C4 manuf attr: 0x%04X = %s", attrid, value)
        super()._update_attribute(attrid, value)


class C4ConfigCluster(CustomCluster):
    """Config/identity cluster on C4 proprietary endpoints (EP 2, EP 196).

    Used by: C4-APD120 dimmer, C4-SW120 switch, C4-KC120277 scene controller.
    The outlet variant (C4OutletConfigCluster) lives in control4_outlet.py.

    Uses a manufacturer-specific cluster ID (0xFC41) instead of the wire
    ID (0x0001) to prevent ZHA from assigning PowerConfigurationClusterHandler.
    """

    cluster_id   = C4_CONFIG_CLUSTER_ID
    name         = "Control4 Config"
    ep_attribute = "c4_config"
    _c4_custom_handler = True

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)

        if attrid == C4_ATTR_MODEL and isinstance(value, str):
            _LOGGER.debug(
                "C4 config model string: raw=%r ep=%s", value,
                self.endpoint.endpoint_id,
            )
            device = self.endpoint.device
            parts = value.split(':', 2)
            new_model = parts[2] if len(parts) >= 3 else value
            if not device.model or device.model in _INVALID_MODELS:
                device.model = new_model
                device.manufacturer = "Control4"
                _LOGGER.info(
                    "C4 config: set device.model=%r on %s",
                    device.model, device.ieee,
                )
                _c4_persist_device(device, "c4_config_attr")
            else:
                _LOGGER.debug(
                    "C4 config: device.model already=%r on %s — not overwriting",
                    device.model, device.ieee,
                )
            _sync_ep1_model(device, device.model, "c4_config_0007")

        elif attrid == C4_ATTR_FIRMWARE and isinstance(value, str):
            _LOGGER.info("C4 firmware: %s", value)

        elif attrid == C4_ATTR_DIM_LEVEL:
            level_raw = value if isinstance(value, int) else 0
            _LOGGER.debug(
                "C4 config: dim level report = %d (ep %s)",
                level_raw, self.endpoint.endpoint_id,
            )
            _sync_ep1_level(self.endpoint.device, level_raw, "ep2_report")

        else:
            _LOGGER.debug("C4 config: 0x%04X = %s", attrid, value)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.debug("C4 config request: hdr=%s", hdr)
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    def handle_message(self, hdr, args):
        # Intercept patch calls handle_message(None, raw_bytes). Use
        # self.deserialize() for full parsing (header + schema-aware body)
        # so super().handle_message() receives the structured args object
        # it expects (e.g. ReadAttributesResponse with .attribute_reports).
        raw_body = None
        if hdr is None and isinstance(args, (bytes, bytearray)) and len(args) >= 3:
            raw = bytes(args)
            try:
                hdr, args = self.deserialize(raw)
            except Exception as e:
                # Schema parse failed — recover header so we can still
                # dispatch based on command_id with raw body bytes.
                try:
                    hdr, raw_body = foundation.ZCLHeader.deserialize(raw)
                    args = raw_body
                except Exception as e2:
                    _LOGGER.warning(
                        "C4 config ep %s: failed to parse ZCL header: %s — raw=%s",
                        self.endpoint.endpoint_id, e2, raw.hex(),
                    )
                    return
                _LOGGER.debug(
                    "C4 config ep %s: schema deserialize failed (%s) — "
                    "falling back to raw body",
                    self.endpoint.endpoint_id, e,
                )
            else:
                # self.deserialize returns raw bytes for unknown commands.
                if isinstance(args, (bytes, bytearray)):
                    raw_body = args

        _LOGGER.debug(
            "C4 config handle_message: ep=%s cmd=0x%02x args=%s",
            self.endpoint.endpoint_id,
            hdr.command_id if hdr else -1,
            raw_body.hex() if isinstance(raw_body, (bytes, bytearray)) else repr(args),
        )

        # cmd 0x00: Read Attributes — device polling for controller identity
        if hdr.command_id == 0x00:
            _LOGGER.debug(
                "C4 config: Read Attributes on ep %s tsn=0x%02x — "
                "sending controller identity",
                self.endpoint.endpoint_id, hdr.tsn,
            )
            asyncio.ensure_future(
                _c4_send_controller_identity(
                    self.endpoint.device,
                    source="read_attr_response",
                    zcl_seq=hdr.tsn,
                )
            )
            return

        # cmd 0x01: Read Attributes Response — pass to super() so
        # zigpy's read_attributes() future resolves. Requires parsed args.
        if hdr.command_id == 0x01:
            if isinstance(args, (bytes, bytearray)):
                _LOGGER.debug(
                    "C4 config ep %s: cmd 0x01 with unparsed body — "
                    "cannot resolve read_attributes future",
                    self.endpoint.endpoint_id,
                )
                return
            return super().handle_message(hdr, args)

        # cmd 0x0A: Report Attributes — parse and cache.
        if hdr.command_id == 0x0A:
            if isinstance(args, (bytes, bytearray)):
                # Schema parse failed earlier — fall back to manual parse.
                try:
                    remaining = args
                    while remaining:
                        attr, remaining = foundation.Attribute.deserialize(remaining)
                        _LOGGER.debug(
                            "C4 config report: ep=%s attr=0x%04x value=%r",
                            self.endpoint.endpoint_id, attr.attrid, attr.value.value,
                        )
                        self._update_attribute(attr.attrid, attr.value.value)
                except Exception as e:
                    _LOGGER.warning(
                        "C4 config: Report Attributes parse failed on ep %s: %s",
                        self.endpoint.endpoint_id, e,
                    )
                return
            return super().handle_message(hdr, args)

        # All other C4-proprietary commands — log and discard
        _LOGGER.debug(
            "C4 config ep %s: ignoring unhandled cmd=0x%02x args=%s",
            self.endpoint.endpoint_id, hdr.command_id,
            raw_body.hex() if isinstance(raw_body, (bytes, bytearray)) else repr(args),
        )


# ---------------------------------------------------------------------------
# Bootstrap primitives, shared by every C4 handheld
# ---------------------------------------------------------------------------
# Both remotes need the same provisioning primitives; they sit here with
# `_c4_send_room_info` and `_c4_send_display_message`, which they are always
# used alongside. The SEQUENCE stays per model - only the primitives are shared.

# Gap between individual bootstrap commands. The real controller interleaves
# these over a couple of seconds; C4_PROVISION_DELAY (0.05s) is aimed at mains
# powered devices and is too aggressive for a sleepy remote.
BOOTSTRAP_CMD_GAP = 0.25

DEFAULT_LOCALE = "en_US"

# c4.zr.loc carries two leading bytes whose meaning is unconfirmed. p2=0x0f
# does not match the locale string length (5), so it is probably a region or
# feature flag. Replicating the observed values verbatim.
LOCALE_P1 = 0x00
LOCALE_P2 = 0x0F


async def _c4_send_raw(device, cmd: str) -> None:
    """Frame and send one C4 ASCII command on the button profile.

    EP1 -> EP1, profile 0xC25C, cluster 0x0001, no reply - the same send path
    as `_c4_send_display_message` and friends above.

    Route discovery is left to zigpy, which forces it from the second attempt
    onwards. Do NOT pass force_route_discovery here: `Device.request` has no
    such parameter, so it falls into **kwargs and collides with the value
    zigpy's own send lambda supplies, raising TypeError inside the retry loop
    and burning every attempt.
    """
    data = _build_c4_frame(int(cmd[2:6], 16), cmd)
    await device.request(
        profile=C4_PROFILE_BUTTON,
        cluster=C4_CLUSTER_ID,
        src_ep=1,
        dst_ep=1,
        sequence=device.get_sequence(),
        data=data,
        expect_reply=False,
    )


async def _c4_send_time(device, when=None, trailer: str | None = "01") -> None:
    """Push wall-clock time: ``0s<seq> c4.zr.tm <hh> <mm> <ss>[ <trailer>]``.

    The time is raw byte values rendered as hex, so 19:15:18 is ``13 0f 12``.
    A Control4 director sends the SR-260 a fourth byte, ``01``, on a Monday
    and on a Friday alike, so it is not the day of week (this used to send
    isoweekday). It sends the SR-250 only the three time bytes. Pass
    ``trailer=None`` for the SR-250. Both models accepted the old form too.

    Per the SR260 init capture the controller sends `tm` before any UI command,
    and re-sends it unprompted after a cold-boot rejoin. If the remote never
    receives it, its clock drifts.
    """
    when = when or datetime.datetime.now()
    seq = next_c4_seq(device)
    cmd = (
        f"0s{seq:04x} c4.zr.tm "
        f"{when.hour:02x} {when.minute:02x} {when.second:02x}"
    )
    if trailer:
        cmd += f" {trailer}"
    _LOGGER.debug("C4 bootstrap: tm -> %r", cmd)
    await _c4_send_raw(device, cmd)


async def _c4_send_locale(device, locale: str = DEFAULT_LOCALE) -> None:
    """Push UI locale: ``0s<seq> c4.zr.loc <p1> <p2> "<locale>"``."""
    safe = "".join(c for c in locale if c.isalnum() or c == "_")
    seq = next_c4_seq(device)
    cmd = f'0s{seq:04x} c4.zr.loc {LOCALE_P1:02x} {LOCALE_P2:02x} "{safe}"'
    _LOGGER.debug("C4 bootstrap: loc -> %r", cmd)
    await _c4_send_raw(device, cmd)


async def _c4_answer_motion_wake(device, default_room: str, trailer: str | None = "01") -> None:
    """Answer `c4.zr.mot` the way a director does: `tm`, then both LCD rows.

    Measured: a source changed from the app is NOT pushed to a sleeping
    remote. The director waits for the pickup's `mot` and answers within
    ~100 ms with `tm` then `ri "<room>" "<source>"`, so row 2 is current the
    moment the screen lights.
    Without this, a source set while the remote slept never reached it.
    """
    try:
        await _c4_send_time(device, trailer=trailer)
        await _c4_send_room_info(
            device, c4_current_room(device, default_room), c4_current_source(device),
        )
    except Exception as exc:
        _LOGGER.debug("C4: motion-wake answer failed for %s: %s", device.ieee, exc)
    try:
        await c4_flush_pending_beep(device)
    except Exception as exc:
        _LOGGER.debug("C4: motion-wake beep flush failed for %s: %s", device.ieee, exc)
