"""ZHA quirk for the Control4 C4-SR250B System Remote Control (SR-250).

A freshly joined SR-250 will not emit button events: it sits on "Waiting for
network" with its keypad dead on the air until a controller pushes the Control4
application-layer bootstrap. This quirk sends that bootstrap (the same sequence
a director sends, see ``documentation/control4-sr260-remote-protocol.md``)::

    Set  c4.zr.tm  <hh> <mm> <ss>            clock (the SR-260 gets a trailing 01)
    Init c4.ln.dm  5a "Loading Room..."      splash
    Init c4.ln.le                            close splash
    Set  c4.zr.loc 00 0f "en_US"             locale
    Set  c4.ln.ri  "<room>" "<source>"       room + active source

with retries, because the remote is a sleepy end device and misses frames sent
while it is asleep. It then surfaces every key as a ``zha_event``
(``<key>_press`` / ``_hold`` / ``_release``), answers the remote's pushed-list
requests in the SR-250's own dialect, and acknowledges its EP-2 check-ins.

Device identity (observed on a live join)
-----------------------------------------
announce string    c4:control4_sr250b:C4-SR250B
model              C4-SR250B   (note the trailing B)
manufacturer       Control4
IEEE prefix        00:0f:ff: - matches C4_IEEE_PREFIX

Composer Pro displays the announce string as ``c4:control4_sr250:C4-SR250``
without the ``B``. The over-the-air value is the one that matters and is what
is matched here.
"""

import asyncio
import contextlib
import logging
import os
import sys
import time as _time

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from c4_basic_cluster import C4BasicCluster  # noqa: E402
from c4_checkin import (  # noqa: E402
    CHECKIN_ACK_EP,
    CHECKIN_DEDUP_S,
    HEARTBEAT_COMMAND,
    checkin_ack_frame,
    checkin_tsn,
    heartbeat_due,
)
from c4_display_cluster import C4SR260DisplayCluster  # noqa: E402
import c4_helpers as C4  # noqa: E402
from c4_helpers import (  # noqa: E402
    BOOTSTRAP_CMD_GAP,
    C4_ATTR_MODEL,
    C4_ATTR_POWER_CYCLE_COUNT,
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_DISPLAY_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    DEFAULT_LOCALE,
    C4ConfigCluster,
    C4DimmerManufCluster,
    _c4_answer_motion_wake,
    _c4_send_clear_display,
    _c4_send_display_message,
    _c4_send_list_items_response,
    _c4_send_locale,
    _c4_send_room_info,
    _c4_send_time,
    _send_many_to_one_route_request,
    c4_current_room,
    c4_current_source,
    c4_flush_pending_beep,
    c4_list_dialect,
    sr250_room_for,
)
import c4_hooks  # noqa: E402, F401
from c4_hooks import _C4_MODEL_QUIRK_MAP  # noqa: E402

# zigpy.device must be imported explicitly: zigpy.zdo.broadcast() refers to
# zigpy.device.broadcast without importing it, and raises AttributeError if
# nothing else pulled the submodule in first.
import zigpy.device  # noqa: F401
from zigpy.profiles import zha
from zigpy.quirks import CustomCluster, CustomDevice
import zigpy.types as zigpy_t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Identify, PowerConfiguration
import zigpy.zdo
import zigpy.zdo.types as zdo_t

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SKIP_CONFIGURATION,
)

_LOGGER = logging.getLogger(__name__)

MODEL = "C4-SR250B"

_REMOTE_CONTROL_DEVICE_TYPE = 0x0006

# The remote is a sleepy end device: a bootstrap sent while it is asleep is
# simply lost. Retry on a schedule (seconds after device init) until we observe
# inbound C4 traffic; the wake hooks below are the primary path.
BOOTSTRAP_SCHEDULE = tuple(
    range(8, 245, 4)
)  # every 4s for ~4min - remote must be awake

# Row 1 of the LCD: the room name alone. Only the fallback for a remote missing
# from the room map in c4_helpers (C4_REMOTE_ROOMS).
DEFAULT_ROOM = "Living Room"
DEFAULT_SOURCE = ""
DEFAULT_SPLASH = "Living Room"

# Bootstrap attempts that also broadcast a ZDO NWK_addr_req for the remote. A
# broadcast needs no route, and the remote must answer it with its current
# short address, which refreshes the coordinator's route to a remote whose
# cached nwk has gone stale. Each is one small frame.
PROBE_ZDO_ATTEMPTS = tuple(range(1, 16))  # t+8s..t+64s


async def _c4_send_nwk_addr_req(device) -> None:
    """Broadcast a ZDO NWK_addr_req for this device's IEEE.

    A broadcast needs no route and no nwk, so it sidesteps a stale cached
    short address that unicasts die on. Unlike the C4 bootstrap frames this
    is a spec-mandated request: a device whose IEEE matches must answer with
    its current short address.

    ALL_DEVICES (0xFFFF), not the RX_ON_WHEN_IDLE default, because the default
    excludes sleepy end devices - which is exactly what this remote is.
    """
    app = device.application

    # Build the argument list from the INSTALLED schema. Some zigpy versions want
    # (IEEEAddr, RequestType, StartIndex); other versions add NWKAddr.
    names, _types = zdo_t.CLUSTERS[zdo_t.ZDOCmd.NWK_addr_req]
    available = {
        "IEEEAddr": device.ieee,
        "RequestType": zdo_t.AddrRequestType.Single,
        "StartIndex": 0,
        "NWKAddr": 0xFFFF,
    }
    args = [available[n] for n in names]

    await zigpy.zdo.broadcast(
        app,
        zdo_t.ZDOCmd.NWK_addr_req,
        0,  # grpid
        0,  # radius
        *args,
        broadcast_address=zigpy_t.BroadcastAddress.ALL_DEVICES,
    )
    # Submitted, NOT delivered: broadcast() returns SUCCESS unconditionally
    # once the packet is handed to send_packet. Inbound is the only proof.
    _LOGGER.info(
        "C4 SR250: NWK_addr_req submitted for %s to ALL_DEVICES "
        "(0xFFFF) - awaiting NWK_addr_rsp",
        device.ieee,
    )


