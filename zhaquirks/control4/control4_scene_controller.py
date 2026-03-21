"""ZHA quirk for the Control4 C4-KC120277 Scene Controller Keypad (8 buttons).

EP layout:
  1        — ZHA Non-Color Scene Controller (no light entity)
  2        — virtual, C4ConfigCluster
  196      — C4 network, C4ConfigCluster
  197      — C4 button, C4SceneControllerButtonCluster (routing hub only)
  200–207  — virtual per-button Event entities (one per physical button)

Handshake mechanism:
  The KC120277 has no OnOff cluster on EP 1, so the standard handshake
  path used by dimmers/switches (OnOff.bind()) is unavailable.  Instead,
  C4SceneControllerIdentifyCluster overrides bind() on the Identify cluster.
  IdentifyClusterHandler inherits ClusterHandler.async_configure (it does
  NOT override it), so the call chain on every join and reconfigure is:

    IdentifyClusterHandler.async_configure
      → ClusterHandler.async_configure
        → self.bind()
          → ClusterHandler.bind()
            → self.cluster.bind()   ← our override fires here
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
from zigpy.zcl.clusters.general import Identify

from zhaquirks.const import (
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
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    KC120277_BUTTON_EP_MAP,
    KC120277_BUTTON_MAP,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import (
    C4SceneControllerButtonCluster,
    _KC120277_BUTTON_CLUSTERS,
)
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KC120277-specific Identify cluster — fires handshake via bind()
# ---------------------------------------------------------------------------

class C4SceneControllerIdentifyCluster(CustomCluster, Identify):
    """Identify cluster that sends coordinator identity + MTORR on bind().

    IdentifyClusterHandler does not override async_configure, so
    ClusterHandler.async_configure → self.bind() → cluster.bind() fires
    on every join and every "Reconfigure device" — the same reliable path
    used by C4DimmerOnOff / C4SwitchOnOff on the other devices.
    """

    async def bind(self):
        try:
            result = await super().bind()
            _LOGGER.info("C4 KC120277: Identify bind succeeded")
            return result
        except Exception as e:
            _LOGGER.warning(
                "C4 KC120277: Identify bind failed (%s), continuing", e
            )
            return (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4KC120277SceneController(CustomDevice):
    """Control4 C4-KC120277 Scene Controller Keypad (8 buttons)."""

    @classmethod
    def match(cls, device):
        model = getattr(device, "model", None)
        manuf = getattr(device, "manufacturer", None)
        _LOGGER.warning(
            "C4 KC120277.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, "ieee", "?"),
        )
        if model == "C4-KC120277":
            _LOGGER.warning("C4 KC120277.match: accepting on model match")
            return True
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-KC120277"),
            (None, "C4-KC120277"),
            (None, None),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0101,
                INPUT_CLUSTERS:  [Identify.cluster_id, C4_MANUF_CLUSTER],
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
        ENDPOINTS: {
            1: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0830,   # Non-Color Scene Controller — no light entity
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    C4SceneControllerIdentifyCluster,  # bind() → identity + MTORR
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            2: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
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
                INPUT_CLUSTERS:  [C4SceneControllerButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            # Virtual per-button endpoints — one Event entity each in ZHA
            **{
                200 + btn_id: {
                    PROFILE_ID:      zha.PROFILE_ID,
                    DEVICE_TYPE:     0x0000,
                    INPUT_CLUSTERS:  [_KC120277_BUTTON_CLUSTERS[btn_id]],
                    OUTPUT_CLUSTERS: [],
                }
                for btn_id in KC120277_BUTTON_MAP
            },
        },
    }

    # One trigger entry per (action, button_name)
    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND:     _action,
            CLUSTER_ID:  C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: KC120277_BUTTON_EP_MAP[_btn_id],
        }
        for _btn_id, _btn_name in KC120277_BUTTON_MAP.items()
        for _action in (SHORT_PRESS, DOUBLE_PRESS, LONG_PRESS, LONG_RELEASE)
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-KC120277"] = Control4KC120277SceneController
_LOGGER.info("C4 KC120277: registered C4-KC120277 in _C4_MODEL_QUIRK_MAP")
