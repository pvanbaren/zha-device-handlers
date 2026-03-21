"""ZHA quirk for the Control4 C4-SW120 On/Off Wall Switch."""

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
from zigpy.zcl.clusters.general import Groups, Identify, OnOff, Scenes

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
    C4_BUTTON_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    DIMMER_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4SwitchButtonCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Switch-specific cluster
# ---------------------------------------------------------------------------

class C4SwitchOnOff(CustomCluster, OnOff):
    """OnOff cluster for C4 wall switches.

    Uses standard cluster 6 on/off commands (unlike dimmers which redirect
    through LevelControl).  Defaults expect_reply=False.
    """

    cluster_id = OnOff.cluster_id
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def bind(self):
        try:
            result = await super().bind()
            _LOGGER.info("C4 SwitchOnOff: bind succeeded")
        except Exception as e:
            _LOGGER.warning("C4 SwitchOnOff: bind failed (%s), continuing", e)
            result = None

        return result

    async def configure_reporting(self, *args, **kwargs):
        _LOGGER.info("C4 SwitchOnOff: skipping configure_reporting (unsupported)")
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
        _LOGGER.info(
            "C4 SwitchOnOff: cmd=%s expect_reply=%s", command_id, expect_reply
        )
        result = await super().command(
            command_id, *args,
            manufacturer=manufacturer, expect_reply=expect_reply,
            tsn=tsn, **kwargs,
        )
        return result if result is not None else self._SUCCESS


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4SW120Switch(CustomDevice):
    """Control4 C4-SW120277 (and similar) on/off wall switch."""

    @classmethod
    def match(cls, device):
        model = getattr(device, 'model', None)
        manuf = getattr(device, 'manufacturer', None)
        _LOGGER.warning(
            "C4 SW120.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, 'ieee', '?'),
        )
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-SW120277"),
            (None, "C4-SW120277"),
            (None, None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0100,
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
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                DEVICE_TYPE: 0x0100,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    Groups.cluster_id,
                    Scenes.cluster_id,
                    C4SwitchOnOff,
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
                INPUT_CLUSTERS:  [C4SwitchButtonCluster],
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
_C4_MODEL_QUIRK_MAP["C4-SW120277"] = Control4SW120Switch
_LOGGER.info("C4 SW120277: registered C4-SW120277 in _C4_MODEL_QUIRK_MAP")