async def c4_sr250_bootstrap(
    device,
    room: str | None = None,
    source: str = DEFAULT_SOURCE,
    locale: str = DEFAULT_LOCALE,
    splash: str | None = DEFAULT_SPLASH,
) -> None:
    """Replay the controller-side init sequence so the remote goes live.

    Order matters: the capture shows the clock arriving before any UI command,
    and the splash being explicitly closed with ``le`` before locale and room
    info land.

    The splash is sent once per device object. ``tm``/``loc``/``ri`` are what
    take the LCD off "Waiting for network"; the ``dm``/``le`` pair is purely a
    visible confirmation that a provisioning cycle ran, and re-provisioning
    happens often enough - on every Device_annce - that repeating it would
    flash the screen
    during ordinary use. Showing it on the first bootstrap keeps the
    confirmation without the churn. Pass ``splash=None`` to suppress it
    outright.
    """
    if room is None:
        room = sr250_room_for(device, DEFAULT_ROOM)
        if splash == DEFAULT_SPLASH:
            splash = room
    show_splash = bool(splash) and not getattr(device, "_c4_sr250_splash_done", False)
    _LOGGER.info(
        "C4 SR250: sending bootstrap to %s (room=%r locale=%r splash=%s)",
        device.ieee,
        room,
        locale,
        show_splash,
    )

    await _c4_send_time(device, trailer=None)  # a director sends the SR-250 no 4th byte
    await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    if show_splash:
        await _c4_send_display_message(device, splash)
        await asyncio.sleep(BOOTSTRAP_CMD_GAP)

        await _c4_send_clear_display(device)
        await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    await _c4_send_locale(device, locale)
    await asyncio.sleep(BOOTSTRAP_CMD_GAP)

    # Re-send the CACHED source, not the `source` default - this bootstrap
    # re-runs on every Device_annce, and a hardcoded "" wipes row 2 while the
    # room is still playing. Same fix as the SR-260; see c4_helpers.
    await _c4_send_room_info(
        device,
        room,
        source or c4_current_source(device),
    )

    # Latch only after the full sequence has been sent without raising; the
    # wake hooks re-fire every _WAKE_KICK_COOLDOWN until this is set.
    device._c4_sr250_bootstrapped = True
    device._c4_sr250_splash_done = True

    # A completed bootstrap is what _c4_bootstrap_loop is retrying towards, so
    # stop it here too. Its own stop flag is only ever set by EP 197 button
    # frames, so a remote talking solely on EP 2 would otherwise let the loop
    # run all 60 attempts after provisioning had already succeeded.
    device._c4_sr250_seen_traffic = True

    _LOGGER.info("C4 SR250: bootstrap sequence sent to %s", device.ieee)


