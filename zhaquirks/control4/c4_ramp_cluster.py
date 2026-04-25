"""C4 ramp/transition time cluster — controls dimmer ramp rates on Control4 devices.

Documented from the C4-APD120 dimmer provisioning protocol (c4.dm.tv namespace).
During provisioning, the coordinator queries 9 transition time parameters via Get
commands.  This cluster allows reading and writing those parameters from Home
Assistant via ZHA service calls.

Transition time indices (from APD120 capture):
  Index 01:  100 ms  — fast/instant ramp
  Index 02:  750 ms  — on-ramp time  (used by C4DimmerOnOff for turn-on)
  Index 03: 2000 ms  — off-ramp time (used by C4DimmerOnOff for turn-off)
  Index 04: 5000 ms  — slow fade 1
  Index 05: 5000 ms  — slow fade 2
  Index 06:  100 ms  — fast ramp (secondary)
  Index 08:    0 ms  — disabled
  Index 09:    0 ms  — disabled
  Index 0A:    0 ms  — disabled

Protocol:
  Get:  0g<seq4> c4.dm.tv <ch> <idx>
  Set:  0s<seq4> c4.dm.tv <ch> <idx> <value_hex_ms>

Values are unsigned 16-bit integers representing milliseconds.

Exported:
  C4RampCluster         — cluster with ramp-rate commands
  C4_RAMP_CLUSTER_ID    — cluster ID (0xFC44)
  RAMP_IDX_*            — named constants for transition time indices
"""

import asyncio
import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
import zigpy.types as t
from zigpy.zcl.foundation import BaseCommandDefs, ZCLCommandDef

from c4_helpers import (
    C4_PROFILE_BUTTON,
    C4_CLUSTER_ID,
    C4_PROVISION_DELAY,
    _build_c4_frame,
    next_c4_seq,
)

_LOGGER = logging.getLogger(__name__)

C4_RAMP_CLUSTER_ID = 0xFC44

# ---------------------------------------------------------------------------
# Transition time index constants
# ---------------------------------------------------------------------------
RAMP_IDX_FAST        = 0x01   # 100 ms default — instant/fast ramp
RAMP_IDX_ON          = 0x02   # 750 ms default — on-ramp time
RAMP_IDX_OFF         = 0x03   # 2000 ms default — off-ramp time
RAMP_IDX_SLOW_1      = 0x04   # 5000 ms default — slow fade 1
RAMP_IDX_SLOW_2      = 0x05   # 5000 ms default — slow fade 2
RAMP_IDX_FAST_2      = 0x06   # 100 ms default — fast ramp (secondary)
RAMP_IDX_DISABLED_1  = 0x08   # 0 ms — disabled
RAMP_IDX_DISABLED_2  = 0x09   # 0 ms — disabled
RAMP_IDX_DISABLED_3  = 0x0A   # 0 ms — disabled

# All known indices in provisioning order
RAMP_INDICES = [
    RAMP_IDX_FAST, RAMP_IDX_ON, RAMP_IDX_OFF,
    RAMP_IDX_SLOW_1, RAMP_IDX_SLOW_2, RAMP_IDX_FAST_2,
    RAMP_IDX_DISABLED_1, RAMP_IDX_DISABLED_2, RAMP_IDX_DISABLED_3,
]

# Default values (ms) observed from the device
RAMP_DEFAULTS_MS = {
    RAMP_IDX_FAST:       100,
    RAMP_IDX_ON:         750,
    RAMP_IDX_OFF:        2000,
    RAMP_IDX_SLOW_1:     5000,
    RAMP_IDX_SLOW_2:     5000,
    RAMP_IDX_FAST_2:     100,
    RAMP_IDX_DISABLED_1: 0,
    RAMP_IDX_DISABLED_2: 0,
    RAMP_IDX_DISABLED_3: 0,
}

# Friendly names for logging / UI
RAMP_INDEX_NAMES = {
    RAMP_IDX_FAST:       "fast",
    RAMP_IDX_ON:         "on_ramp",
    RAMP_IDX_OFF:        "off_ramp",
    RAMP_IDX_SLOW_1:     "slow_1",
    RAMP_IDX_SLOW_2:     "slow_2",
    RAMP_IDX_FAST_2:     "fast_2",
    RAMP_IDX_DISABLED_1: "disabled_1",
    RAMP_IDX_DISABLED_2: "disabled_2",
    RAMP_IDX_DISABLED_3: "disabled_3",
}


