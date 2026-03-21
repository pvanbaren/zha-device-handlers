"""ZHA quirk for the Control4 LOZ-5S1-W Switched Outlet.

Hardware: single switched outlet with physical on/off paddle button.

Zigbee endpoints after interview:
  1   — ZHA profile, device_type 0x0101, clusters [Basic … OnOff Level Time]
  2   — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  196 — C4_PROFILE_NETWORK 0xC25D, cluster 0x0001 (injected by patch)
  197 — C4_PROFILE_BUTTON  0xC25C, cluster 0x0001 (injected by patch)
  198 — C4_PROFILE_OUTLET  0xC25E, cluster 0x0000 (auto-discovered)

EP 198 (profile 0xC25E) is absent on APD120/SW120, making it the reliable
signature discriminator.

Outlet-specific notes:
  • attr 0x0000 on EP 2 cluster 0x0001 is an on/off flag (not a dim level).
  • Provisioning sequence unknown — only coordinator identity is sent.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.profiles import zha
from zigpy.quirks import CustomCluster, CustomDevice
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import (
    Basic, Groups, Identify, LevelControl, OnOff, Scenes, Time,
)

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
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_ATTR_DIM_LEVEL,
    C4_ATTR_FIRMWARE,
    C4_ATTR_MODEL,
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4_PROFILE_OUTLET,
    _INVALID_MODELS,
    _sync_ep1_onoff,
    C4ConfigCluster,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4DualOutletButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Outlet-specific config cluster variant
# ---------------------------------------------------------------------------

class C4OutletConfigCluster(C4ConfigCluster):
    """C4 config cluster for outlet devices (EP 2 / EP 196).

    On a dimmer, attr 0x0000 on cluster 0x0001 is the dim level (0–255).
    On the LOZ-5S1-W the same attribute is an on/off flag (2=on, 0=off).
    Everything else is identical to C4ConfigCluster.
    """

    def _update_attribute(self, attrid, value):
        if attrid == C4_ATTR_DIM_LEVEL:
            is_on = isinstance(value, int) and value > 0
            _LOGGER.info(
                "C4 outlet config (ep %s): on/off state = %s (raw=%r)",
                self.endpoint.endpoint_id, "on" if is_on else "off", value,
            )
            _sync_ep1_onoff(
                self.endpoint.device, is_on, "outlet_ep2_report"
            )
        else:
            super()._update_attribute(attrid, value)


# ---------------------------------------------------------------------------
# EP 198 operational state cluster
# ---------------------------------------------------------------------------

class C4OutletStateCluster(CustomCluster):
    """Operational state cluster on EP 198 of the LOZ-5S1-W (profile 0xC25E).

    Handles unsolicited Report Attributes and Read Attr Rsp from the device
    for model string, firmware version, and on/off state.
    """

    cluster_id   = Basic.cluster_id   # 0x0000 — matches EP 198 descriptor
    name         = "Control4 Outlet State"
    ep_attribute = "c4_outlet_state"
    _c4_custom_handler = True

    def _update_attribute(self, attrid, value):
        super()._update_attribute(attrid, value)

        if attrid == C4_ATTR_MODEL and isinstance(value, str):
            _LOGGER.warning("C4 outlet state: model = %r", value)
            device = self.endpoint.device
            if not device.model or device.model in ("", "unknown"):
                device.model = value

        elif attrid == C4_ATTR_FIRMWARE and isinstance(value, str):
            _LOGGER.info("C4 outlet state: firmware = %s", value)

        elif attrid == 0x0000:
            is_on = isinstance(value, int) and value > 0
            _LOGGER.info(
                "C4 outlet state ep198: on/off = %s (raw=%r)",
                "on" if is_on else "off", value,
            )
            _sync_ep1_onoff(
                self.endpoint.device, is_on, "outlet_ep198_report"
            )
        else:
            _LOGGER.info("C4 outlet state: attr 0x%04x = %r", attrid, value)

    def handle_message(self, hdr, args):
        _LOGGER.info(
            "C4 outlet state ep198: cmd=0x%02x args_hex=%s",
            hdr.command_id if hdr else -1,
            args.hex() if isinstance(args, (bytes, bytearray)) else repr(args),
        )

        if hdr is None:
            _LOGGER.warning(
                "C4 outlet state ep198: no ZCL header, raw=%r", args
            )
            return

        if hdr.command_id in (0x01, 0x0A):
            try:
                remaining = args
                while remaining:
                    if hdr.command_id == 0x01:
                        # Read Attr Rsp: [attr_id(2)] [status(1)] [type(1)] [value]
                        rec, remaining = (
                            foundation.ReadAttributeRecord.deserialize(remaining)
                        )
                        if rec.status == foundation.Status.SUCCESS:
                            self._update_attribute(rec.attrid, rec.value.value)
                    else:
                        # Report Attributes: [attr_id(2)] [type(1)] [value]
                        attr, remaining = (
                            foundation.Attribute.deserialize(remaining)
                        )
                        self._update_attribute(attr.attrid, attr.value.value)
            except Exception as exc:
                _LOGGER.warning(
                    "C4 outlet state ep198: parse error cmd=0x%02x — %s",
                    hdr.command_id, exc,
                )
            return

        if hdr.command_id == 0x00:
            _LOGGER.info(
                "C4 outlet state ep198: ignoring Read Attributes from device"
            )
            return

        super().handle_cluster_request(hdr, args)

    async def bind(self):
        _LOGGER.info(
            "C4 OutletState ep198: skipping bind (C4 proprietary profile)"
        )
        return (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def configure_reporting(self, *args, **kwargs):
        _LOGGER.info("C4 OutletState: skipping configure_reporting (unsupported)")
        return [[foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        count = len(records) if records else 1
        return [[
            foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
            for _ in range(count)
        ]]

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        result = {}
        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    _LOGGER.info(
                        "C4 OutletState ep198: unknown attr name %r, skipping", attr
                    )
                    continue
            else:
                attr_id = attr
            result[attr_id] = self._attr_cache.get(attr_id)
        return result, {}


# ---------------------------------------------------------------------------
# Outlet OnOff clusters
# ---------------------------------------------------------------------------

class C4OutletOnOff(CustomCluster, OnOff):
    """OnOff cluster for the LOZ-5S1-W outlet (outlet 0 / EP 1)."""

    cluster_id = OnOff.cluster_id
    OUTLET_IDX = 0
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def bind(self):
        try:
            result = await super().bind()
            _LOGGER.info("C4 OutletOnOff: bind succeeded")
        except Exception as exc:
            _LOGGER.warning("C4 OutletOnOff: bind failed (%s), continuing", exc)
            result = None

        return result

    async def configure_reporting(self, *args, **kwargs):
        _LOGGER.info("C4 OutletOnOff: skipping configure_reporting (unsupported)")
        return [[foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        count = len(records) if records else 1
        return [[
            foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
            for _ in range(count)
        ]]

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        _LOGGER.info("C4 OutletOnOff: cmd=%s", command_id)
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


class C4Outlet1OnOff(C4OutletOnOff):
    """OnOff cluster for the second outlet (outlet 1 / EP 11).

    Sends a raw ZCL on/off command to EP 2 on the device and optimistically
    updates the local cache since no device report arrives on this synthetic EP.
    """

    OUTLET_IDX = 1

    async def bind(self):
        """Skip bind — EP 1 OutletOnOff already handles identity / routing."""
        _LOGGER.info("C4 Outlet1OnOff: skipping bind (handled by EP 1)")
        return self._SUCCESS

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        if command_id == OnOff.ServerCommandDefs.toggle.id:
            cached = self.get("on_off")
            return await self.command(
                OnOff.ServerCommandDefs.off.id
                if cached else OnOff.ServerCommandDefs.on.id,
                manufacturer=manufacturer, expect_reply=expect_reply,
                tsn=tsn, **kwargs,
            )

        if command_id in (
            OnOff.ServerCommandDefs.on.id,
            OnOff.ServerCommandDefs.off.id,
        ):
            is_on = command_id == OnOff.ServerCommandDefs.on.id
            zcl_seq = self.endpoint.device.get_sequence() & 0xFF
            data = bytes([0x01, zcl_seq, command_id])
            _LOGGER.info(
                "C4 Outlet1OnOff: ZCL cmd=0x%02x → EP 2, data=%s",
                command_id, data.hex(),
            )
            try:
                await self.endpoint.device.request(
                    profile=zha.PROFILE_ID,
                    cluster=OnOff.cluster_id,
                    src_ep=1, dst_ep=2,
                    sequence=self.endpoint.device.get_sequence(),
                    data=data,
                    expect_reply=False,
                )
            except Exception as exc:
                _LOGGER.warning("C4 Outlet1OnOff: send failed: %s", exc)

            # Optimistic update — device will never report on synthetic EP 11
            self._update_attribute(OnOff.AttributeDefs.on_off.id, is_on)
            return self._SUCCESS

        return await super(C4OutletOnOff, self).command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4LOZ5S1WOutlet(CustomDevice):
    """Control4 LOZ-5S1-W Switched Outlet."""

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("loz-5s1-w", "Control4"),
            ("loz-5s1-w", None),
            (None, None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS: [
                    Basic.cluster_id,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    OnOff.cluster_id,
                    LevelControl.cluster_id,
                    Time.cluster_id,
                ],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            # EP 198 is the key discriminator — APD120 and SW120 lack it.
            198: {
                PROFILE_ID: C4_PROFILE_OUTLET,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS:  [Basic.cluster_id],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0100,   # On/Off — no dimming for an outlet
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4OutletOnOff,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            11: {                       # synthetic EP for outlet 2
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0100,
                INPUT_CLUSTERS:  [C4Outlet1OnOff],
                OUTPUT_CLUSTERS: [],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4DualOutletButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            198: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4OutletStateCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {
        ("click",   "outlet_1"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_1"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_1"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("click",   "outlet_2"): {COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("press",   "outlet_2"): {COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
        ("release", "outlet_2"): {COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197},
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["loz-5s1-w"] = Control4LOZ5S1WOutlet
_LOGGER.info("C4 LOZ-5S1-W: registered loz-5s1-w in _C4_MODEL_QUIRK_MAP")