class C4SR250RawCluster(CustomCluster):
    """EP-197 cluster: decode the remote's ASCII frames into ZHA events.

    ``c4_hooks`` Patch 2 routes inbound C4-profile packets to whichever cluster
    on the target endpoint carries ``_c4_custom_handler``, calling
    ``handle_message(None, <bytes>)``. Key presses (`bb`/`bh`/`be`, and `cbb`
    inside a list) are mapped through ``_KEYS`` and fired as ``zha_event``s;
    every frame is also logged verbatim.
    """

    cluster_id = C4_BUTTON_CLUSTER_ID
    name = "Control4 SR250 Buttons"
    ep_attribute = "c4_sr250_raw"
    _c4_custom_handler = True

    _VERBS = {
        "0t": "announce",
        "0r": "report",
        "0s": "set",
        "0g": "get",
        "0i": "init",
    }

    # namespace -> (event label, index of the button ID within ns_args).
    # A hold is bb -> bh xN -> be, and c4.ln.cbb carries the button ID in its
    # SECOND argument (the first is a constant 0x0e of unknown meaning), which
    # is why the index is part of this table rather than assumed to be 0.
    _BUTTON_EVENTS = {
        "c4.zr.bb": ("down", 0),
        "c4.zr.bh": ("hold", 0),
        "c4.zr.be": ("up", 0),
        "c4.ln.cbb": ("menu", 1),
    }

    # Button ID -> key name, from a sweep of every key. The 47 keys occupy
    # 0x00-0x2e with no gaps and no duplicates.
    _KEYS = {
        0x00: "List",
        0x01: "Room Off",
        0x02: "Watch",
        0x03: "Listen",
        0x04: "PgUp",
        0x05: "Red",
        0x06: "Green",
        0x07: "Yellow",
        0x08: "Blue",
        0x09: "Control4",
        0x0A: "PgDown",
        0x0B: "Mute",
        0x0C: "Vol-",
        0x0D: "Vol+",
        0x0E: "Ch+",
        0x0F: "Guide",
        0x10: "Up",
        0x11: "Down",
        0x12: "Left",
        0x13: "Right",
        0x14: "Prev",
        0x15: "Info",
        0x16: "Menu",
        0x17: "Cncl",
        0x18: "DVR",
        0x19: "SkipForward",
        0x1A: "Rewind",
        0x1B: "Pause",
        0x1C: "FastForward",
        0x1D: "Stop",
        0x1E: "Play",
        0x1F: "1",
        0x20: "2",
        0x21: "3",
        0x22: "4",
        0x23: "6",
        0x24: "7",
        0x25: "8",
        0x26: "9",
        0x27: "*",
        0x28: "#",
        0x29: "Ch-",
        0x2A: "Select",
        0x2B: "SkipBack",
        0x2C: "Record",
        0x2D: "5",
        0x2E: "0",
    }

    # Button ID -> event slug. The emitted zha_event ``command`` is
    # ``f"{slug}_{action}"``, e.g. ``volume_up_press``. Slugs are spelled out
    # rather than derived from _KEYS because mechanically slugifying "Vol+" /
    # "Vol-" / "*" / "#" / "Room Off" produces collisions and unstable names,
    # and a trigger that silently stops matching is the worst failure mode
    # this device could have.
    _KEY_SLUGS = {
        0x00: "list",
        0x01: "room_off",
        0x02: "watch",
        0x03: "listen",
        0x04: "page_up",
        0x05: "red",
        0x06: "green",
        0x07: "yellow",
        0x08: "blue",
        0x09: "control4",
        0x0A: "page_down",
        0x0B: "mute",
        0x0C: "volume_down",
        0x0D: "volume_up",
        0x0E: "channel_up",
        0x0F: "guide",
        0x10: "up",
        0x11: "down",
        0x12: "left",
        0x13: "right",
        0x14: "prev",
        0x15: "info",
        0x16: "menu",
        0x17: "cancel",
        0x18: "dvr",
        0x19: "skip_forward",
        0x1A: "rewind",
        0x1B: "pause",
        0x1C: "fast_forward",
        0x1D: "stop",
        0x1E: "play",
        0x1F: "digit_1",
        0x20: "digit_2",
        0x21: "digit_3",
        0x22: "digit_4",
        0x23: "digit_6",
        0x24: "digit_7",
        0x25: "digit_8",
        0x26: "digit_9",
        0x27: "star",
        0x28: "hash",
        0x29: "channel_down",
        0x2A: "select",
        0x2B: "skip_back",
        0x2C: "record",
        0x2D: "digit_5",
        0x2E: "digit_0",
    }

    # _BUTTON_EVENTS label -> action suffix in the emitted command. "menu" is
    # its own action rather than an alias of "press" because a menu press
    # arrives on c4.ln.cbb with NO matching release, so an automation written
    # against press/release pairs must not silently absorb it.
    _ACTIONS = {
        "down": "press",
        "hold": "hold",
        "up": "release",
        "menu": "menu",
    }

    # Used when a frame carries a button ID we have no name for. Should never
    # fire - the 47 known IDs are contiguous 0x00-0x2e - but an out-of-range
    # ID must surface rather than be swallowed.
    _UNKNOWN_SLUG = "unknown"

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Decode a cluster request the same way as a raw frame."""
        self._process_raw(hdr, args)

    def handle_message(self, hdr, args, *extra, **kwargs):
        """Decode a raw C4 frame delivered by c4_hooks."""
        self._process_raw(hdr, args)

    def _process_raw(self, hdr, args):
        raw = self._coerce_bytes(args)
        if raw is None:
            _LOGGER.warning(
                "C4 SR250 raw: cannot extract bytes: args=%s type=%s",
                args,
                type(args),
            )
            return

        text = raw.decode("ascii", errors="replace").strip()
        parts = text.split()
        verb = self._VERBS.get(parts[0][:2], "?") if parts else "?"

        # The namespace is NOT at a fixed index. Observed layouts:
        #   "0t12ee sa c4.zr.bb 11 0000 0000"  -> parts[1] is the opcode "sa",
        #                                         the namespace is parts[2]
        #   "0r0041 000"                       -> report, no namespace at all
        #   "0r0043"                           -> bare report, one token
        # Locate the c4.* token instead of trusting position.
        ns_idx = next(
            (i for i, p in enumerate(parts) if i and p.startswith("c4.")),
            None,
        )
        if ns_idx is None:
            namespace = "-"
            opcode = parts[1] if len(parts) > 1 else "-"
            ns_args = parts[2:]
        else:
            namespace = parts[ns_idx]
            opcode = " ".join(parts[1:ns_idx]) or "-"
            ns_args = parts[ns_idx + 1 :]

        _LOGGER.info(
            "C4 SR250 RX  verb=%-8s op=%-4s ns=%-12s args=%-24s  %s   [hex=%s]",
            verb,
            opcode,
            namespace,
            " ".join(ns_args),
            text,
            raw.hex(),
        )

        # Keypad traffic in menu context is diverted to c4.ln.cbb and produces
        # no bb/be pair at all, so it is handled alongside bb/bh/be.
        event = self._BUTTON_EVENTS.get(namespace)
        if event is not None:
            label, _id_idx = event
            if len(ns_args) > _id_idx:
                _raw_id = ns_args[_id_idx]
                try:
                    key = self._KEYS.get(int(_raw_id, 16), "?")
                except ValueError:
                    key = "?"
                _LOGGER.info(
                    "C4 SR250 BTN  %-5s id=0x%-3s %-12s [ns=%-9s args=%s]",
                    label,
                    _raw_id,
                    key,
                    namespace,
                    " ".join(ns_args),
                )
                self._emit_button(label, _raw_id, key, namespace, ns_args)
        elif namespace == "c4.zr.mot":
            _LOGGER.info("C4 SR250 MOT  motion wake")
            self._emit("motion_wake", {"namespace": namespace})
            # A director sends the SR-250 a three-byte tm (see _c4_send_time).
            room = sr250_room_for(self.endpoint.device, DEFAULT_ROOM)
            asyncio.ensure_future(
                _c4_answer_motion_wake(
                    self.endpoint.device,
                    room,
                    trailer=None,
                )
            )
        elif namespace == "c4.ln.gi":
            # The remote is paging a list we pushed. Never sent for the
            # remote's own local List menu.
            seq = parts[0][2:] if parts else ""
            list_id = self._parse_hex(ns_args, 0)
            offset = self._parse_hex(ns_args, 1)
            count = self._parse_hex(ns_args, 2)
            _LOGGER.info(
                "C4 SR250 LIST gi    list=0x%04X offset=%d count=%d seq=%s",
                list_id,
                offset,
                count,
                seq,
            )
            asyncio.ensure_future(self._answer_gi_request(list_id, offset, count, seq))
        elif namespace == "c4.ln.is":
            # Select pressed on a pushed list. Replaces the bb/be pair, which
            # the remote suppresses while a list is up.
            list_id = self._parse_hex(ns_args, 0)
            sel_idx = self._parse_hex(ns_args, 1)
            _LOGGER.info(
                "C4 SR250 LIST is    list=0x%04X sel=%d",
                list_id,
                sel_idx,
            )
            if self._quirk_menu_display() is not None:
                # HA's List menu (show_menu): navigated here, fires menu_action
                # and no menu_select, so menu_select dispatchers never see it.
                asyncio.ensure_future(self._quirk_menu_key("is", list_id, sel_idx))
            else:
                self._fire_menu_select(list_id, sel_idx)
        elif namespace == "c4.ln.ise":
            # Release of the matching `is`. Deliberately inert: firing a second
            # event here would double-dispatch the menu selection.
            _LOGGER.debug("C4 SR250 LIST ise   (ignored) args=%s", ns_args)
        elif namespace == "c4.ln.lb":
            # Left on a pushed list (Back). Only HA's List menu has anywhere to
            # go back to.
            list_id = self._parse_hex(ns_args, 0)
            sel_idx = self._parse_hex(ns_args, 1)
            _LOGGER.info(
                "C4 SR250 LIST lb    list=0x%04X sel=%d",
                list_id,
                sel_idx,
            )
            if self._quirk_menu_display() is not None:
                asyncio.ensure_future(self._quirk_menu_key("lb", list_id, sel_idx))
        elif namespace == "c4.ln.cn":
            list_id = self._parse_hex(ns_args, 0)
            sel_idx = self._parse_hex(ns_args, 1)
            _LOGGER.info(
                "C4 SR250 LIST cn    list=0x%04X sel=%d",
                list_id,
                sel_idx,
            )
            if self._quirk_menu_display() is not None:
                # Cancel steps up one level too, and closes from the top: it
                # was the SR-250's only Back before Left (lb) was found.
                asyncio.ensure_future(self._quirk_menu_key("lb", list_id, sel_idx))
            else:
                self._fire_menu_cancel(list_id, sel_idx)

        with contextlib.suppress(Exception):
            self.endpoint.device._c4_sr250_seen_traffic = True

    # ------------------------------------------------------------------
    # Controller-provisioned list (LCD menu) support.
    #
    # Ported from C4RemoteButtonCluster (c4_button_cluster.py), which drives
    # the same protocol on the SR-260. Two distinct list mechanisms exist and
    # only the second is usable from HA:
    #
    #   local List menu  - opened by the List key on the remote itself.
    #                      Arrows and Select transmit NOTHING; only Cncl/List
    #                      escape, as c4.ln.cbb. Useless for selection.
    #   pushed list      - established by show_list on the display cluster.
    #                      The remote pages items with c4.ln.gi and reports
    #                      the commit with c4.ln.is.
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_hex(data, idx: int) -> int:
        """Parse the hex token at ``idx``, or return 0.

        Never raises - a malformed list frame must not take down the decoder.
        """
        if len(data) <= idx:
            return 0
        try:
            return int(data[idx], 16)
        except (TypeError, ValueError):
            return 0

    def _get_display_cluster(self):
        """Return the EP-1 display cluster holding the active menu, or None.

        The menu state lives on the display cluster rather than here because
        show_list is invoked against that cluster; this cluster (EP 197) only
        ever reads it.
        """
        ep1 = self.endpoint.device.endpoints.get(1)
        if ep1 is None:
            return None
        return ep1.in_clusters.get(C4_DISPLAY_CLUSTER_ID)

    async def _answer_gi_request(
        self,
        list_id: int,
        offset: int,
        count: int,
        seq: str,
    ) -> None:
        """Reply to a c4.ln.gi page request with the cached item labels."""
        display = self._get_display_cluster()
        if display is None:
            _LOGGER.warning(
                "C4 SR250: gi for list 0x%04X but no display cluster on EP 1",
                list_id,
            )
            return

        menu = getattr(display, "_active_menu", None)
        if menu is None or menu.get("list_id") != list_id:
            _LOGGER.warning(
                "C4 SR250: gi for list 0x%04X but active menu is %r - ignoring",
                list_id,
                menu,
            )
            return

        items_all = list(menu.get("items") or [])
        slice_end = offset + count if count else len(items_all)
        items = items_all[offset:slice_end]

        dialect = c4_list_dialect(self.endpoint.device)
        try:
            sent = await _c4_send_list_items_response(
                self.endpoint.device,
                seq,
                items,
                icon=dialect["item_icon"],
                # NOT self.endpoint.endpoint_id: this cluster lives on EP 197
                # because that is where a gi arrives; the reply goes to EP 1.
                endpoint=dialect["reply_ep"],
                form=dialect["gi_form"],
            )
        except Exception:
            _LOGGER.warning(
                "C4 SR250: gi response send failed",
                exc_info=True,
            )
        else:
            # Counted off the bytes actually sent: the builder may truncate
            # to fit the frame after this function chose its slice.
            _in_frame = bytes(sent).count(b'"') // 2 if sent is not None else -1
            _LOGGER.info(
                "C4 SR250 LIST gi -> answered %d of %d at offset=%d (%d in frame): %s",
                len(items),
                len(items_all),
                offset,
                _in_frame,
                items,
            )
            if sent is not None:
                _LOGGER.debug(
                    "C4 SR250 LIST gi -> sent %d bytes: %r",
                    len(sent),
                    bytes(sent),
                )

    def _quirk_menu_display(self):
        """Return the display cluster, if HA's List menu (show_menu) is on screen."""
        display = self._get_display_cluster()
        menu = getattr(display, "_active_menu", None) if display else None
        if menu and menu.get("owner") == "settings":
            return display
        return None

    async def _quirk_menu_key(self, kind, list_id: int, index: int) -> None:
        """Navigate HA's List menu, as C4RemoteButtonCluster does on the SR-260."""
        display = self._quirk_menu_display()
        if display is None:
            return
        try:
            picked = await display.settings_event(kind, list_id, index)
        except Exception:
            _LOGGER.warning("C4 SR250: List menu key failed", exc_info=True)
            return
        if picked is None:
            return
        self._emit(
            "menu_action",
            {
                "action": picked.action,
                "label": picked.title,
                "path": picked.path,
                "selected_index": picked.selected,
                ENDPOINT_ID: self.endpoint.endpoint_id,
            },
        )
        _LOGGER.info(
            "C4 SR250: menu_action %r (%r at %r)",
            picked.action,
            picked.title,
            picked.path,
        )

    def _fire_menu_select(self, list_id: int, sel_idx: int) -> None:
        """Resolve the chosen label, emit ``menu_select``, clear the menu."""
        display = self._get_display_cluster()
        item = None
        title = ""
        if display is not None:
            menu = getattr(display, "_active_menu", None)
            if menu is not None and menu.get("list_id") == list_id:
                items = menu.get("items") or []
                item = items[sel_idx] if 0 <= sel_idx < len(items) else None
                title = menu.get("title") or ""
            display._active_menu = None

        self._emit(
            "menu_select",
            {
                "list_id": list_id,
                "selected_index": sel_idx,
                "item": item,
                "title": title,
                ENDPOINT_ID: self.endpoint.endpoint_id,
            },
        )
        _LOGGER.info(
            "C4 SR250: menu_select list=0x%04X idx=%d item=%r",
            list_id,
            sel_idx,
            item,
        )
        asyncio.ensure_future(self._show_selection_and_close(item or ""))

    def _fire_menu_cancel(self, list_id: int, sel_idx: int) -> None:
        """Emit ``menu_cancel`` and dismiss the list.

        Dispatcher automations should wait on menu_select OR menu_cancel, so a
        Cancel press exits the wait immediately instead of blocking until the
        selection timeout.
        """
        display = self._get_display_cluster()
        title = ""
        if display is not None:
            menu = getattr(display, "_active_menu", None)
            if menu is not None and menu.get("list_id") == list_id:
                title = menu.get("title") or ""
            display._active_menu = None

        self._emit(
            "menu_cancel",
            {
                "list_id": list_id,
                "selected_index": sel_idx,
                "title": title,
                ENDPOINT_ID: self.endpoint.endpoint_id,
            },
        )
        _LOGGER.info(
            "C4 SR250: menu_cancel list=0x%04X idx=%d",
            list_id,
            sel_idx,
        )
        asyncio.ensure_future(self._close_list())

    async def _show_selection_and_close(self, item: str) -> None:
        """Leave the chosen label on the LCD, then drop the list overlay.

        Failures are swallowed: this remote sleeps between presses, so an
        undelivered cosmetic update must not surface as an automation error.
        """
        device = self.endpoint.device
        try:
            if item:
                # `c4.ln.ri "<room>" "<source>"`, as a director sends it: row 2
                # then PERSISTS as the room's active source. A `c4.ln.dm`
                # splash would be wiped by the clear_display below.
                await _c4_send_room_info(
                    device,
                    c4_current_room(device, sr250_room_for(device, DEFAULT_ROOM)),
                    item,
                )
            await _c4_send_clear_display(device)
        except Exception as _exc:
            _LOGGER.debug(
                "C4 SR250: post-select LCD update failed - %s",
                _exc,
            )

    async def _close_list(self) -> None:
        """Dismiss the LCD list overlay (c4.ln.le)."""
        try:
            await _c4_send_clear_display(self.endpoint.device)
        except Exception as _exc:
            _LOGGER.debug("C4 SR250: close_list failed - %s", _exc)

    def _emit(self, command, params):
        """Fire one zha_event through ZHA's cluster-handler listener.

        ZHA attaches a ClusterHandler to every cluster named in the
        replacement, and that handler implements ``zha_send_event``, so this
        is the sanctioned quirk -> HA event bus path - the same one
        ``c4_button_cluster`` already uses for the SR260.

        Deliberately swallowing exceptions: this runs inside the c4_hooks
        Patch 2 intercept, so a raising listener would take the raw capture
        down with it. Losing the log is worse than losing one event.
        """
        try:
            self.listener_event("zha_send_event", command, params)
        except Exception as _exc:
            _LOGGER.warning("C4 SR250: could not emit %r: %s", command, _exc)

    def _emit_button(self, label, raw_id, key, namespace, ns_args):
        """Turn one decoded button frame into a zha_event.

        ``command`` is ``f"{slug}_{action}"`` (e.g. ``volume_up_press``) so an
        automation can match on a single flat string. This is not cosmetic:
        HA's event trigger builds a voluptuous schema from ``event_data`` and
        does NOT apply ALLOW_EXTRA to nested dicts, so matching on a key
        inside ``params`` would require listing every key params carries.
        Putting the discriminator in ``command`` sidesteps that entirely; the
        structured detail still rides along in params for whoever wants it.
        """
        action = self._ACTIONS.get(label)
        if action is None:
            return
        try:
            button_id = int(raw_id, 16)
        except ValueError:
            return
        slug = self._KEY_SLUGS.get(button_id, self._UNKNOWN_SLUG)
        params = {
            "button": key,
            "button_id": button_id,
            "slug": slug,
            "action": action,
            "namespace": namespace,
            "args": " ".join(ns_args),
        }
        if label == "down":
            self._pressed_at = (button_id, _time.monotonic())
        elif label == "up":
            self._pressed_at = None
        elif label == "hold":
            # Ticks come at only 2 Hz, so a volume ramp accelerates on how long
            # the key has been held. Unlike the SR-260's, this remote's bh has
            # no hold-timer field, so time it from the key's bb.
            params["hold_ms"] = self._hold_ms(button_id)
        self._emit(f"{slug}_{action}", params)

    def _hold_ms(self, button_id):
        """Milliseconds since this key went down, as of a bh tick."""
        now = _time.monotonic()
        pressed = getattr(self, "_pressed_at", None)
        if pressed is None or pressed[0] != button_id:
            # Its bb was lost: count from about one tick ago.
            pressed = (button_id, now - 0.5)
            self._pressed_at = pressed
        return round((now - pressed[1]) * 1000)

    @staticmethod
    def _coerce_bytes(args):
        """Normalise the several shapes zigpy/c4_hooks may hand us."""
        if isinstance(args, (bytes, bytearray)):
            return bytes(args)
        if args and isinstance(args, (list, tuple)):
            first = args[0]
            if isinstance(first, (bytes, bytearray)):
                return bytes(first)
            if isinstance(first, int):
                return bytes(args)
            if isinstance(first, (list, tuple)):
                return bytes(first)
        return None


