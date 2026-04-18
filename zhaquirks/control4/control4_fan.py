"""ZHA quirk for the Control4 C4-4SF120 4-Speed Fan Controller.

Protocol confirmed from two Wireshark captures:

CAPTURE 1 — pairing/provisioning sequence:
  Model string: "c4:control4_light:C4-4SF120" → model token "C4-4SF120"
  Provisioning commands sent on C4_PROFILE_BUTTON (0xC25C), EP 1→1:
    c4.dmx.off 0000       — initialise to off
    c4.dm.tv 00 01 00 … 06 — 6 transition-time parameters

CAPTURE 2 — operational speed changes:
  Speed SET command: c4.dmx.fsc 00 [speed_hex] on C4_PROFILE_BUTTON, EP 1→1
    speed 00 = off
    speed 01 = low
    speed 02 = medium-low
    speed 03 = medium-high
    speed 04 = high
  Device state announcement on EP 197 (C4_PROFILE_BUTTON):
    0t[chan] sa c4.dmx.fs 00 00 [speed] [rpmA] [rpmB] [rpmC] [rpmD] …
    Field index 2 (0-indexed from the data list after the namespace) is the
    current fan speed, hex-encoded (00–04).

The coordinator sends `c4.dmx.fsc` commands; the device acks and then emits
a `sa c4.dmx.fs` announcement confirming the new speed.  Standard ZCL
LevelControl or OnOff commands are NOT used for speed control.
"""

import asyncio
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
from zigpy.zcl.clusters.hvac import Fan

from zhaquirks.const import (
    CLUSTER_ID,
    COMMAND,
    DEVICE_TYPE,
    DOUBLE_PRESS,
    TRIPLE_PRESS,
    QUADRUPLE_PRESS,
    ENDPOINT_ID,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
    SKIP_CONFIGURATION,
)

# Ensure patches are installed before this device class is used
import c4_hooks

