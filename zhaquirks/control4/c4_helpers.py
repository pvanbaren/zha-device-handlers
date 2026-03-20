"""Shared constants, store, helper utilities, and shared clusters for Control4 ZHA quirks.

Shared clusters defined here (used by 2+ device files):
  C4DimmerManufCluster  — EP 1 manufacturer cluster (all devices)
  C4ConfigCluster       — EP 2 / EP 196 config cluster (dimmer, switch, scene controller)
"""

import asyncio
import json
import logging
import os
import struct
import sys

# Make this directory importable by sibling modules regardless of load order.
_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

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
C4_CLUSTER_ID       = 0x0001   # C4 serial-over-ZigBee cluster (same wire ID)
C4_BUTTON_CLUSTER_ID = 0xFC42  # ZHA-side virtual cluster for button events

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
}

# Virtual endpoint IDs for KC120277 per-button Event entities (ZHA-side only)
KC120277_BUTTON_EP_MAP: dict[int, int] = {
    btn_id: 200 + btn_id for btn_id in range(8)
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
    _LOGGER.info("C4: saving IEEE→model map to %s: %s", _C4_STORE_PATH, data)
    with open(_C4_STORE_PATH, "w") as f:
        json.dump(data, f)


def get_model_from_ieee(key: str) -> str | None:
    return _C4_IEEE_MODEL_MAP.get(key)


def set_model_for_ieee(key: str, value: str) -> None:
    _C4_IEEE_MODEL_MAP[key] = value
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _save_store, dict(_C4_IEEE_MODEL_MAP))


# ---------------------------------------------------------------------------
# Frame / command helpers
# ---------------------------------------------------------------------------

def _build_c4_frame(seq_num, ascii_cmd: str) -> bytes:
    """Build a C4 serial-over-ZigBee APS payload (ASCII command + CRLF).

    The APS header is generated automatically by device.request(); do NOT
    include it here.  seq_num is unused — zigpy manages the APS counter.
    """
    return (ascii_cmd + "\r\n").encode("ascii")