# ---------------------------------------------------------------------------
# EP-2 power-cycle detection
# ---------------------------------------------------------------------------
#
# Set the re-arm flag to False to log what it would do without acting.
# _C4_SR250_LOG_EP2_FRAMES logs every EP-2 frame in hex, for re-deriving frame
# shapes; the baseline, the re-arm and a counter moving backwards always log.
_C4_SR250_LOG_EP2_FRAMES = False
_C4_SR250_REARM_ON_POWER_CYCLE = True


def _c4_parse_attribute_records(raw: bytes) -> dict:
    """Decode the attribute records in an EP-2 report frame.

    The SR-250 sends the same payload under two different shapes: profile-wide
    Report Attributes (cmd 0x0A) and cluster-specific commands 0x01-0x03 whose
    bodies are attribute-record shaped but which C4ConfigCluster discards as
    "unhandled cmd". The cluster-specific form is the majority (28 of 37
    frames carrying the power-cycle counter in one capture), and it is
    the only form that arrives during the quiet stretches, so reading only the
    0x0A shape would miss a reboot for minutes or indefinitely.

    Returns {} for anything else. In particular Read Attributes (cmd 0x00) is
    excluded on purpose: its body is a bare list of attribute ids and would
    misparse as records.
    """
    if len(raw) < 3:
        return {}

    # Low two bits of frame control: 0b01 = cluster-specific command. Read off
    # the raw byte rather than hdr.frame_control, whose accessors have moved
    # between zigpy versions.
    cluster_specific = (raw[0] & 0x03) == 0x01

    hdr, body = foundation.ZCLHeader.deserialize(raw)
    if not cluster_specific and hdr.command_id != 0x0A:
        return {}

    records = {}
    remaining = body
    while remaining:
        attr, remaining = foundation.Attribute.deserialize(remaining)
        records[attr.attrid] = attr.value.value
    return records


