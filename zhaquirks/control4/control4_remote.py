"""ZHA quirk for the Control4 C4-SR260 IR/Zigbee Remote (50 buttons).

EP layout:
  1            — ZHA Remote Control + PowerConfiguration (battery) +
                 C4SR260DisplayCluster (writable LCD message)
  2            — virtual, C4ConfigCluster
  196          — virtual, C4ConfigCluster
  197          — C4 button, C4RemoteButtonCluster (routing hub only)
  100–149      — virtual per-button Event entities (one per physical button)

The remote emits press / release events on the c4.zr.* namespace:
  sa c4.zr.bb <btn> 0000 0000 00000000   (button begin / press)
  sa c4.zr.be <btn> 0000 0000 00000000   (button end   / release)

Each is mapped to a SHORT_PRESS / SHORT_RELEASE on the matching virtual
endpoint, so HA sees one Event entity per physical key with two actions.

See documentation/control4-sr260-remote-protocol.md for the protocol
analysis (button-ID layout, namespaces, init sequence, etc.).

Uses SKIP_CONFIGURATION because the device does not honour ZHA's
bind/configure-reporting flow — the C4 protocol handshake is handled
reactively by _c4_sniff_model() when the remote broadcasts its model.
"""

import asyncio
import logging
import time as _time
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomDevice
from zigpy.zcl.clusters.general import Identify, PowerConfiguration

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    LONG_PRESS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SHORT_PRESS,
    SHORT_RELEASE,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    SR260_BUTTON_EP_MAP,
    SR260_BUTTON_MAP,
    BOOTSTRAP_CMD_GAP,
    DEFAULT_LOCALE,
    C4DimmerManufCluster,
    C4ConfigCluster,
    _c4_send_time,
    _c4_send_locale,
    _c4_send_room_info,
    c4_current_source,
    c4_flush_pending_beep,
    sr260_room_for,
    _c4_send_display_message,
    _c4_send_clear_display,
)
from c4_basic_cluster import C4BasicCluster
from c4_checkin import (
    CHECKIN_ACK_EP,
    CHECKIN_DEDUP_S,
    CONFIG_REPLY_DST_EP,
    CONFIG_REPLY_SRC_EP,
    FIRMWARE_CLUSTER,
    FIRMWARE_PROFILE,
    FIRMWARE_REPLY_EP,
    HEARTBEAT_COMMAND,
    checkin_ack_frame,
    checkin_tsn,
    config_read_reply,
    firmware_reply_frame,
    heartbeat_due,
    is_config_read,
    is_firmware_query,
    read_attr_ids,
)
from c4_button_cluster import (
    C4RemoteButtonCluster,
    _SR260_BUTTON_CLUSTERS,
)
from c4_display_cluster import C4SR260DisplayCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# Zigbee Home Automation device type 0x0006 = Remote Control
_REMOTE_CONTROL_DEVICE_TYPE = 0x0006


async def _c4_sr260_send_checkin_ack(device, report_tsn: int) -> None:
    data = checkin_ack_frame(device.get_sequence())
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK, cluster=C4.C4_CLUSTER_ID,
            src_ep=CHECKIN_ACK_EP, dst_ep=CHECKIN_ACK_EP,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
    except Exception as exc:
        _LOGGER.warning(
            "C4 SR260: check-in ack for report tsn=0x%02x failed - %r",
            report_tsn, exc,
        )
        return
    _LOGGER.info(
        "C4 SR260: check-in ack sent to %s, report tsn=0x%02x reply=%s",
        device.ieee, report_tsn, data.hex(),
    )
    # The remote is awake right now: the window a held Find Remote waits for.
    await c4_flush_pending_beep(device)


def _c4_sr260_emit_heartbeat(device) -> None:
    """Fire the check-in heartbeat through the first button cluster's handler."""
    for ep_id, ep in device.endpoints.items():
        if not ep_id:
            continue  # ZDO
        cluster = getattr(ep, "in_clusters", {}).get(C4_BUTTON_CLUSTER_ID)
        if cluster is not None:
            cluster.listener_event("zha_send_event", HEARTBEAT_COMMAND, {})
            return