async def _c4_send_commands(device, commands, c4_seq_start, label):
    """Send a list of C4 ASCII commands to a device.

    Returns (success_count, fail_count, next_c4_seq).
    """
    c4_seq = c4_seq_start
    success_count = fail_count = 0

    for cmd in commands:
        frame = _build_c4_frame(c4_seq, cmd)
        try:
            _LOGGER.info(
                "C4 provision [%s]: [%02x] %s (%d bytes)",
                label, c4_seq, cmd, len(frame),
            )
            await device.request(
                profile=C4_PROFILE_NETWORK,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=frame,
                expect_reply=False,
            )
            success_count += 1
        except Exception as e:
            _LOGGER.warning(
                "C4 provision [%s]: [%02x] FAILED %s — %s",
                label, c4_seq, cmd, e,
            )
            fail_count += 1

        c4_seq = (c4_seq + 1) & 0xFF
        await asyncio.sleep(C4_PROVISION_DELAY)

    return success_count, fail_count, c4_seq


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

    _LOGGER.warning(
        "C4 identity (%s): Read Attr Rsp to %s ep2→2 zcl_seq=0x%02x IEEE=%s data=%s",
        source, device.ieee, zcl_seq, coordinator_ieee, data.hex(),
    )
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK, cluster=C4_CLUSTER_ID,
            src_ep=2, dst_ep=2,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
        _LOGGER.info("C4 identity (%s): sent successfully", source)
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

    _LOGGER.warning(
        "C4 report identity (%s): Report Attr to %s ep2→2 zcl_seq=0x%02x IEEE=%s data=%s",
        source, device.ieee, zcl_seq, coordinator_ieee, data.hex(),
    )
    try:
        await device.request(
            profile=C4_PROFILE_NETWORK, cluster=C4_CLUSTER_ID,
            src_ep=2, dst_ep=2,
            sequence=device.get_sequence(), data=data, expect_reply=False,
        )
        _LOGGER.info("C4 report identity (%s): sent successfully", source)
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
            _LOGGER.info("Many-to-One Route Request sent via EZSP")
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
            _LOGGER.info("Many-to-One Route Request sent via ZNP")
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
            _LOGGER.warning(
                "C4 persist (%s): device_updated() succeeded for %s model=%r",
                source, device.ieee, device.model,
            )
            return
        except Exception as e:
            _LOGGER.warning("C4 persist (%s): device_updated() raised %s", source, e)

    try:
        app.listener_event("device_updated", device)
        _LOGGER.warning(
            "C4 persist (%s): listener_event(device_updated) fired for %s model=%r",
            source, device.ieee, device.model,
        )
        return
    except Exception as e:
        _LOGGER.warning(
            "C4 persist (%s): listener_event(device_updated) raised %s", source, e
        )

    if hasattr(app, '_dblistener') and hasattr(app._dblistener, 'device_updated'):
        try:
            app._dblistener.device_updated(device)
            _LOGGER.warning(
                "C4 persist (%s): _dblistener.device_updated() succeeded for %s model=%r",
                source, device.ieee, device.model,
            )
            return
        except Exception as e:
            _LOGGER.warning(
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
        _LOGGER.info("C4 sync (%s): level=%d", source, level_raw)
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
            _LOGGER.info("C4 sync (%s): on_off=%s", source, is_on)
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
        _LOGGER.info("C4 sync (%s): basic model=%r", source, model)
    except Exception:
        _LOGGER.warning("C4 sync model (%s): failed", source, exc_info=True)


# ---------------------------------------------------------------------------
# Sniff model string from a ZCL Report Attributes payload
# ---------------------------------------------------------------------------

def _c4_sniff_model(device, inner: bytes) -> None:
    """Peek into a ZCL Report Attributes payload for attr 0x0007 (model string).

    Called from the broadcast intercept patch before any cluster routing.
    On success, caches the model in the IEEE→model store and, if device.model
    is not yet set, writes it to device.model and schedules DB persistence.
    """
    try:
        hdr, remaining = foundation.ZCLHeader.deserialize(inner)
        if hdr.command_id != 0x0A:
            return
        while remaining:
            attr, remaining = foundation.Attribute.deserialize(remaining)
            if attr.attrid == C4_ATTR_MODEL and isinstance(attr.value.value, str):
                raw = attr.value.value
                parts = raw.split(":")
                model = parts[2] if len(parts) >= 3 else raw
                if model and model not in _INVALID_MODELS:
                    _LOGGER.info(
                        "C4 sniffer: caching ieee=%r -> model=%r",
                        device.ieee, model,
                    )
                    set_model_for_ieee(str(device.ieee), model)

                if not device.model or device.model in _INVALID_MODELS:
                    device.model = model
                    device.manufacturer = "Control4"
                    _LOGGER.warning(
                        "C4 sniffer: set device.model=%r manufacturer=%r on 0x%04X",
                        model, device.manufacturer, device.nwk,
                    )
                    _c4_persist_device(device, "sniffer_immediate")

                    async def _deferred_persist(dev=device):
                        await asyncio.sleep(5)
                        _LOGGER.warning(
                            "C4 sniffer: deferred persist for %s model=%r",
                            dev.ieee, dev.model,
                        )
                        _c4_persist_device(dev, "sniffer_deferred")
                    asyncio.ensure_future(_deferred_persist())
                else:
                    _LOGGER.warning(
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
        _LOGGER.info("C4 manuf: hdr=%s args=%s", hdr, args)
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    def _update_attribute(self, attrid, value):
        _LOGGER.info("C4 manuf attr: 0x%04X = %s", attrid, value)
        super()._update_attribute(attrid, value)


class C4ConfigCluster(CustomCluster):
    """Config/identity cluster on C4 proprietary endpoints (EP 2, EP 196).

    Used by: C4-APD120 dimmer, C4-SW120 switch, C4-KC120277 scene controller.
    The outlet variant (C4OutletConfigCluster) lives in control4_outlet.py.
    """

    cluster_id   = C4_CLUSTER_ID
    name         = "Control4 Config"
    ep_attribute = "c4_config"
    _c4_custom_handler = True

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)

        if attrid == C4_ATTR_MODEL and isinstance(value, str):
            _LOGGER.warning(
                "C4 config model string: raw=%r ep=%s", value,
                self.endpoint.endpoint_id,
            )
            device = self.endpoint.device
            parts = value.split(':', 2)
            new_model = parts[2] if len(parts) >= 3 else value
            if not device.model or device.model in _INVALID_MODELS:
                device.model = new_model
                device.manufacturer = "Control4"
                _LOGGER.warning(
                    "C4 config: set device.model=%r on %s",
                    device.model, device.ieee,
                )
                _c4_persist_device(device, "c4_config_attr")
            else:
                _LOGGER.info(
                    "C4 config: device.model already=%r on %s — not overwriting",
                    device.model, device.ieee,
                )
            _sync_ep1_model(device, device.model, "c4_config_0007")

        elif attrid == C4_ATTR_FIRMWARE and isinstance(value, str):
            _LOGGER.info("C4 firmware: %s", value)

        elif attrid == C4_ATTR_DIM_LEVEL:
            level_raw = value if isinstance(value, int) else 0
            _LOGGER.info(
                "C4 config: dim level report = %d (ep %s)",
                level_raw, self.endpoint.endpoint_id,
            )
            _sync_ep1_level(self.endpoint.device, level_raw, "ep2_report")

        else:
            _LOGGER.info("C4 config: 0x%04X = %s", attrid, value)

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        _LOGGER.info("C4 config request: hdr=%s", hdr)
        super().handle_cluster_request(hdr, args, dst_addressing=dst_addressing)

    def handle_message(self, hdr, args):
        # Intercept patch calls handle_message(None, raw_bytes).
        if hdr is None and isinstance(args, (bytes, bytearray)) and len(args) >= 3:
            try:
                hdr, args = foundation.ZCLHeader.deserialize(args)
            except Exception as e:
                _LOGGER.warning(
                    "C4 config ep %s: failed to parse ZCL header: %s — raw=%s",
                    self.endpoint.endpoint_id, e,
                    args.hex() if isinstance(args, (bytes, bytearray)) else repr(args),
                )
                return

        _LOGGER.warning(
            "C4 config handle_message: ep=%s cmd=0x%02x args_hex=%s",
            self.endpoint.endpoint_id,
            hdr.command_id if hdr else -1,
            args.hex() if isinstance(args, (bytes, bytearray)) else repr(args),
        )

        # cmd 0x00: Read Attributes — device polling for controller identity
        if hdr.command_id == 0x00:
            _LOGGER.warning(
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
        # zigpy's read_attributes() future resolves.
        if hdr.command_id == 0x01:
            try:
                return super().handle_message(hdr, args)
            except Exception as e:
                _LOGGER.info(
                    "C4 config ep %s: cmd 0x01 base handler: %s",
                    self.endpoint.endpoint_id, e,
                )
                return super().handle_message(hdr, args)

        # cmd 0x0A: Report Attributes — parse and cache
        if hdr.command_id == 0x0A:
            try:
                remaining = args
                while remaining:
                    attr, remaining = foundation.Attribute.deserialize(remaining)
                    _LOGGER.warning(
                        "C4 config report: ep=%s attr=0x%04x value=%r",
                        self.endpoint.endpoint_id, attr.attrid, attr.value.value,
                    )
                    self._update_attribute(attr.attrid, attr.value.value)
            except Exception as e:
                _LOGGER.warning(
                    "C4 config: Report Attributes parse failed on ep %s: %s",
                    self.endpoint.endpoint_id, e,
                )
            return super().handle_message(hdr, args)

        # All other C4-proprietary commands — log and discard
        _LOGGER.info(
            "C4 config ep %s: ignoring unhandled cmd=0x%02x args=%s",
            self.endpoint.endpoint_id, hdr.command_id,
            args.hex() if isinstance(args, (bytes, bytearray)) else repr(args),
        )