# ---------------------------------------------------------------------------
# EP-2 check-in acknowledgement
# ---------------------------------------------------------------------------
#
# A Control4 director answers every 64 s EP-2 check-in (cluster-specific cmd
# 0x03) with `01 <tsn> 04` on EP 196 -> 196, profile 0xC25D, cluster 0x0001.
# Measured 530 of 530 over 9.4 h, and that remote never rejoined. Unanswered,
# three missed check-ins are followed by cmd 0x02 and a rejoin, every ~336 s.
# The tsn is the sender's own counter, not an echo of the report's. The
# frames are in c4_checkin, shared with the SR-260. Set False to stop replying.
_C4_SR250_ACK_CHECKINS = True


async def _c4_send_checkin_ack(device, report_tsn: int) -> None:
    data = checkin_ack_frame(device.get_sequence())
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK,
            cluster=C4_CLUSTER_ID,
            src_ep=CHECKIN_ACK_EP,
            dst_ep=CHECKIN_ACK_EP,
            sequence=device.get_sequence(),
            data=data,
            expect_reply=False,
        )
    except Exception as exc:
        _LOGGER.warning(
            "C4 SR250: check-in ack for report tsn=0x%02x failed - %r",
            report_tsn,
            exc,
        )
        return
    _LOGGER.info(
        "C4 SR250: check-in ack sent to %s, report tsn=0x%02x reply=%s",
        device.ieee,
        report_tsn,
        data.hex(),
    )
    # The remote is awake right now: the window a held Find Remote waits for.
    await c4_flush_pending_beep(device)