async def _c4_sr260_send(device, what, data, profile, src_ep, dst_ep, cluster=None):
    try:
        await device.request(
            profile=profile, cluster=C4.C4_CLUSTER_ID if cluster is None else cluster,
            src_ep=src_ep, dst_ep=dst_ep,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
    except Exception as exc:
        _LOGGER.warning("C4 SR260: %s failed - %r", what, exc)
        return
    _LOGGER.info("C4 SR260: %s sent %s", what, data.hex())


def _c4_network_channel(device):
    try:
        return int(device.application.state.network_info.channel)
    except (AttributeError, TypeError, ValueError):
        return None


class C4SR260ConfigCluster(C4ConfigCluster):
    """EP-2 config cluster that answers the remote's check-in like a director.

    See c4_checkin for the measured frames. Unanswered, the SR-260 rejoins
    every few minutes. It also answers the 0x000C/0x0001 read, which the
    shared cluster would answer with the identity reply.
    """

    def handle_message(self, hdr, args):
        """Answer the EP-2 check-in and wake queries, then pass the rest on."""
        # c4_hooks delivers raw bytes with hdr=None.
        if hdr is None and isinstance(args, (bytes, bytearray)):
            try:
                self._c4_ack_checkin(bytes(args))
            except Exception as exc:
                _LOGGER.warning("C4 SR260: check-in ack error: %r", exc)
            try:
                if self._c4_answer_config_read(bytes(args)):
                    return None
            except Exception as exc:
                _LOGGER.warning("C4 SR260: config read error: %r", exc)
        return super().handle_message(hdr, args)

    def _c4_ack_checkin(self, raw: bytes) -> None:
        tsn = checkin_tsn(raw)
        if tsn is None:
            return
        device = self.endpoint.device
        now = _time.monotonic()
        last = getattr(device, "_c4_sr260_last_checkin", None)
        if last is not None and last[0] == tsn and now - last[1] < CHECKIN_DEDUP_S:
            return
        device._c4_sr260_last_checkin = (tsn, now)
        asyncio.ensure_future(_c4_sr260_send_checkin_ack(device, tsn))
        if heartbeat_due(getattr(device, "_c4_heartbeat_at", None), now):
            device._c4_heartbeat_at = now
            _c4_sr260_emit_heartbeat(device)

    def _c4_answer_config_read(self, raw: bytes) -> bool:
        # Global (fc low bits 00), no manufacturer code, command 0x00.
        if len(raw) < 5 or raw[0] & 0x07 or raw[2] != 0x00:
            return False
        ids = read_attr_ids(raw[3:])
        if not is_config_read(ids):
            return False
        device = self.endpoint.device
        channel = _c4_network_channel(device)
        if channel is None:
            return False  # leave it to the shared handler rather than guess
        data = config_read_reply(raw[1], ids, channel)
        asyncio.ensure_future(_c4_sr260_send(
            device, f"config read reply (attrs {[hex(a) for a in ids]})", data,
            C4_PROFILE_NETWORK, CONFIG_REPLY_SRC_EP, CONFIG_REPLY_DST_EP,
        ))
        return True


class Control4SR260Remote(CustomDevice):
    """Control4 C4-SR260 IR/Zigbee remote (50 buttons + LCD)."""

    @classmethod
    def match(cls, device):
        model = getattr(device, "model", None)
        manuf = getattr(device, "manufacturer", None)
        _LOGGER.debug(
            "C4 SR260.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, "ieee", "?"),
        )
        if model == "C4-SR260":
            _LOGGER.debug("C4 SR260.match: accepting on model match")
            return True
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-SR260"),
            (None, "C4-SR260"),
            ("Control4", None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     _REMOTE_CONTROL_DEVICE_TYPE,
                INPUT_CLUSTERS:  [
                    Identify.cluster_id,
                    PowerConfiguration.cluster_id,
                    C4_MANUF_CLUSTER,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            196: {
                PROFILE_ID:      C4_PROFILE_NETWORK,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID:      C4_PROFILE_BUTTON,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: _REMOTE_CONTROL_DEVICE_TYPE,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    PowerConfiguration.cluster_id,
                    C4DimmerManufCluster,
                    C4SR260DisplayCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            2: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                # Check-ins arrive here (src EP 2); see C4SR260ConfigCluster.
                INPUT_CLUSTERS:  [C4SR260ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4RemoteButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            # Virtual per-button endpoints — one Event entity per physical key
            **{
                SR260_BUTTON_EP_MAP[btn_id]: {
                    PROFILE_ID:      zha.PROFILE_ID,
                    DEVICE_TYPE:     0x0000,
                    INPUT_CLUSTERS:  [_SR260_BUTTON_CLUSTERS[btn_id]],
                    OUTPUT_CLUSTERS: [],
                }
                for btn_id in SR260_BUTTON_MAP
            },
        },
    }

    # One trigger entry per (action, button_name).
    # SR260 has no click-count protocol, but it does have a separate hold
    # message: `c4.zr.bh <btn>` is re-sent every ~501 ms while the button is
    # held, between the initial `bb` and the final `be`.  (That rate is
    # measured off the remote's own hold counter; see c4_button_cluster.py.)
    # The cluster maps
    # bb→SHORT_PRESS, bh→LONG_PRESS (one per message — automations bound to
    # LONG_PRESS auto-repeat their action), and be→SHORT_RELEASE.
    device_automation_triggers = {
        # Per-button press / hold / release triggers — one entry per button.
        **{
            (_action, _btn_name): {
                COMMAND:     _action,
                CLUSTER_ID:  C4_BUTTON_CLUSTER_ID,
                ENDPOINT_ID: SR260_BUTTON_EP_MAP[_btn_id],
            }
            for _btn_id, _btn_name in SR260_BUTTON_MAP.items()
            for _action in (SHORT_PRESS, LONG_PRESS, SHORT_RELEASE)
        },
        # Wake-from-sleep trigger — c4.zr.mot, fired by the remote
        # whenever motion / pickup wakes it up (and once on each
        # cold-boot / rejoin).  Routed through the C4RemoteButtonCluster
        # on EP 197, same path as the press / release events.
        ("motion_wake", "remote"): {
            COMMAND:     "motion_wake",
            CLUSTER_ID:  C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: 197,
        },
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-SR260"] = Control4SR260Remote
_LOGGER.info("C4 SR260: registered C4-SR260 in _C4_MODEL_QUIRK_MAP")


# ---------------------------------------------------------------------------
# Bootstrap — what takes the LCD off a blank screen
# ---------------------------------------------------------------------------
# Without a bootstrap the consequence is not cosmetic: the remote joins the
# mesh cleanly, reports "Join success", and then sits silent with only the
# signal and battery icons on the LCD. It never broadcasts its model, so
# `_c4_sniff_model` never fires, so the quirk never binds, so HA never
# provisions it. A closed loop.
#
# The SR-250 escapes that loop by announcing its model unprompted; this model
# does not. Break it once by seeding the IEEE->model store (see README,
# "Pairing an SR-260"); this bootstrap is what keeps it broken.
#
# The sequence (tm, dm/le splash, loc, ri) is the one a director sends after
# a join, from an SR-260 init capture.
#
# Confirmed live: a `c4.ln.dm` write reached the remote and
# rendered, then cleared on its own - `dm` is a transient splash. The line that
# PERSISTS is `ri`, which is why the sequence ends there.

# Row 1 of the LCD: the room name alone, as a Control4 director sends it.
#
# This is only the fallback for a remote missing from the room map in
# c4_helpers (C4_REMOTE_ROOMS), which names each remote's room.
# Keep in lockstep with SR260_DEFAULT_ROOM in c4_button_cluster.py, which is
# the fallback for a selection landing before the first bootstrap.
DEFAULT_ROOM = "Living Room"
DEFAULT_SOURCE = ""
DEFAULT_SPLASH = "Living Room"

# A bootstrap sent while the remote is asleep is simply lost, so it is driven
# by inbound traffic rather than by a timer. The cooldown stops a burst of
# frames firing a burst of bootstraps.
_WAKE_KICK_COOLDOWN = 6.0


async def c4_sr260_bootstrap(
    device,
    room: str = DEFAULT_ROOM,
    source: str = DEFAULT_SOURCE,
    locale: str = DEFAULT_LOCALE,
    splash: str = DEFAULT_SPLASH,
) -> None:
    """Replay the controller-side init sequence so the remote goes live.

    Order matters: the clock arrives before any UI command, and the splash is
    explicitly closed with `le` before locale and room info land.

    The splash shows once per device object. Re-provisioning happens on every
    Device_annce, so repeating it would flash the screen during ordinary use.
    Pass `splash=None` to suppress it outright.
    """
    show_splash = bool(splash) and not getattr(
        device, "_c4_sr260_splash_done", False
    )
    _LOGGER.info(
        "C4 SR260: sending bootstrap to %s (room=%r locale=%r splash=%s)",
        device.ieee, room, locale, show_splash,
    )

    await _c4_send_time(device)
    await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    if show_splash:
        await _c4_send_display_message(device, splash)
        await asyncio.sleep(BOOTSTRAP_CMD_GAP)
        await _c4_send_clear_display(device)
        await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    await _c4_send_locale(device, locale)
    await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    # Last, and the only one that leaves something on screen. It also seeds
    # `device._c4_room`, which is what c4_button_cluster reads back when it
    # echoes a menu selection.
    #
    # Re-send the CACHED source rather than the `source` argument's default.
    # This runs again on every ZDO Device_annce, which the remote emits every
    # few minutes, so a hardcoded "" here wiped row 2 a few minutes into
    # watching anything. A real director re-sends `ri "<room>" "<source>"`
    # on every wake with the source intact; this is the same behaviour. An
    # explicit non-default `source` still wins, and "" from a room-off is
    # cached like any other value, so an off room stays off on screen.
    await _c4_send_room_info(
        device, room, source or c4_current_source(device),
    )

    device._c4_sr260_bootstrapped = True
    device._c4_sr260_splash_done = True
    _LOGGER.info("C4 SR260: bootstrap sequence sent to %s", device.ieee)


def _c4_sr260_packet_received(self, *args, **kwargs):
    """Wake hook on the STANDARD path: ZDO and plain ZCL frames.

    This is the one that matters for a remote which is not yet talking C4 -
    the only thing a freshly joined SR-260 emits is a PowerConfiguration
    report on EP 1, and that arrives here. Re-arm on Device_annce (ZDO cluster
    0x0013) because the latch lives on the HA-side object and would otherwise
    survive the remote's own power cycle, leaving HA convinced a device with a
    blank screen was provisioned.
    """
    try:
        pkt = args[0] if args else kwargs.get("packet")
        if (
            getattr(pkt, "cluster_id", None) == 0x0013
            and getattr(pkt, "profile_id", None) in (0, None)
        ):
            if getattr(self, "_c4_sr260_bootstrapped", False):
                _LOGGER.info(
                    "C4 SR260: device announce - re-arming bootstrap latch",
                )
            self._c4_sr260_bootstrapped = False
            self._c4_sr260_last_kick = 0.0
    except Exception as exc:
        _LOGGER.debug("C4 SR260: announce re-arm error: %s", exc)

    _c4_sr260_maybe_bootstrap(self, "standard packet")
    return super(Control4SR260Remote, self).packet_received(*args, **kwargs)


def _c4_sr260_custom_profile(self, packet):
    """Wake hook on the C4 path.

    c4_hooks Patch 4 intercepts C4-profile packets in
    ControllerApplication.packet_received, calls this, and returns without
    ever invoking Device.packet_received - so the hook above never sees them.
    Both paths are needed: the standard one gets the remote provisioned, this
    one keeps it provisioned once it is talking C4.
    """
    _c4_sr260_maybe_bootstrap(
        self, f"C4 packet (profile=0x{getattr(packet, 'profile_id', 0):04X})"
    )
    try:
        _c4_sr260_answer_firmware_query(self, packet)
    except Exception as exc:
        _LOGGER.debug("C4 SR260: firmware query error: %s", exc)
    return super(Control4SR260Remote, self).custom_profile_packet_received(packet)


def _c4_sr260_answer_firmware_query(device, packet) -> None:
    """Answer the 0xC25E firmware query the way a director does (c4_checkin)."""
    if (
        getattr(packet, "profile_id", None) != FIRMWARE_PROFILE
        or getattr(packet, "cluster_id", None) != FIRMWARE_CLUSTER
    ):
        return
    raw = packet.data
    raw = raw.serialize() if hasattr(raw, "serialize") else bytes(raw)
    if not is_firmware_query(raw):
        return
    now = _time.monotonic()
    last = getattr(device, "_c4_sr260_last_fw_query", None)
    if last is not None and last[0] == raw[1] and now - last[1] < CHECKIN_DEDUP_S:
        return
    device._c4_sr260_last_fw_query = (raw[1], now)
    asyncio.ensure_future(_c4_sr260_send(
        device, "firmware query reply", firmware_reply_frame(device.get_sequence()),
        FIRMWARE_PROFILE, FIRMWARE_REPLY_EP, FIRMWARE_REPLY_EP,
        cluster=FIRMWARE_CLUSTER,
    ))


def _c4_sr260_maybe_bootstrap(device, why: str) -> None:
    """Fire the bootstrap if it is armed and off cooldown."""
    try:
        if getattr(device, "_c4_sr260_bootstrapped", False):
            return
        now = _time.monotonic()
        if now - getattr(device, "_c4_sr260_last_kick", 0.0) <= _WAKE_KICK_COOLDOWN:
            return
        device._c4_sr260_last_kick = now
        _LOGGER.info("C4 SR260: %s - firing wake-triggered bootstrap", why)
        room = sr260_room_for(device, DEFAULT_ROOM)
        asyncio.ensure_future(c4_sr260_bootstrap(device, room=room, splash=room))
    except Exception as exc:
        _LOGGER.debug("C4 SR260: wake hook error: %s", exc)


Control4SR260Remote.packet_received = _c4_sr260_packet_received
Control4SR260Remote.custom_profile_packet_received = _c4_sr260_custom_profile
_LOGGER.info("C4 SR260: wake-triggered bootstrap hooks installed")
