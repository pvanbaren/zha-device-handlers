"""C4 button clusters — shared across dimmer, switch, scene controller, outlet.

Classes exported:
  C4ButtonCluster                  — base, used by dimmer
  C4SwitchButtonCluster            — on/off switch variant
  C4SceneControllerButtonCluster   — KC120277 8-button keypad
  C4DualOutletButtonCluster        — LOZ-5S1-W dual outlet
  _KC120277_BUTTON_CLUSTERS        — per-button virtual cluster dict (btn_id → class)
  _make_kc120277_button_cluster()  — factory for per-button EventableCluster
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.zcl.clusters.general import LevelControl, OnOff

from zhaquirks import EventableCluster
from zhaquirks.const import (
    BUTTON,
    DOUBLE_PRESS,
    ENDPOINT_ID,
    LONG_PRESS,
    LONG_RELEASE,
    SHORT_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
)

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    DIMMER_BUTTON_MAP,
    DIMMER_EVENT_MAP,
    KC120277_BUTTON_EP_MAP,
    KC120277_BUTTON_MAP,
    OUTLET_EP_MAP,
    _sync_ep1_level,
    _sync_ep1_onoff,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Factory: one EventableCluster class per KC120277 physical button
# ---------------------------------------------------------------------------

def _make_kc120277_button_cluster(button_num: int) -> type:
    """Return a unique EventableCluster class for one KC120277 physical button.

    Each class lives on its own virtual endpoint (EP 200+button_num), so ZHA
    creates one independent Event entity per button.  Physical Zigbee frames
    never arrive on these endpoints — routing is done by
    C4SceneControllerButtonCluster._handle_button_event().
    """

    class _ButtonCluster(EventableCluster):
        cluster_id   = C4_BUTTON_CLUSTER_ID
        name         = f"Button {button_num + 1}"
        ep_attribute = f"c4_scene_btn_{button_num}"
        _c4_custom_handler = False  # no physical routing

        def handle_message(self, hdr, args):
            pass  # no physical packets arrive here

        def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
            pass

    _ButtonCluster.__name__     = f"C4SceneButton{button_num}Cluster"
    _ButtonCluster.__qualname__ = f"C4SceneButton{button_num}Cluster"
    return _ButtonCluster


# One cluster class per button — keyed by button_id (0–7)
_KC120277_BUTTON_CLUSTERS: dict[int, type] = {
    btn_id: _make_kc120277_button_cluster(btn_id)
    for btn_id in KC120277_BUTTON_MAP
}


# ---------------------------------------------------------------------------
# Base button cluster
# ---------------------------------------------------------------------------

class C4ButtonCluster(EventableCluster):
    """Button events from C4 devices on endpoint 197 (0xC5).

    Handles state announcements from the C4 serial-over-Zigbee protocol.
    Subclasses override BUTTON_MAP and/or individual event handlers.
    """

    cluster_id   = C4_BUTTON_CLUSTER_ID
    name         = "Control4 Button Events"
    ep_attribute = "c4_buttons"
    _c4_custom_handler = True

    BUTTON_MAP = DIMMER_BUTTON_MAP

    # ------------------------------------------------------------------
    # Message entry points
    # ------------------------------------------------------------------

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.debug("C4 button request: hdr=%s args=%s", hdr, args)
        self._process_raw(hdr, args)

    def handle_message(self, hdr, args):
        self._process_raw(hdr, args)

    # ------------------------------------------------------------------
    # Raw frame parser
    # ------------------------------------------------------------------

    def _process_raw(self, hdr, args):
        raw_bytes = None
        if isinstance(args, (bytes, bytearray)):
            raw_bytes = bytes(args)
        elif args and isinstance(args, (list, tuple)):
            if isinstance(args[0], (bytes, bytearray)):
                raw_bytes = bytes(args[0])
            elif isinstance(args[0], int):
                raw_bytes = bytes(args)
            elif isinstance(args[0], (list, tuple)):
                raw_bytes = bytes(args[0])

        if raw_bytes is None:
            _LOGGER.warning(
                "C4 button: cannot extract bytes: args=%s type=%s",
                args, type(args),
            )
            return

        text = raw_bytes.decode("ascii", errors="replace").strip()
        _LOGGER.debug("C4 button text: %r", text)

        cmd = text.split()
        if len(cmd) >= 3:
            prefix = cmd[0][0:2]
            if prefix == "0t":
                msg_type = "announce"
            elif prefix == "0r":
                msg_type = "report"
            elif prefix == "0s":
                msg_type = "set"
            elif prefix == "0g":
                msg_type = "get"
            elif prefix == "0i":
                msg_type = "initialize"
            else:
                msg_type = prefix

            if cmd[1] == "sa":
                self._handle_state_announcement(cmd[2], cmd[3:])
                return
            elif msg_type == "report":
                _LOGGER.info("C4 report: %s", text)
                return

            self.listener_event(
                "zha_send_event",
                {
                    "command": "raw_button_frame",
                    "params": {
                        "type": msg_type,
                        "sequence": cmd[0][2:] if len(cmd[0]) > 2 else None,
                        "namespace": cmd[1] if len(cmd) > 2 else None,
                        "data": cmd[2:] if len(cmd) > 3 else None,
                        "raw_text": text,
                    },
                },
            )

    # ------------------------------------------------------------------
    # State announcement dispatcher
    # ------------------------------------------------------------------

    def _handle_state_announcement(self, namespace, data):
        _LOGGER.info("C4 state: %s data='%s'", namespace, data)

        if namespace == "c4.dmx.dim":
            self._handle_dim_level(data)
        elif namespace == "c4.dmx.ls":
            self._handle_light_state(data)
        elif namespace == "c4.dmx.warn":
            _LOGGER.info("C4 state: warning = %s", data)
        elif namespace == "c4.dmx.amb":
            _LOGGER.info("C4 state: ambient = %s", data)
        elif namespace == "c4.dmx.bp":
            if data:
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.cc":
            if len(data) >= 2:
                _LOGGER.info(
                    "C4 state: click count, button = %s, clicks = %s",
                    data[0], data[1],
                )
                self._handle_button_event(namespace, data[0], data[1])
        elif namespace == "c4.dmx.hc":
            _LOGGER.info("C4 state: hold, button = %s", data[0])
            self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.he":
            _LOGGER.info("C4 state: release after hold, button = %s", data[0])
            self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.sc":
            if data:
                _LOGGER.info("C4 state: scene change, button = %s", data[0])
                self._handle_button_event(namespace, data[0])
        elif namespace == "c4.dmx.tc":
            if data:
                _LOGGER.info("C4 state: transition complete, button = %s", data[0])
                self._handle_button_event(namespace, data[0])
        else:
            _LOGGER.info(
                "C4 state: unknown namespace '%s', data = %s", namespace, data
            )

    # ------------------------------------------------------------------
    # Button event handler
    # ------------------------------------------------------------------

    def _handle_button_event(self, namespace, button, extra=None):
        button_id = int(button, 16)
        button_name = self.BUTTON_MAP.get(button_id, f"button_{button_id:#04x}")
        event_code = namespace.split('.')[-1] if namespace else "unknown"
        action = DIMMER_EVENT_MAP.get(event_code, f"unknown_{event_code}")

        if action == "click_count" and extra is not None:
            if extra == "01":
                action = SHORT_PRESS
            elif extra == "02":
                action = DOUBLE_PRESS
            elif extra == "03":
                action = TRIPLE_PRESS
            else:
                action = QUADRUPLE_PRESS

        params = {"event_code": event_code, "button_id": button_id}
        if extra is not None:
            params["extra_value"] = int(extra, 16)
            if event_code == "cc":
                params["click_count"] = params["extra_value"]

        _LOGGER.info(
            "C4 button event: button=%s event=%s", button_name, event_code
        )
        self._sync_state_from_event(event_code, button_id, params)

        self.listener_event(
            "zha_send_event",
            action,
            {
                BUTTON: button_name,
                ENDPOINT_ID: self.endpoint.endpoint_id,
            },
        )

    # ------------------------------------------------------------------
    # Light state / dim level helpers
    # ------------------------------------------------------------------

    def _handle_light_state(self, fields):
        """Parse c4.dmx.ls multi-field light state; field[2] = 0–100 % (hex)."""
        try:
            if len(fields) >= 3:
                level_pct = int(fields[2], 16)
                zcl_level = round(level_pct * 254 / 100) if level_pct > 0 else 0
                _LOGGER.info(
                    "C4 state: ls level=%d%% → zcl=%d", level_pct, zcl_level
                )
                _sync_ep1_level(self.endpoint.device, zcl_level, "0t_dmx_ls")
        except (ValueError, IndexError) as e:
            _LOGGER.warning("C4 state: failed to parse ls: '%s' (%s)", fields, e)

    def _handle_dim_level(self, data):
        """Handle c4.dmx.dim; data[0] = 0–100 % encoded as a hex byte."""
        try:
            level_pct = int(data[0], 16)
            zcl_level = round(level_pct * 254 / 100) if level_pct > 0 else 0
            _LOGGER.info(
                "C4 state: dim level=%d%% → zcl=%d", level_pct, zcl_level
            )
            _sync_ep1_level(self.endpoint.device, zcl_level, "c4.dmx.dim")
        except (ValueError, IndexError, TypeError):
            _LOGGER.warning("C4: failed to parse dim level: '%s'", data)

    # ------------------------------------------------------------------
    # State sync helpers
    # ------------------------------------------------------------------

    def _sync_state_from_event(self, event_code, button_id, params):
        click_count = params.get("click_count")
        if event_code == "cc" and click_count is not None:
            self._sync_cc_event(button_id, click_count)
        elif event_code == "tc":
            extra = params.get("extra_value")
            if extra is not None:
                _sync_ep1_level(self.endpoint.device, extra, "tc_event")

    def _sync_cc_event(self, button_id, click_count):
        """Sync on/off from c4.dmx.cc click-count confirmation."""
        try:
            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            onoff_cluster  = ep1.in_clusters.get(OnOff.cluster_id)
            level_cluster  = ep1.in_clusters.get(LevelControl.cluster_id)

            if button_id == 0x01 and click_count >= 1:
                _LOGGER.debug("C4 button sync: ON confirmed (btn=0x01 cc=%d)", click_count)
                if onoff_cluster is not None:
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, True
                    )
            elif button_id == 0x05 and click_count >= 1:
                _LOGGER.debug("C4 button sync: OFF confirmed (btn=0x05 cc=%d)", click_count)
                if onoff_cluster is not None:
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, False
                    )
                if level_cluster is not None:
                    level_cluster.update_attribute(
                        LevelControl.AttributeDefs.current_level.id, 0
                    )
        except Exception:
            _LOGGER.warning("C4 button sync: failed", exc_info=True)


# ---------------------------------------------------------------------------
# Switch variant
# ---------------------------------------------------------------------------

class C4SwitchButtonCluster(C4ButtonCluster):
    """Button events for on/off switches — syncs OnOff only (no LevelControl)."""

    name         = "Control4 Switch Button Events"
    ep_attribute = "c4_switch_buttons"

    def _handle_dim_level_text(self, text):
        parts = text.split()
        try:
            level_raw = int(parts[-1], 16)
            _sync_ep1_onoff(self.endpoint.device, level_raw > 0, "sw_dm_t0c")
        except (ValueError, IndexError):
            _LOGGER.warning("C4: failed to parse dim level: '%s'", text)

    def _sync_cc_event(self, button_id, click_count):
        try:
            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            onoff_cluster = ep1.in_clusters.get(OnOff.cluster_id)
            if onoff_cluster is not None:
                if button_id == 0x01 and click_count >= 1:
                    _LOGGER.info("C4 switch sync: ON (btn=0x01)")
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, True
                    )
                elif button_id == 0x05 and click_count >= 1:
                    _LOGGER.info("C4 switch sync: OFF (btn=0x05)")
                    onoff_cluster.update_attribute(
                        OnOff.AttributeDefs.on_off.id, False
                    )
        except Exception:
            _LOGGER.info("C4 switch sync: failed", exc_info=True)


# ---------------------------------------------------------------------------
# Scene controller variant
# ---------------------------------------------------------------------------

class C4SceneControllerButtonCluster(C4ButtonCluster):
    """Button events for C4-KC120277; routes to per-button virtual endpoints."""

    name         = "Control4 Scene Controller Button Events"
    ep_attribute = "c4_scene_controller_buttons"
    BUTTON_MAP   = KC120277_BUTTON_MAP

    def _handle_light_state(self, fields):
        _LOGGER.debug("C4 scene ctrl: ignoring c4.dmx.ls (no load)")

    def _sync_state_from_event(self, event_code, button_id, params):
        pass  # no EP 1 clusters to sync on a scene controller

    def _sync_cc_event(self, button_id, click_count):
        pass

    def _handle_button_event(self, namespace, button, extra=None):
        try:
            button_id = int(button, 16)
        except (ValueError, TypeError):
            _LOGGER.warning("C4 scene ctrl: invalid button hex %r", button)
            return

        event_code = namespace.split(".")[-1] if namespace else "unknown"
        action = DIMMER_EVENT_MAP.get(event_code, f"unknown_{event_code}")
        if action == "click_count" and extra is not None:
            if extra == "01":
                action = SHORT_PRESS
            elif extra == "02":
                action = DOUBLE_PRESS
            elif extra == "03":
                action = TRIPLE_PRESS
            else:
                action = QUADRUPLE_PRESS

        _LOGGER.info(
            "C4 scene ctrl: button_id=0x%02x event=%s action=%s extra=%s",
            button_id, event_code, action, extra,
        )

        ep_id = KC120277_BUTTON_EP_MAP.get(button_id)
        if ep_id is None:
            _LOGGER.warning(
                "C4 scene ctrl: button_id=0x%02x has no virtual EP — "
                "add it to KC120277_BUTTON_EP_MAP", button_id,
            )
            return

        ep = self.endpoint.device.endpoints.get(ep_id)
        if ep is None:
            _LOGGER.warning(
                "C4 scene ctrl: virtual EP %d not in device endpoints "
                "(re-pair after quirk update?)", ep_id,
            )
            return

        btn_cluster = ep.in_clusters.get(C4_BUTTON_CLUSTER_ID)
        if btn_cluster is None:
            _LOGGER.warning(
                "C4 scene ctrl: no cluster 0x%04X on EP %d",
                C4_BUTTON_CLUSTER_ID, ep_id,
            )
            return

        btn_cluster.listener_event("zha_send_event", action, {ENDPOINT_ID: ep_id})
        _LOGGER.info(
            "C4 scene ctrl: fired %r on EP %d cluster %s",
            action, ep_id, type(btn_cluster).__name__,
        )


# ---------------------------------------------------------------------------
# Dual outlet variant
# ---------------------------------------------------------------------------

class C4DualOutletButtonCluster(C4SwitchButtonCluster):
    """Button/state cluster for dual-outlet devices (LOZ-5S1-W).

    Protocol (from Wireshark captures):
      State announcements arrive as:
        0t<chan> sa c4.dm.tc <outlet_idx> <level>\r\n
      where outlet_idx is 00 or 01, level is 64 (ON) or 00 (OFF).

      The c4.dmx.* namespaces used by dimmers/switches are NOT used by
      the outlet — only c4.dm.tc (state announce) and c4.dm.tv (set).
    """

    name         = "Control4 Dual Outlet Button Events"
    ep_attribute = "c4_dual_outlet_buttons"

    def _sync_onoff_for_outlet(self, outlet_idx, is_on):
        ep_id = OUTLET_EP_MAP.get(outlet_idx)
        if ep_id is None:
            _LOGGER.warning("C4 dual outlet: unknown outlet index %d", outlet_idx)
            return
        try:
            ep = self.endpoint.device.endpoints.get(ep_id)
            if ep is None:
                return
            onoff = ep.in_clusters.get(OnOff.cluster_id)
            if onoff is not None:
                _LOGGER.info(
                    "C4 dual outlet: ep%d (outlet %d) on_off=%s",
                    ep_id, outlet_idx, is_on,
                )
                onoff.update_attribute(OnOff.AttributeDefs.on_off.id, is_on)
        except Exception:
            _LOGGER.warning("C4 dual outlet: sync failed", exc_info=True)

    def _handle_state_announcement(self, namespace, data):
        """Handle c4.dm.tc state announcements from the outlet.

        Format: sa c4.dm.tc <outlet_idx_hex> <level_hex>
        outlet_idx: 00 or 01
        level: 64 (=100 decimal, ON) or 00 (OFF)
        """
        if namespace == "c4.dm.tc":
            if len(data) >= 2:
                try:
                    outlet_idx = int(data[0], 16)
                    level = int(data[1], 16)
                    is_on = level > 0
                    _LOGGER.info(
                        "C4 dual outlet: c4.dm.tc outlet=%d level=%d on=%s",
                        outlet_idx, level, is_on,
                    )
                    self._sync_onoff_for_outlet(outlet_idx, is_on)
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(
                        "C4 dual outlet: failed to parse c4.dm.tc: data=%s (%s)",
                        data, e,
                    )
            else:
                _LOGGER.warning(
                    "C4 dual outlet: c4.dm.tc too few fields: %s", data
                )
            return

        # Fall through to parent for any other namespaces (e.g. c4.dmx.*)
        super()._handle_state_announcement(namespace, data)

    def _sync_cc_event(self, button_id, click_count):
        # On dual outlet devices, cc button_id is the outlet index
        self._sync_onoff_for_outlet(button_id, click_count == 1)