class C4SR250ConfigCluster(C4ConfigCluster):
    """EP-2 config cluster that also watches the power-cycle counter.

    Attribute 0x0006 increments once per power cycle and is untouched by a
    network rejoin or a factory reset (it held across a mesh-leave reset and
    two secured rejoins, then incremented at a battery pull). That makes it the only observed signal separating "the
    remote rebooted" from "the remote re-associated".

    The bootstrap latch needs the first of those and ZDO Device_annce only
    reports the second, so a battery change that the association survived left
    the latch set and the LCD stuck on "Waiting for network". Gap and burst
    detection were both measured and rejected - the outage is *shorter* than a
    normal idle gap.
    """

    def handle_message(self, hdr, args):
        """Watch the power-cycle counter and ack check-ins, then dispatch."""
        # Patch 2 (c4_hooks) delivers raw bytes with hdr=None. Inspect them
        # before super() dispatches, because the frames that carry the counter
        # are mostly ones super() drops.
        if hdr is None and isinstance(args, (bytes, bytearray)):
            try:
                self._c4_watch_power_cycle(bytes(args))
            except Exception as exc:
                _LOGGER.debug("C4 SR250: EP2 counter watch error: %r", exc)
            try:
                self._c4_ack_checkin(bytes(args))
            except Exception as exc:
                _LOGGER.warning("C4 SR250: check-in ack error: %r", exc)
        return super().handle_message(hdr, args)

    def _c4_ack_checkin(self, raw: bytes) -> None:
        tsn = checkin_tsn(raw)
        if tsn is None:
            return
        device = self.endpoint.device
        now = _time.monotonic()
        if heartbeat_due(getattr(device, "_c4_heartbeat_at", None), now):
            device._c4_heartbeat_at = now
            self._c4_emit_heartbeat(device)
        if not _C4_SR250_ACK_CHECKINS:
            return
        last = getattr(device, "_c4_sr250_last_checkin", None)
        if last is not None and last[0] == tsn and now - last[1] < CHECKIN_DEDUP_S:
            return
        device._c4_sr250_last_checkin = (tsn, now)
        asyncio.ensure_future(_c4_send_checkin_ack(device, tsn))

    @staticmethod
    def _c4_emit_heartbeat(device) -> None:
        """Fire the check-in heartbeat through the raw cluster's handler."""
        for ep_id, ep in device.endpoints.items():
            if not ep_id:
                continue  # ZDO
            for cluster in getattr(ep, "in_clusters", {}).values():
                if isinstance(cluster, C4SR250RawCluster):
                    cluster._emit(HEARTBEAT_COMMAND, {})
                    return

    def _c4_watch_power_cycle(self, raw: bytes) -> None:
        device = self.endpoint.device
        records = _c4_parse_attribute_records(raw)
        count = records.get(C4_ATTR_POWER_CYCLE_COUNT)

        if _C4_SR250_LOG_EP2_FRAMES:
            _LOGGER.info(
                "C4 SR250 EP2: cmd=0x%02x len=%d cycles=%s dev=0x%x hex=%s",
                raw[2] if len(raw) > 2 else -1,
                len(raw),
                "-" if count is None else hex(count),
                id(device),
                raw.hex(),
            )

        if count is None:
            return

        # The counter has only ever been observed inside the model+firmware
        # report. Requiring the model in the same frame keeps a mis-walked
        # record from being read as a reboot.
        if C4_ATTR_MODEL not in records:
            _LOGGER.warning(
                "C4 SR250: power-cycle count %s without a model attribute - "
                "ignoring, and the frame shape has changed. raw=%s",
                hex(count),
                raw.hex(),
            )
            return

        previous = getattr(device, "_c4_sr250_power_cycles", None)
        device._c4_sr250_power_cycles = count

        if previous is None:
            # Nothing to compare against yet. A fresh device object has no
            # latch either, so the ordinary wake path already bootstraps; there
            # is nothing to persist across a restart.
            _LOGGER.info(
                "C4 SR250: power-cycle baseline %s for %s",
                hex(count),
                device.ieee,
            )
            return

        if count == previous:
            return

        if count < previous:
            _LOGGER.warning(
                "C4 SR250: power-cycle count went backwards, %s -> %s. "
                "Re-baselining without re-arming.",
                hex(previous),
                hex(count),
            )
            return

        if not _C4_SR250_REARM_ON_POWER_CYCLE:
            _LOGGER.info(
                "C4 SR250: WOULD re-arm bootstrap latch - power cycle "
                "%s -> %s on %s (dry run, dev=0x%x)",
                hex(previous),
                hex(count),
                device.ieee,
                id(device),
            )
            return

        _LOGGER.info(
            "C4 SR250: power cycle %s -> %s - re-arming bootstrap latch "
            "for %s (dev=0x%x)",
            hex(previous),
            hex(count),
            device.ieee,
            id(device),
        )
        # Same two writes the Device_annce path makes. The existing wake hook
        # fires the bootstrap on the next frame, so there is no new send path
        # here and no interaction with _c4_bootstrap_loop.
        device._c4_sr250_bootstrapped = False
        device._c4_sr250_last_kick = 0.0


def _c4_fix_node_desc(device) -> None:
    """Clear the bogus rx-on-when-idle bit the SR-250 advertises.

    Observed live: the remote's Device_annce carries MAC capability
    byte 0x8C - end device, MAINS power, RX-ON-WHEN-IDLE. It is a battery
    device that sleeps. Because rx_on_when_idle is set, bellows never calls
    setExtendedTimeout for it (confirmed in-log: extendedTimeout=False), the
    stack does not buffer traffic for it as a sleepy child, and it is aged
    like an always-on device. The result is a full rejoin
    (childJoinHandler -> trustCenterJoinHandler -> Device_annce) every
    328.0 s +/- 0.1 - 45 of them in 4.5 hours.

    Clearing bit 3 makes zigpy/bellows treat it as the sleepy end device it
    actually is. The device is genuinely non-compliant; this is a workaround,
    not a protocol fix.
    """
    _RX_ON_WHEN_IDLE = 0x08
    try:
        node_desc = getattr(device, "node_desc", None)
        if node_desc is None:
            return
        flags = getattr(node_desc, "mac_capability_flags", None)
        if flags is None:
            return
        flags = int(flags)
        if not flags & _RX_ON_WHEN_IDLE:
            return
        new_flags = flags & ~_RX_ON_WHEN_IDLE
        try:
            device.node_desc = node_desc.replace(mac_capability_flags=new_flags)
        except (AttributeError, TypeError):
            node_desc.mac_capability_flags = new_flags
        _LOGGER.info(
            "C4 SR250: cleared rx_on_when_idle, mac_capability_flags 0x%02X -> 0x%02X",
            flags,
            new_flags,
        )
    except Exception as _exc:
        _LOGGER.warning("C4 SR250: could not patch node_desc: %s", _exc)


# Kill switch: set False to disable the sleepy-child push below and fall back
# to the in-memory node_desc patch alone.
_C4_SR250_PUSH_SLEEPY = True


