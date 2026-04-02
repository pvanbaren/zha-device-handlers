"""ZHA quirk for the Control4 C4-APD120 Adaptive Phase Dimmer."""

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
    Groups, Identify, LevelControl, OnOff, Scenes,
)

from zhaquirks.const import (
    BUTTON,
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
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
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_DEFAULT_ON_LEVEL,
    C4_MANUF_CLUSTER,
    C4_OFF_TRANSITION,
    C4_ON_TRANSITION,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    DIMMER_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4ButtonCluster
from c4_led_cluster import C4LEDCluster
from c4_ramp_cluster import C4RampCluster, C4_RAMP_CLUSTER_ID
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dimmer-specific clusters
# ---------------------------------------------------------------------------

class C4DimmerOnOff(CustomCluster, OnOff):
    """OnOff cluster that redirects on/off to LevelControl.

    C4 dimmers ignore standard On/Off (cluster 0x0006) commands but respond
    to move_to_level_with_on_off (cluster 0x0008, cmd 0x04).
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _get_ramp_cluster(self):
        """Find the C4RampCluster on EP 4, if available."""
        try:
            ep4 = self.endpoint.device.endpoints.get(4)
            if ep4 is not None:
                return ep4.in_clusters.get(C4_RAMP_CLUSTER_ID)
        except Exception:
            pass
        return None

    def _get_on_transition(self) -> int:
        """Return the on-ramp time in ZCL 1/10-second units."""
        ramp = self._get_ramp_cluster()
        if ramp is not None:
            return ramp.get_on_ramp_tenths()
        return C4_ON_TRANSITION

    def _get_off_transition(self) -> int:
        """Return the off-ramp time in ZCL 1/10-second units."""
        ramp = self._get_ramp_cluster()
        if ramp is not None:
            return ramp.get_off_ramp_tenths()
        return C4_OFF_TRANSITION

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=True,
        tsn=None,
        **kwargs,
    ):
        level_cluster = self.endpoint.level

        if command_id == OnOff.ServerCommandDefs.on.id:
            level = self._get_on_level()
            on_transition = self._get_on_transition()
            _LOGGER.info(
                "C4 OnOff: on() → move_to_level_with_on_off(%d, %d)",
                level, on_transition,
            )
            result = await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                level, on_transition,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )
            return result if result is not None else self._SUCCESS

        if command_id == OnOff.ServerCommandDefs.off.id:
            off_transition = self._get_off_transition()
            _LOGGER.info(
                "C4 OnOff: off() → move_to_level_with_on_off(0, %d)",
                off_transition,
            )
            result = await level_cluster.command(
                LevelControl.ServerCommandDefs.move_to_level_with_on_off.id,
                0, off_transition,
                expect_reply=False, manufacturer=manufacturer, tsn=tsn,
            )
            return result if result is not None else self._SUCCESS

        if command_id == OnOff.ServerCommandDefs.toggle.id:
            cached = self.get("on_off")
            if cached:
                return await self.command(
                    OnOff.ServerCommandDefs.off.id,
                    manufacturer=manufacturer, expect_reply=expect_reply,
                    tsn=tsn, **kwargs,
                )
            else:
                return await self.command(
                    OnOff.ServerCommandDefs.on.id,
                    manufacturer=manufacturer, expect_reply=expect_reply,
                    tsn=tsn, **kwargs,
                )

        return await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )

    def _get_on_level(self) -> int:
        level_cluster = self.endpoint.level
        if level_cluster is not None:
            on_level = level_cluster.get("on_level")
            if on_level is not None and 0 < on_level < 255:
                return on_level
            cached = level_cluster.get("current_level")
            if cached is not None and cached > 0:
                return cached
        return C4_DEFAULT_ON_LEVEL


class C4DimmerLevelControl(CustomCluster, LevelControl):
    """LevelControl that defaults expect_reply=False and caches local attrs."""

    cluster_id = LevelControl.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    _LOCAL_ATTRS = {
        LevelControl.AttributeDefs.on_level.id,
        LevelControl.AttributeDefs.on_off_transition_time.id,
        LevelControl.AttributeDefs.on_transition_time.id,
        LevelControl.AttributeDefs.off_transition_time.id,
        LevelControl.AttributeDefs.default_move_rate.id,
    }

    async def write_attributes(self, attributes, manufacturer=None):
        local_attrs  = {}
        device_attrs = {}

        for attr, value in attributes.items():
            attr_id = (
                self.find_attribute(attr).id
                if isinstance(attr, str) else attr
            )
            if attr_id in self._LOCAL_ATTRS:
                local_attrs[attr_id] = value
            else:
                device_attrs[attr] = value

        for attr_id, value in local_attrs.items():
            _LOGGER.debug(
                "C4 Level: caching local attr 0x%04X = %s", attr_id, value
            )
            self._update_attribute(attr_id, value)

        if device_attrs:
            return await super().write_attributes(
                device_attrs, manufacturer=manufacturer
            )
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        local_reads  = {}
        device_reads = []

        for attr in attributes:
            attr_id = (
                self.find_attribute(attr).id
                if isinstance(attr, str) else attr
            )
            if attr_id in self._LOCAL_ATTRS:
                local_reads[attr_id] = self.get(attr_id)
            else:
                device_reads.append(attr)

        success, failure = {}, {}
        if device_reads:
            success, failure = await super().read_attributes(
                device_reads,
                allow_cache=allow_cache, only_cache=only_cache,
                manufacturer=manufacturer,
            )
        success.update(local_reads)
        return success, failure

    async def command(
        self,
        command_id,
        *args,
        manufacturer=None,
        expect_reply=False,
        tsn=None,
        **kwargs,
    ):
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4APD120Dimmer(CustomDevice):
    """Control4 C4-APD120 Adaptive Phase Dimmer."""

    @classmethod
    def match(cls, device):
        model = getattr(device, 'model', None)
        manuf = getattr(device, 'manufacturer', None)
        _LOGGER.warning(
            "C4 APD120.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, 'ieee', '?'),
        )
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-APD120"),
            (None, "C4-APD120"),
            (None, None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS:  [Identify.cluster_id, C4_MANUF_CLUSTER],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            196: {
                PROFILE_ID: C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4.C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0101,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4DimmerOnOff,
                    C4DimmerLevelControl,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            2: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4ButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            3: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LEDCluster],
                OUTPUT_CLUSTERS: [],
            },
            4: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4RampCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {}
    for _btn_id, _btn_name in DIMMER_BUTTON_MAP.items():
        device_automation_triggers[("click",   _btn_name)] = {
            COMMAND: "click",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197,
        }
        device_automation_triggers[("press",   _btn_name)] = {
            COMMAND: "press",   CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197,
        }
        device_automation_triggers[("release", _btn_name)] = {
            COMMAND: "release", CLUSTER_ID: C4_BUTTON_CLUSTER_ID, ENDPOINT_ID: 197,
        }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-APD120"] = Control4APD120Dimmer
_LOGGER.info("C4 APD120: registered C4-APD120 in _C4_MODEL_QUIRK_MAP")