import c4_helpers as C4
from c4_helpers import (
    C4_BUTTON_CLUSTER_ID,
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROVISION_DELAY,
    DIMMER_BUTTON_MAP,
    _build_c4_frame,
    C4ConfigCluster,
    C4DimmerManufCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_button_cluster import C4ButtonCluster
from c4_led_cluster import C4LEDCluster, C4_LED_CLUSTER_ID
from c4_hooks import _C4_MODEL_QUIRK_MAP

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device speed (0–4) → LED button_id (0-indexed from top of keypad)
# The C4-4SF120 has 5 physical buttons; LEDs are addressed 0–4.
# ---------------------------------------------------------------------------
_FAN_SPEED_TO_LED = {
    4: 0,   # high     → top button
    3: 1,   # med-high → second button
    2: 2,   # med-low  → third button
    1: 3,   # low      → fourth button
    0: 4,   # off      → bottom button
}
_FAN_LED_BUTTONS = list(_FAN_SPEED_TO_LED.values())  # [0, 1, 2, 3, 4]

# ---------------------------------------------------------------------------
# Fan provisioning commands — sent on C4_PROFILE_BUTTON (0xC25C), EP 1→1.
# These are the same commands captured during pairing, sent before operational
# speed commands become available.
# ---------------------------------------------------------------------------
_FAN_PROVISION_COMMANDS = [
    "c4.dm.tv 00 01 00", # Transition time param 01
    "c4.dm.tv 00 02 00", # Transition time param 02
    "c4.dm.tv 00 03 00", # Transition time param 03
    "c4.dm.tv 00 04 00", # Transition time param 04
    "c4.dm.tv 00 05 00", # Transition time param 05
    "c4.dm.tv 00 06 00", # Transition time param 06
]

# ---------------------------------------------------------------------------
# Fan control cluster
# ---------------------------------------------------------------------------

class C4FanControlCluster(CustomCluster, Fan):
    """FanControl cluster (0x0202) for the C4-4SF120.

    Fan speed is set by sending `c4.dmx.fsc 00 [speed]` on the C4 button
    profile (0xC25C).  The speed byte maps directly to ZCL fan_mode:
      0 → off, 2 → low, 3 → medium, 4 → high
      (we skip 1 = minimum speed since zha.fan only supports 3 speeds + off)

    The device reports its current speed via `sa c4.dmx.fs` announcements
    which are intercepted by C4FanButtonCluster._handle_fan_state and pushed
    back into this cluster's attribute cache via _update_fan_mode().
    """

    cluster_id = Fan.cluster_id   # 0x0202
    _SUCCESS   = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    async def async_initialize(self, from_cache=False):
        """Seed fan_mode_sequence and send provisioning commands."""
        # fan_mode_sequence 3 = Off/Low/Med/High/On — all 5 modes supported
        self._update_attribute(Fan.AttributeDefs.fan_mode_sequence.id, 3)
        self._update_attribute(Fan.AttributeDefs.fan_mode.id, 0)
        await super().async_initialize(from_cache=from_cache)
        await self._send_provision_commands()

    async def _send_provision_commands(self):
        """Send fan transition time commands on C4_PROFILE_BUTTON, EP 1→1.

        Commands are idempotent so re-sending on every HA restart is harmless.
        """
        device = self.endpoint.device
        for cmd in _FAN_PROVISION_COMMANDS:
            chan = device.get_sequence() & 0xFFFF
            full_cmd = f"0s{chan:04x} {cmd}"
            data = _build_c4_frame(0, full_cmd)
            try:
                _LOGGER.info("C4 Fan provision: %s", full_cmd)
                await device.request(
                    profile=C4_PROFILE_BUTTON,
                    cluster=C4_CLUSTER_ID,
                    src_ep=1, dst_ep=1,
                    sequence=device.get_sequence(),
                    data=data,
                    expect_reply=False,
                )
            except Exception as e:
                _LOGGER.warning("C4 Fan provision: FAILED %s — %s", cmd, e)
            await asyncio.sleep(C4_PROVISION_DELAY)

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Return cached values — device does not respond to standard ZCL reads."""
        result, failure = {}, {}
        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    continue
            else:
                attr_id = int(attr)
            cached = self._attr_cache.get(attr_id)
            if cached is not None:
                result[attr_id] = cached
            else:
                failure[attr_id] = foundation.Status.UNSUP_ATTRIBUTE
        return result, failure

    async def write_attributes(self, attributes, manufacturer=None):
        """Map fan_mode writes to c4.dmx.fsc speed commands."""
        fan_mode_id = Fan.AttributeDefs.fan_mode.id

        for attr, value in attributes.items():
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    continue
            else:
                attr_id = int(attr)

            if attr_id != fan_mode_id:
                continue

            mode = int(value)
            if not (0 <= mode <= 4):
                _LOGGER.warning("C4 Fan: out-of-range fan_mode %d — ignored", mode)
                continue

            await self._set_fan_speed(mode)
            # Optimistic local update; device will confirm with c4.dmx.fs
            self._update_attribute(fan_mode_id, mode)
            _LOGGER.info("C4 Fan: fan_mode=%d sent via c4.dmx.fsc", mode)

        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    async def _set_fan_speed(self, mode: int) -> None:
        """Send c4.dmx.fsc 00 [mode] on C4_PROFILE_BUTTON, EP 1→1.

        Command format (confirmed from capture):
          0s[chan4] c4.dmx.fsc 00 [speed2]
        where [chan4] is a 4-digit hex channel ID and [speed2] is a 2-digit
        hex fan mode (00–04).  Sent on profile 0xC25C, cluster 0x0001, EP 1→1.
        We remap modes 1-3 to 2-4 since zha.fan only supports 3 speeds + off
        """
        if (mode >= 1) and (mode <= 3):
            mode = mode + 1
        device = self.endpoint.device
        chan    = device.get_sequence() & 0xFFFF
        cmd     = f"0s{chan:04x} c4.dmx.fsc 00 {mode:02x}"
        data    = _build_c4_frame(0, cmd)

        _LOGGER.info("C4 Fan: sending %s", cmd)
        try:
            await device.request(
                profile=C4_PROFILE_BUTTON,
                cluster=C4_CLUSTER_ID,
                src_ep=1, dst_ep=1,
                sequence=device.get_sequence(),
                data=data,
                expect_reply=False,
            )
        except Exception as e:
            _LOGGER.warning("C4 Fan: c4.dmx.fsc send failed: %s", e)


# ---------------------------------------------------------------------------
# Fan button cluster
# ---------------------------------------------------------------------------

class C4FanButtonCluster(C4ButtonCluster):
    """Button/state cluster for C4-4SF120 on EP 197.

    Extends C4ButtonCluster to:
      • handle `c4.dmx.fs` fan state announcements (device→coordinator)
        and push the confirmed speed into the EP 1 FanControl cluster cache
      • suppress `c4.dmx.ls` light-state handling (fan has no load level)
      • suppress EP 1 OnOff/LevelControl sync (those clusters are absent)

    c4.dmx.fs announcement format (confirmed from capture):
      sa c4.dmx.fs 00 00 [speed] [rpmA] [rpmB] [rpmC] [rpmD] [rpmE] [rpmF]
    data[0] = '00'       (channel, always 00 in captures)
    data[1] = '00'       (unknown, always 00)
    data[2] = speed      hex 00–04
    data[3+]             RPM / sensor readings — not currently used
    """

    name         = "Control4 Fan Button Events"
    ep_attribute = "c4_fan_buttons"

    # ------------------------------------------------------------------
    # Override state announcement dispatcher to add c4.dmx.fs
    # ------------------------------------------------------------------

    def _handle_state_announcement(self, namespace, data):
        if namespace == "c4.dmx.fs":
            self._handle_fan_state(data)
        else:
            # Delegate everything else (warn, amb, bp, cc, hc, he, sc, tc…)
            # to the base class.  c4.dmx.ls is intentionally allowed through
            # so the base class logs it; _handle_light_state below suppresses
            # the actual level sync.
            super()._handle_state_announcement(namespace, data)

    def _handle_fan_state(self, data):
        """Parse c4.dmx.fs: data[2] is the current fan speed (hex 00–04)."""
        try:
            speed = int(data[2], 16)
            if not (0 <= speed <= 4):
                _LOGGER.warning(
                    "C4 fan: c4.dmx.fs speed out of range: %d", speed
                )
                return
            _LOGGER.info("C4 fan: c4.dmx.fs speed=%d", speed)
            self._update_fan_mode(speed)
            asyncio.ensure_future(self._update_speed_leds(speed))
        except (IndexError, ValueError) as e:
            _LOGGER.warning(
                "C4 fan: failed to parse c4.dmx.fs: data=%s (%s)", data, e
            )

    # ------------------------------------------------------------------
    # Suppress light-state / OnOff sync (no load on a fan controller)
    # ------------------------------------------------------------------

    def _handle_light_state(self, fields):
        """Fan has no dimmable load — ignore c4.dmx.ls level updates."""
        _LOGGER.debug("C4 fan: ignoring c4.dmx.ls (no load)")

    def _sync_state_from_event(self, event_code, button_id, params):
        """Only sync fan_mode from cc events; skip tc level sync."""
        click_count = params.get("click_count")
        if event_code == "cc" and click_count is not None:
            self._sync_cc_event(button_id, click_count)

    def _sync_cc_event(self, button_id, click_count):
        """Optimistic fan_mode from click-count confirmation.

        The definitive update arrives via c4.dmx.fs shortly after.
        0x01 = top/up button, 0x05 = bottom/down button (matches dimmer map).
        """
        if button_id == 0x01 and click_count >= 1:
            _LOGGER.info("C4 fan: top button confirmed — reporting max speed")
            self._update_fan_mode(4)
        elif button_id == 0x02 and click_count >= 1:
            _LOGGER.info("C4 fan: second button confirmed — reporting medium speed")
            self._update_fan_mode(3)
        elif button_id == 0x03 and click_count >= 1:
            _LOGGER.info("C4 fan: third button confirmed — reporting low speed")
            self._update_fan_mode(2)
        elif button_id == 0x04 and click_count >= 1:
            _LOGGER.info("C4 fan: fourth button confirmed — redirecting to low speed")
            self._update_fan_mode(1)
        elif button_id == 0x05 and click_count >= 1:
            _LOGGER.info("C4 fan: bottom button confirmed — reporting off")
            self._update_fan_mode(0)

    # ------------------------------------------------------------------
    # Helper: push fan_mode into EP 1 FanControl cluster cache
    # ------------------------------------------------------------------

    def _update_fan_mode(self, fan_mode: int) -> None:
        try:
            ep1 = self.endpoint.device.endpoints.get(1)
            if ep1 is None:
                return
            if fan_mode > 1:
                fan_mode = fan_mode - 1  # Map back to 2→1, 3→2, 4→3 for ZHA fan_mode
            fan_cluster = ep1.in_clusters.get(Fan.cluster_id)
            if fan_cluster is not None:
                fan_cluster._update_attribute(
                    Fan.AttributeDefs.fan_mode.id, fan_mode
                )
                _LOGGER.info("C4 fan: EP1 fan_mode → %d", fan_mode)
        except Exception:
            _LOGGER.warning("C4 fan: fan_mode update failed", exc_info=True)

    async def _update_speed_leds(self, device_speed: int) -> None:
        """Set the LED for the active speed's button to blue, others off."""
        try:
            ep3 = self.endpoint.device.endpoints.get(3)
            if ep3 is None:
                return
            led_cluster = ep3.in_clusters.get(C4_LED_CLUSTER_ID)
            if led_cluster is None:
                return

            # Configure button LEDs to follow the fan speed
            for btn in _FAN_LED_BUTTONS:
                await led_cluster.set_led_mode(btn, 0, 7 - btn, 0)

            _LOGGER.info("C4 fan: LED update for speed %d complete", device_speed)
        except Exception:
            _LOGGER.warning("C4 fan: LED update failed", exc_info=True)


# ---------------------------------------------------------------------------
# Device quirk
# ---------------------------------------------------------------------------

class Control4C4SF120FanController(CustomDevice):
    """Control4 C4-4SF120 4-Speed Fan Controller.

    ZHA creates a fan entity from C4FanControlCluster (0x0202) on EP 1.
    No OnOff or LevelControl cluster is present so no light/switch entity
    is created alongside the fan entity.

    Fan speeds map directly to ZCL fan_mode values:
      0 = Off, 1 = Low, 2 = Medium-Low, 3 = Medium-High, 4 = High
    """

    @classmethod
    def match(cls, device):
        model = getattr(device, 'model', None)
        manuf = getattr(device, 'manufacturer', None)
        _LOGGER.warning(
            "C4 4SF120.match called: model=%r manuf=%r ieee=%s",
            model, manuf, getattr(device, 'ieee', '?'),
        )
        if model == "C4-4SF120":
            _LOGGER.warning("C4 4SF120.match: accepting on model match")
            return True
        return super().match(device)

    signature = {
        "manufacturer_code": 0x1040,
        MODELS_INFO: [
            ("Control4", "C4-4SF120"),
            (None, "C4-4SF120"),
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
                PROFILE_ID: C4.C4_PROFILE_NETWORK,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
            197: {
                PROFILE_ID: C4.C4_PROFILE_BUTTON,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4_CLUSTER_ID],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        SKIP_CONFIGURATION: True,
        ENDPOINTS: {
            1: {
                PROFILE_ID: zha.PROFILE_ID,
                # Device type 0x0300 = Color Dimmable Light is commonly used
                # as a neutral type that doesn't force a specific entity type;
                # ZHA will create a Fan entity from C4FanControlCluster.
                DEVICE_TYPE: 0x0300,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    C4FanControlCluster,    # → ZHA fan entity
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
                INPUT_CLUSTERS:  [C4FanButtonCluster],
                OUTPUT_CLUSTERS: [],
            },
            3: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4LEDCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {
        (_action, _btn_name): {
            COMMAND: _action,
            CLUSTER_ID: C4_BUTTON_CLUSTER_ID,
            ENDPOINT_ID: 197,
        }
        for _btn_id, _btn_name in DIMMER_BUTTON_MAP.items()
        for _action in ("click", "press", "release", DOUBLE_PRESS, TRIPLE_PRESS, QUADRUPLE_PRESS)
    }


# ---------------------------------------------------------------------------
# Self-register with the get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-4SF120"] = Control4C4SF120FanController
_LOGGER.info("C4 4SF120: registered C4-4SF120 in _C4_MODEL_QUIRK_MAP")