async def _c4_push_sleepy_child(device) -> None:
    """Tell the NCP and the zigpy DB that this device is a sleepy end device.

    `_c4_fix_node_desc` only rewrites the in-memory ``node_desc``. Measured:
    it applies cleanly (0x8C -> 0x84, logged twice per reload) yet
    ``node_descriptors_v15.mac_capability_flags`` stays 140 (0x8C) and every
    bootstrap still fails ZIGBEE_SEND_UNICAST_NO_ROUTE. The stack routes with
    the persisted descriptor, so an in-memory fix alone never reaches the layer
    that decides whether to buffer for a sleeping child.

    Two pushes, independently guarded so either can work alone:

    1. ``setExtendedTimeout`` on the NCP, so bellows holds a message for a
       child that is asleep instead of failing fast with NO_ROUTE.
    2. Re-persist the corrected ``node_desc`` through zigpy's own listener, so
       the DB agrees with the in-memory object across restarts.
    """
    if not _C4_SR250_PUSH_SLEEPY:
        return

    app = getattr(device, "application", None)
    if app is None:
        return

    # 1. NCP-level: buffer for this child rather than failing fast.
    #
    # `app._ezsp` is None until bellows' connect() runs, and this coroutine is
    # scheduled from the quirk's __init__ - which happens first. So wait for
    # it rather than probing once.
    #
    # Waiting for `_ezsp` to be non-None is NOT sufficient: the handle is set
    # before the controller is actually running, and calling then raises
    # ControllerError("ApplicationController is not running"). Wait for the
    # controller state too.
    ezsp = None
    for _ in range(45):
        ezsp = getattr(app, "_ezsp", None)
        if ezsp is not None and not getattr(app, "_watchdog_failures", None):
            state = getattr(app, "state", None)
            running = getattr(app, "is_running", None)
            if running is None or running:
                if state is None or getattr(state, "network_info", None) is not None:
                    break
        await asyncio.sleep(2)

    if ezsp is None:
        _LOGGER.warning(
            "C4 SR250: no EZSP handle on %s after 60s - cannot push setExtendedTimeout",
            type(app).__name__,
        )
    else:
        # bellows renamed this: modern builds expose the snake_case
        # set_extended_timeout(nwk=, ieee=, extended_timeout=), older ones the
        # raw EZSP frame setExtendedTimeout(remoteEui64=, extendedTimeout=).
        for _try in range(6):
            try:
                if hasattr(ezsp, "set_extended_timeout"):
                    await ezsp.set_extended_timeout(
                        nwk=device.nwk,
                        ieee=device.ieee,
                        extended_timeout=True,
                    )
                else:
                    await ezsp.setExtendedTimeout(
                        remoteEui64=device.ieee,
                        extendedTimeout=True,
                    )
                _LOGGER.info(
                    "C4 SR250: setExtendedTimeout(True) pushed for %s",
                    device.ieee,
                )
                break
            except Exception as exc:
                # ControllerError here means we were still too early; retry.
                _LOGGER.warning(
                    "C4 SR250: setExtendedTimeout attempt %d failed - %r",
                    _try + 1,
                    exc,
                )
                await asyncio.sleep(5)

    # 2. DB-level: make the corrected descriptor survive a restart.
    #
    # zigpy's appdb listener has NO `node_descriptor_updated` event - the
    # descriptor is persisted only via `raw_device_initialized`, which calls
    # `_save_node_descriptor`. Firing a non-existent event name is silently
    # swallowed by `listener_event`, which is exactly the trap hit on the first
    # attempt: the success line logged while the DB stayed 0x8C.
    try:
        node_desc = getattr(device, "node_desc", None)
        if node_desc is not None and not (int(node_desc.mac_capability_flags) & 0x08):
            app.listener_event("raw_device_initialized", device)
            _LOGGER.info(
                "C4 SR250: re-persisted node_desc 0x%02X for %s",
                int(node_desc.mac_capability_flags),
                device.ieee,
            )
    except Exception as exc:
        _LOGGER.warning("C4 SR250: could not persist node_desc - %s", exc)


