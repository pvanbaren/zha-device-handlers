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

import logging
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
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import (
    C4RemoteButtonCluster,
    _SR260_BUTTON_CLUSTERS,
)
from c4_display_cluster import C4SR260DisplayCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)


# Zigbee Home Automation device type 0x0006 = Remote Control
_REMOTE_CONTROL_DEVICE_TYPE = 0x0006


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
            (None, None),
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
    # SR260 has no separate hold/click-count protocol — each press emits
    # bb→be (mapped to SHORT_PRESS / SHORT_RELEASE).  Long-press detection
    # can be built in HA by measuring the gap between the two events.
    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND:     _action,
            CLUSTER_ID:  C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: SR260_BUTTON_EP_MAP[_btn_id],
        }
        for _btn_id, _btn_name in SR260_BUTTON_MAP.items()
        for _action in (SHORT_PRESS, SHORT_RELEASE)
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-SR260"] = Control4SR260Remote
_LOGGER.info("C4 SR260: registered C4-SR260 in _C4_MODEL_QUIRK_MAP")