def _ms_to_zcl_tenths(ms: int) -> int:
    """Convert milliseconds to ZCL 1/10-second units."""
    return max(0, (ms + 50) // 100)  # round to nearest tenth


class C4RampCluster(CustomCluster):
    """Ramp/transition time cluster for Control4 dimmers.

    Provides commands to read and write dimmer ramp rates via the C4
    serial-over-ZigBee protocol (c4.dm.tv namespace).

    The cluster caches the current ramp times locally so that
    C4DimmerOnOff can read them for on/off transition commands.

    Usage from Home Assistant (via zha.issue_zigbee_cluster_command):
      service: zha.issue_zigbee_cluster_command
      data:
        ieee: "00:0f:ff:..."
        endpoint_id: 4
        cluster_id: 0xFC44
        cluster_type: in
        command: 0          # set_ramp_rate
        command_type: server
        args:
          - 2               # index (RAMP_IDX_ON = on-ramp)
          - 1500            # time_ms (1500 ms)
    """

    cluster_id = C4_RAMP_CLUSTER_ID
    name = "Control4 Ramp Control"
    ep_attribute = "c4_ramp_control"
    _c4_custom_handler = True

    # Local cache: index → time in ms
    _ramp_times: dict[int, int] = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Initialize cache with defaults
        self._ramp_times = dict(RAMP_DEFAULTS_MS)

    class ServerCommandDefs(BaseCommandDefs):
        """Server commands exposed to ZHA UI and service calls."""

        set_ramp_rate = ZCLCommandDef(
            id=0x00,
            schema={
                "index": t.uint8_t,
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_on_ramp = ZCLCommandDef(
            id=0x01,
            schema={
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_off_ramp = ZCLCommandDef(
            id=0x02,
            schema={
                "time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        set_on_off_ramps = ZCLCommandDef(
            id=0x03,
            schema={
                "on_time_ms": t.uint16_t,
                "off_time_ms": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

    # ------------------------------------------------------------------
    # Public accessors for other clusters (C4DimmerOnOff)
    # ------------------------------------------------------------------

    def get_on_ramp_tenths(self) -> int:
        """Return the on-ramp time in ZCL 1/10-second units."""
        return _ms_to_zcl_tenths(self._ramp_times.get(RAMP_IDX_ON, 750))

    def get_off_ramp_tenths(self) -> int:
        """Return the off-ramp time in ZCL 1/10-second units."""
        return _ms_to_zcl_tenths(self._ramp_times.get(RAMP_IDX_OFF, 2000))

    def get_ramp_ms(self, index: int) -> int:
        """Return a ramp time in ms for a given index."""
        return self._ramp_times.get(index, RAMP_DEFAULTS_MS.get(index, 0))

    # ------------------------------------------------------------------
    # Command method overrides
    # ------------------------------------------------------------------

    async def set_ramp_rate(self, index, time_ms):
        """Set a specific transition time by index."""
        idx = int(index)
        ms = int(time_ms)
        if idx not in RAMP_INDICES:
            _LOGGER.warning(
                "C4 Ramp: invalid index 0x%02x (valid: %s)",
                idx, [f"0x{i:02x}" for i in RAMP_INDICES],
            )
            return
        await self._send_ramp_set(idx, ms)

    async def set_on_ramp(self, time_ms):
        """Set the on-ramp time (index 0x02)."""
        await self._send_ramp_set(RAMP_IDX_ON, int(time_ms))

    async def set_off_ramp(self, time_ms):
        """Set the off-ramp time (index 0x03)."""
        await self._send_ramp_set(RAMP_IDX_OFF, int(time_ms))

    async def set_on_off_ramps(self, on_time_ms, off_time_ms):
        """Set both on-ramp and off-ramp times in one call."""
        await self._send_ramp_set(RAMP_IDX_ON, int(on_time_ms))
        await self._send_ramp_set(RAMP_IDX_OFF, int(off_time_ms))

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Log any unexpected inbound cluster requests."""
        _LOGGER.debug(
            "C4 Ramp: cluster request cmd=0x%02x args=%s",
            hdr.command_id if hdr else -1, args,
        )

    # ------------------------------------------------------------------
    # C4 command builder and transport
    # ------------------------------------------------------------------

    async def _send_ramp_set(self, index: int, time_ms: int):
        """Send a c4.dm.tv Set command to change a transition time."""
        device = self.endpoint.device
        name = RAMP_INDEX_NAMES.get(index, f"0x{index:02x}")

        # Clamp to uint16 range
        time_ms = max(0, min(65535, time_ms))

        # Channel is always 00 for single-output dimmer
        seq = next_c4_seq(device)
        cmd = f"0s{seq:04x} c4.dm.tv 00 {index:02x} {time_ms:04x}"

        _LOGGER.info(
            "C4 Ramp: setting %s (idx 0x%02x) to %d ms — cmd: %s",
            name, index, time_ms, cmd,
        )

        frame = _build_c4_frame(seq, cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=frame,
                expect_reply=False,
            )
            # Update local cache on successful send
            old_ms = self._ramp_times.get(index, 0)
            self._ramp_times[index] = time_ms
            _LOGGER.info(
                "C4 Ramp: %s updated: %d ms → %d ms (ZCL: %d tenths)",
                name, old_ms, time_ms, _ms_to_zcl_tenths(time_ms),
            )

            # Sync ZCL LevelControl transition attributes on EP 1
            self._sync_zcl_transition_attrs()

        except Exception as e:
            _LOGGER.warning(
                "C4 Ramp: failed to set %s to %d ms — %s", name, time_ms, e,
            )

    def _sync_zcl_transition_attrs(self):
        """Push cached ramp times into the EP 1 LevelControl attribute cache.

        This keeps ZHA's attribute display consistent with the actual device
        ramp times and ensures C4DimmerOnOff uses the correct values.
        """
        try:
            from zigpy.zcl.clusters.general import LevelControl

            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            level_cluster = ep1.in_clusters.get(LevelControl.cluster_id)
            if level_cluster is None:
                return

            on_tenths = self.get_on_ramp_tenths()
            off_tenths = self.get_off_ramp_tenths()

            level_cluster._update_attribute(
                LevelControl.AttributeDefs.on_transition_time.id, on_tenths,
            )
            level_cluster._update_attribute(
                LevelControl.AttributeDefs.off_transition_time.id, off_tenths,
            )
            level_cluster._update_attribute(
                LevelControl.AttributeDefs.on_off_transition_time.id, on_tenths,
            )
            _LOGGER.debug(
                "C4 Ramp: synced ZCL attrs — on=%d, off=%d (1/10 s)",
                on_tenths, off_tenths,
            )
        except Exception:
            _LOGGER.debug("C4 Ramp: ZCL attr sync failed", exc_info=True)