class Control4SR250Remote(CustomDevice):
    """Control4 C4-SR250B remote - bootstrap driver + raw capture."""

    # Tasks keyed by IEEE, so a second device instance for the same physical
    # remote can cancel the first one's loops. See __init__.
    _c4_sr250_tasks_by_ieee: dict = {}

    @classmethod
    def match(cls, device):
        """Accept the SR-250's model string(s)."""
        model = getattr(device, "model", None)
        if model in (MODEL, "C4-SR250"):
            _LOGGER.debug("C4 SR250.match: accepting model=%r", model)
            return True
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", MODEL),
            (None, MODEL),
            ("Control4", "C4-SR250"),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: _REMOTE_CONTROL_DEVICE_TYPE,
                INPUT_CLUSTERS: [
                    Identify.cluster_id,
                    PowerConfiguration.cluster_id,
                    C4_MANUF_CLUSTER,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: _REMOTE_CONTROL_DEVICE_TYPE,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    PowerConfiguration.cluster_id,
                    C4DimmerManufCluster,
                    # ZHA-side virtual cluster (0xFC47), not on the wire. It
                    # is what `_get_display_cluster` looks up on EP 1 and the
                    # only route to show_list / close_list, so the pushed-list
                    # handlers below are dead without it.
                    C4SR260DisplayCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [C4SR250ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [C4SR250RawCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    # HA device triggers, generated from the same table the cluster emits
    # from so the two cannot drift. Every trigger lands on EP 197 /
    # C4_BUTTON_CLUSTER_ID because this remote routes all keypad traffic
    # through one raw-capture cluster - unlike the SR260, which fans out to a
    # virtual endpoint per key.
    #
    # These exist for the UI. Automations should prefer an event trigger on
    # zha_event keyed by device_ieee: device triggers are keyed on device_id,
    # which changes if the remote is ever re-added, and this remote re-joins
    # often enough that that is a live risk.
    device_automation_triggers = {
        **{
            (_action, _slug): {
                COMMAND: f"{_slug}_{_action}",
                CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
                ENDPOINT_ID: 197,
            }
            for _slug in C4SR250RawCluster._KEY_SLUGS.values()
            for _action in ("press", "hold", "release")
        },
        # Menu-context presses. Only List and Cncl reach us while the LCD list
        # menu is up; the arrows and Select transmit nothing at all.
        **{
            ("menu", _slug): {
                COMMAND: f"{_slug}_menu",
                CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
                ENDPOINT_ID: 197,
            }
            for _slug in ("list", "cancel")
        },
        ("motion_wake", "remote"): {
            COMMAND: "motion_wake",
            CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: 197,
        },
    }

    def __init__(self, *args, **kwargs):
        """Set up the device and correct its node descriptor."""
        super().__init__(*args, **kwargs)
        _c4_fix_node_desc(self)

        # Cancel any tasks left by a previous instance of this device. ZHA
        # reloads construct a fresh object without tearing the old one down,
        # so without this every reload stacks another bootstrap loop, with
        # orphans holding a dead app reference ("ApplicationController is not
        # running"). Cancel per-instance tasks AND any left by a different
        # device object for the same IEEE, since two instances can exist.
        _key = str(getattr(self, "ieee", "")) or "unknown"
        _reg = Control4SR250Remote._c4_sr250_tasks_by_ieee
        for _t in _reg.pop(_key, ()):
            if _t is not None and not _t.done():
                _t.cancel()
        for _attr in ("_c4_sr250_task", "_c4_sr250_sleepy_task"):
            _old = getattr(self, _attr, None)
            if _old is not None and not _old.done():
                _old.cancel()

        self._c4_sr250_seen_traffic = False
        self._c4_sr250_sleepy_task = asyncio.ensure_future(_c4_push_sleepy_child(self))
        self._c4_sr250_task = asyncio.ensure_future(self._c4_bootstrap_loop())
        _reg[_key] = (self._c4_sr250_sleepy_task, self._c4_sr250_task)

    async def _c4_bootstrap_loop(self):
        """Retry the bootstrap on a widening schedule until the remote talks.

        A sleepy end device will silently drop anything sent while it is
        asleep, so a single attempt at startup is unreliable. Pressing any key
        wakes the remote, which is why the schedule stretches out to about four
        minutes - it gives a human time to pick the thing up.
        """
        # Seed a route before the first send. The SR-250 is in no neighbour or
        # route table, and the coordinator was sending OUTGOING_DIRECT to a
        # cached nwk, so every attempt died SEND_UNICAST_NO_ROUTE. A
        # many-to-one route request makes routers report a path back to the
        # coordinator. Uses the existing helper in c4_helpers.
        try:
            _app = getattr(self, "application", None)
            if _app is not None:
                await _send_many_to_one_route_request(_app)
                _LOGGER.info(
                    "C4 SR250: many-to-one route request sent before bootstrap"
                )
        except Exception as _exc:
            _LOGGER.warning("C4 SR250: route request failed - %r", _exc)

        previous = 0
        for attempt, at_second in enumerate(BOOTSTRAP_SCHEDULE, start=1):
            await asyncio.sleep(at_second - previous)
            previous = at_second

            if self._c4_sr250_seen_traffic:
                _LOGGER.info(
                    "C4 SR250: inbound traffic seen, bootstrap loop done "
                    "after %d attempt(s)",
                    attempt - 1,
                )
                return

            try:
                _LOGGER.info(
                    "C4 SR250: bootstrap attempt %d/%d (t+%ds)",
                    attempt,
                    len(BOOTSTRAP_SCHEDULE),
                    at_second,
                )
                await c4_sr250_bootstrap(self)
            except Exception as exc:
                _LOGGER.warning(
                    "C4 SR250: bootstrap attempt %d failed: %s", attempt, exc
                )
            finally:
                # In `finally` because a unicast bootstrap to a remote with no
                # route raises NO_ROUTE, and the broadcast is what repairs that.
                if attempt in PROBE_ZDO_ATTEMPTS:
                    try:
                        await _c4_send_nwk_addr_req(self)
                    except Exception as _pexc:
                        _LOGGER.warning("C4 SR250: NWK_addr_req failed - %r", _pexc)

        if not self._c4_sr250_seen_traffic:
            _LOGGER.warning(
                "C4 SR250: bootstrap schedule exhausted with no inbound C4 "
                "traffic from %s. Wake the remote with a keypress and reload "
                "the ZHA integration to retry.",
                self.ieee,
            )


_C4_MODEL_QUIRK_MAP[MODEL] = Control4SR250Remote
_C4_MODEL_QUIRK_MAP["C4-SR250"] = Control4SR250Remote
_LOGGER.info("C4 SR250: registered %s in _C4_MODEL_QUIRK_MAP", MODEL)

# --- Robust registration ---------------------------------------------------
# c4_hooks is imported twice: as top-level ``c4_hooks`` via the sys.path insert
# at the top of this file, and as ``control4.c4_hooks`` by the zhaquirks package
# walk. Each module object owns a SEPARATE _C4_MODEL_QUIRK_MAP, and the resolve
# patch that actually gets installed reads only one of them. Registering into
# just the instance we happened to import is a coin flip (observed: zha.resolve
# found model 'C4-SR250B' but the map lookup missed). Register into all of them;
# the package __init__ makes this redundant, but it is harmless.
for _hooks_name in (
    "c4_hooks",
    "control4.c4_hooks",
    "zhaquirks.control4.c4_hooks",
):
    _hooks_mod = sys.modules.get(_hooks_name)
    _hooks_map = getattr(_hooks_mod, "_C4_MODEL_QUIRK_MAP", None)
    if _hooks_map is not None:
        _hooks_map[MODEL] = Control4SR250Remote
        _hooks_map["C4-SR250"] = Control4SR250Remote
        _LOGGER.info("C4 SR250: registered into %s", _hooks_name)

# --- Wake-triggered bootstrap ----------------------------------------------
# Observed: every timer-driven attempt died with
# sl_Status.MAC_NO_ACK_RECEIVED even though the LCD stayed lit on "Waiting for
# network". A lit screen does not mean the receiver is on - an unprovisioned
# SR-250 cycles through a rejoin/channel scan and is only reliably on-channel
# in the instant it transmits its ~30s keepalive. Firing on a timer is a blind
# shot; firing the moment it talks to us lands inside the one window where its
# radio is definitely listening.
_WAKE_KICK_COOLDOWN = 6.0


def _c4_sr250_packet_received(self, *args, **kwargs):
    # Re-arm the bootstrap latch on ZDO Device_annce (cluster 0x0013 on the
    # ZDO profile 0x0000). An announce means the remote rebooted and lost its
    # provisioning - exactly when a fresh bootstrap is required.
    #
    # Without this, the _c4_sr250_bootstrapped latch survives the REMOTE's
    # power cycle, because the flag lives on the HA-side device object, and
    # HA believes the device is bootstrapped while its LCD sits on "Waiting
    # for network".
    #
    # C4-profile frames never reach this method (c4_hooks Patch 4 intercepts
    # them and returns), but ZDO frames do - which is why the hook lives here
    # rather than in custom_profile_packet_received.
    try:
        _pkt = args[0] if args else kwargs.get("packet")
        if getattr(_pkt, "cluster_id", None) == 0x0013 and getattr(
            _pkt, "profile_id", None
        ) in (0, None):
            if getattr(self, "_c4_sr250_bootstrapped", False):
                _LOGGER.info(
                    "C4 SR250: device announce - re-arming bootstrap latch (dev=0x%x)",
                    id(self),
                )
            self._c4_sr250_bootstrapped = False
            self._c4_sr250_last_kick = 0.0
            _c4_fix_node_desc(self)
    except Exception as _exc:
        _LOGGER.debug("C4 SR250: announce re-arm error: %s", _exc)

    try:
        if not getattr(self, "_c4_sr250_bootstrapped", False):
            now = _time.monotonic()
            if now - getattr(self, "_c4_sr250_last_kick", 0.0) > _WAKE_KICK_COOLDOWN:
                self._c4_sr250_last_kick = now
                _LOGGER.info(
                    "C4 SR250: inbound packet - firing wake-triggered "
                    "bootstrap (dev=0x%x)",
                    id(self),
                )
                asyncio.ensure_future(c4_sr250_bootstrap(self))
    except Exception as _exc:
        _LOGGER.debug("C4 SR250: wake hook error: %s", _exc)
    return super(Control4SR250Remote, self).packet_received(*args, **kwargs)


Control4SR250Remote.packet_received = _c4_sr250_packet_received
_LOGGER.info("C4 SR250: wake-triggered bootstrap hook installed")

# --- Wake hook, take 2: the path C4 traffic actually uses -------------------
# c4_hooks Patch 4 intercepts C4-profile packets in ControllerApplication
# .packet_received, calls device.custom_profile_packet_received(packet) and
# then RETURNS without ever invoking Device.packet_received. So the hook above
# is on a path C4 frames never take - confirmed live: hook installed x2, fired
# 0 times, while the remote transmitted every ~36s. Hook the method the
# intercept actually calls.


def _c4_sr250_custom_profile(self, packet):
    try:
        if not getattr(self, "_c4_sr250_bootstrapped", False):
            now = _time.monotonic()
            if now - getattr(self, "_c4_sr250_last_kick", 0.0) > _WAKE_KICK_COOLDOWN:
                self._c4_sr250_last_kick = now
                _LOGGER.info(
                    "C4 SR250: C4 packet in (profile=0x%04X) - firing "
                    "bootstrap (dev=0x%x)",
                    getattr(packet, "profile_id", 0),
                    id(self),
                )
                asyncio.ensure_future(c4_sr250_bootstrap(self))
    except Exception as _exc:
        _LOGGER.debug("C4 SR250: c4 wake hook error: %s", _exc)
    return super(Control4SR250Remote, self).custom_profile_packet_received(packet)


Control4SR250Remote.custom_profile_packet_received = _c4_sr250_custom_profile
_LOGGER.info("C4 SR250: C4-profile wake hook installed")
