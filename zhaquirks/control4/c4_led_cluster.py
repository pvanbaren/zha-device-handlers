"""C4 LED control cluster — configures per-button LED indicators on Control4 keypads.

Documented from the C4-APD120 dimmer provisioning protocol (c4.dmx.led namespace).
The same protocol applies to the C4-KC120277 scene controller keypad and other
Control4 devices with LED indicators (up to 12 buttons).

LED config protocol (from Rev E provisioning capture):
  Namespace: c4.dmx.led
  Command:   0s<addr> c4.dmx.led <group> <param> <value>

  Behavioral params (1-byte values):
    param 00 = mode       (00 = normal)
    param 01 = behavior   (00 = off, 01 = on-indicator, or the level at which
                           the LED turns on.  On the C4-4SF120 fan controller
                           the behavior value encodes the fan speed:
                             07 = fan speed 4 (high)
                             06 = fan speed 3 (med-high)
                             05 = fan speed 2 (med-low)
                             04 = fan speed 1 (low)
                             03 = fan speed 0 (off))
    param 02 = color mode (00 = default, 01 = on-indicator, 02 = custom color)

  Color params (3-byte RGB values):
    param 03 = on-color   (e.g., 0000ff = blue)
    param 04 = off-color  (e.g., 000000 = off)

Exported:
  C4LEDCluster             — cluster with LED control commands
  C4_LED_CLUSTER_ID        — cluster ID (0xFC43)
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

import c4_helpers as C4
from c4_helpers import (
    C4_PROFILE_BUTTON,
    C4_CLUSTER_ID,
    C4_PROVISION_DELAY,
    _build_c4_frame,
)

_LOGGER = logging.getLogger(__name__)

C4_LED_CLUSTER_ID = 0xFC43

# ---------------------------------------------------------------------------
# EEPROM address base for LED config (from APD120 provisioning map)
# ---------------------------------------------------------------------------
# Behavioral params:  0x8906 + (group * 3) + param_offset
# RGB color params:   0x892A + (group * 2) + (param - 3)
#
# For runtime LED changes we use the same address map.  The device processes
# the c4.dmx.led namespace — the EEPROM address tells it where to persist.
_LED_BEHAV_BASE = 0x8906
_LED_COLOR_BASE = 0x892A

# LED behavioral param indices
LED_PARAM_MODE = 0x00
LED_PARAM_BEHAVIOR = 0x01
LED_PARAM_COLOR_MODE = 0x02
LED_PARAM_ON_COLOR = 0x03
LED_PARAM_OFF_COLOR = 0x04

# Behavior values
LED_BEHAVIOR_OFF = 0x00
LED_BEHAVIOR_ON_INDICATOR = 0x01
# Fan speed level behaviors (C4-4SF120): behavior = speed + 3
LED_BEHAVIOR_FAN_SPEED_0 = 0x03
LED_BEHAVIOR_FAN_SPEED_1 = 0x04
LED_BEHAVIOR_FAN_SPEED_2 = 0x05
LED_BEHAVIOR_FAN_SPEED_3 = 0x06
LED_BEHAVIOR_FAN_SPEED_4 = 0x07

# Color mode values
LED_COLOR_MODE_DEFAULT = 0x00
LED_COLOR_MODE_ON_INDICATOR = 0x01
LED_COLOR_MODE_CUSTOM = 0x02

# Common colors (RGB hex strings)
LED_COLOR_OFF = "000000"
LED_COLOR_BLUE = "0000ff"
LED_COLOR_RED = "ff0000"
LED_COLOR_GREEN = "00ff00"
LED_COLOR_WHITE = "ffffff"
LED_COLOR_AMBER = "ffbf00"
LED_COLOR_CYAN = "00ffff"
LED_COLOR_MAGENTA = "ff00ff"


def _led_behav_addr(group: int, param: int) -> int:
    """EEPROM address for a behavioral LED param (params 00-02)."""
    return _LED_BEHAV_BASE + (group * 3) + param


def _led_color_addr(group: int, param: int) -> int:
    """EEPROM address for an RGB LED color param (params 03-04)."""
    return _LED_COLOR_BASE + (group * 2) + (param - LED_PARAM_ON_COLOR)


class C4LEDCluster(CustomCluster):
    """LED control cluster for Control4 keypads.

    Provides commands to configure per-button LED colors and behaviors.
    Commands are sent to the device as C4 serial-over-ZigBee set (0s) frames
    using the c4.dmx.led namespace.

    Usage from Home Assistant (via zha.issue_zigbee_cluster_command):
      service: zha.issue_zigbee_cluster_command
      data:
        ieee: "00:0f:ff:..."
        endpoint_id: 3
        cluster_id: 0xFC43
        cluster_type: in
        command: 0          # set_led_color
        command_type: server
        args:
          - 0               # button_id (0-11)
          - 0               # on_red
          - 0               # on_green
          - 255             # on_blue
          - 0               # off_red
          - 0               # off_green
          - 0               # off_blue
    """

    cluster_id = C4_LED_CLUSTER_ID
    name = "Control4 LED Control"
    ep_attribute = "c4_led_control"
    _c4_custom_handler = True

    # Track the running C4 sequence number for LED commands
    _c4_led_seq = 0x50

    class ServerCommandDefs(BaseCommandDefs):
        """Server commands exposed to ZHA UI and service calls."""

        set_led_color = ZCLCommandDef(
            id=0x00,
            schema={
                "button_id": t.uint8_t,
                "on_red": t.uint8_t,
                "on_green": t.uint8_t,
                "on_blue": t.uint8_t,
                "off_red": t.uint8_t,
                "off_green": t.uint8_t,
                "off_blue": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

        set_led_mode = ZCLCommandDef(
            id=0x01,
            schema={
                "button_id": t.uint8_t,
                "mode": t.uint8_t,
                "behavior": t.uint8_t,
                "color_mode": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

        set_led_all_same_color = ZCLCommandDef(
            id=0x02,
            schema={
                "num_buttons": t.uint8_t,
                "on_red": t.uint8_t,
                "on_green": t.uint8_t,
                "on_blue": t.uint8_t,
                "off_red": t.uint8_t,
                "off_green": t.uint8_t,
                "off_blue": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

        set_all_led_modes = ZCLCommandDef(
            id=0x03,
            schema={
                "num_buttons": t.uint8_t,
                "mode": t.uint8_t,
                "behavior": t.uint8_t,
                "color_mode": t.uint8_t,
            },
            is_manufacturer_specific=True,
        )

    # ------------------------------------------------------------------
    # Command method overrides
    # ------------------------------------------------------------------
    # ZHA calls these by name (matching ServerCommandDefs) via:
    #   getattr(cluster, commands[command].name)(**params)
    # By defining them explicitly we intercept the call and use our C4
    # transport instead of zigpy's default Cluster.request() ZCL path.

    async def set_led_color(
        self, button_id, on_red, on_green, on_blue,
        off_red, off_green, off_blue,
    ):
        """Set LED on-color and off-color for a single button."""
        on_color = f"{int(on_red):02x}{int(on_green):02x}{int(on_blue):02x}"
        off_color = f"{int(off_red):02x}{int(off_green):02x}{int(off_blue):02x}"
        await self._send_led_color(int(button_id), on_color, off_color)

    async def set_led_mode(self, button_id, mode, behavior, color_mode):
        """Set LED behavioral params for a single button."""
        await self._send_led_mode(
            int(button_id), int(mode), int(behavior), int(color_mode),
        )

    async def set_led_all_same_color(
        self, num_buttons, on_red, on_green, on_blue,
        off_red, off_green, off_blue,
    ):
        """Set all buttons to the same LED color."""
        on_color = f"{int(on_red):02x}{int(on_green):02x}{int(on_blue):02x}"
        off_color = f"{int(off_red):02x}{int(off_green):02x}{int(off_blue):02x}"
        await self._send_led_all_same_color(int(num_buttons), on_color, off_color)

    async def set_all_led_modes(self, num_buttons, mode, behavior, color_mode):
        """Set all buttons to the same LED mode/behavior/color_mode."""
        await self._send_all_led_modes(
            int(num_buttons), int(mode), int(behavior), int(color_mode),
        )

    def handle_cluster_request(self, hdr, args, *, dst_addressing=None):
        """Log any unexpected inbound cluster requests."""
        _LOGGER.debug(
            "C4 LED: cluster request cmd=0x%02x args=%s",
            hdr.command_id if hdr else -1, args,
        )

    # ------------------------------------------------------------------
    # C4 command builders
    # ------------------------------------------------------------------

    async def _send_led_color(self, button_id: int, on_color: str, off_color: str):
        """Send LED on-color and off-color for a single button.

        Also enables the LED with behavior=on-indicator, color_mode=custom.
        """
        device = self.endpoint.device

        # Build the command list: behavioral setup + RGB colors
        on_addr = _led_color_addr(button_id, LED_PARAM_ON_COLOR)
        off_addr = _led_color_addr(button_id, LED_PARAM_OFF_COLOR)
        mode_addr = _led_behav_addr(button_id, LED_PARAM_MODE)
        behav_addr = _led_behav_addr(button_id, LED_PARAM_BEHAVIOR)
        cmode_addr = _led_behav_addr(button_id, LED_PARAM_COLOR_MODE)

        commands = [
            # Enable the LED with custom color mode
            f"0s{mode_addr:04x} c4.dmx.led {button_id:02x} 00 00",
            f"0s{behav_addr:04x} c4.dmx.led {button_id:02x} 01 01",
            f"0s{cmode_addr:04x} c4.dmx.led {button_id:02x} 02 02",
            # Set the RGB colors
            f"0s{on_addr:04x} c4.dmx.led {button_id:02x} 03 {on_color}",
            f"0s{off_addr:04x} c4.dmx.led {button_id:02x} 04 {off_color}",
        ]

        _LOGGER.info(
            "C4 LED: setting button %d on=%s off=%s",
            button_id, on_color, off_color,
        )

        success, fail, self._c4_led_seq = await self._send_c4_commands(
            device, commands, f"led_color_btn{button_id}"
        )
        _LOGGER.info(
            "C4 LED: button %d color: %d ok, %d failed",
            button_id, success, fail,
        )

    async def _send_led_mode(
        self, button_id: int, mode: int, behavior: int, color_mode: int
    ):
        """Send LED behavioral params for a single button."""
        device = self.endpoint.device
        mode_addr = _led_behav_addr(button_id, LED_PARAM_MODE)
        behav_addr = _led_behav_addr(button_id, LED_PARAM_BEHAVIOR)
        cmode_addr = _led_behav_addr(button_id, LED_PARAM_COLOR_MODE)

        commands = [
            f"0s{mode_addr:04x} c4.dmx.led {button_id:02x} 00 {mode:02x}",
            f"0s{behav_addr:04x} c4.dmx.led {button_id:02x} 01 {behavior:02x}",
            f"0s{cmode_addr:04x} c4.dmx.led {button_id:02x} 02 {color_mode:02x}",
        ]

        _LOGGER.info(
            "C4 LED: setting button %d mode=%02x behavior=%02x color_mode=%02x",
            button_id, mode, behavior, color_mode,
        )

        success, fail, self._c4_led_seq = await self._send_c4_commands(
            device, commands, f"led_mode_btn{button_id}"
        )
        _LOGGER.info(
            "C4 LED: button %d mode: %d ok, %d failed",
            button_id, success, fail,
        )

    async def _send_led_all_same_color(
        self, num_buttons: int, on_color: str, off_color: str
    ):
        """Set all buttons to the same LED color."""
        device = self.endpoint.device
        commands = []

        for btn in range(num_buttons):
            mode_addr = _led_behav_addr(btn, LED_PARAM_MODE)
            behav_addr = _led_behav_addr(btn, LED_PARAM_BEHAVIOR)
            cmode_addr = _led_behav_addr(btn, LED_PARAM_COLOR_MODE)
            on_addr = _led_color_addr(btn, LED_PARAM_ON_COLOR)
            off_addr = _led_color_addr(btn, LED_PARAM_OFF_COLOR)

            commands.extend([
                f"0s{mode_addr:04x} c4.dmx.led {btn:02x} 00 00",
                f"0s{behav_addr:04x} c4.dmx.led {btn:02x} 01 01",
                f"0s{cmode_addr:04x} c4.dmx.led {btn:02x} 02 02",
                f"0s{on_addr:04x} c4.dmx.led {btn:02x} 03 {on_color}",
                f"0s{off_addr:04x} c4.dmx.led {btn:02x} 04 {off_color}",
            ])

        _LOGGER.info(
            "C4 LED: setting %d buttons to on=%s off=%s (%d commands)",
            num_buttons, on_color, off_color, len(commands),
        )

        success, fail, self._c4_led_seq = await self._send_c4_commands(
            device, commands, "led_all_color"
        )
        _LOGGER.info(
            "C4 LED: all buttons color: %d ok, %d failed", success, fail,
        )

    async def _send_all_led_modes(
        self, num_buttons: int, mode: int, behavior: int, color_mode: int
    ):
        """Set all buttons to the same LED mode/behavior/color_mode."""
        device = self.endpoint.device
        commands = []

        for btn in range(num_buttons):
            mode_addr = _led_behav_addr(btn, LED_PARAM_MODE)
            behav_addr = _led_behav_addr(btn, LED_PARAM_BEHAVIOR)
            cmode_addr = _led_behav_addr(btn, LED_PARAM_COLOR_MODE)

            commands.extend([
                f"0s{mode_addr:04x} c4.dmx.led {btn:02x} 00 {mode:02x}",
                f"0s{behav_addr:04x} c4.dmx.led {btn:02x} 01 {behavior:02x}",
                f"0s{cmode_addr:04x} c4.dmx.led {btn:02x} 02 {color_mode:02x}",
            ])

        _LOGGER.info(
            "C4 LED: setting %d buttons to mode=%02x behavior=%02x color_mode=%02x (%d commands)",
            num_buttons, mode, behavior, color_mode, len(commands),
        )

        success, fail, self._c4_led_seq = await self._send_c4_commands(
            device, commands, "led_all_modes"
        )
        _LOGGER.info(
            "C4 LED: all buttons mode: %d ok, %d failed", success, fail,
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _send_c4_commands(self, device, commands, label):
        """Send C4 commands to the device, returning (ok, fail, next_seq)."""
        seq = self._c4_led_seq
        ok = fail = 0

        for cmd in commands:
            frame = _build_c4_frame(seq, cmd)
            try:
                _LOGGER.debug(
                    "C4 LED [%s]: [%02x] %s", label, seq, cmd,
                )
                await device.request(
                    profile=C4_PROFILE_BUTTON,
                    cluster=C4_CLUSTER_ID,
                    src_ep=1, dst_ep=1,
                    sequence=device.get_sequence(),
                    data=frame,
                    expect_reply=False,
                )
                ok += 1
            except Exception as e:
                _LOGGER.warning(
                    "C4 LED [%s]: [%02x] FAILED %s — %s", label, seq, cmd, e,
                )
                fail += 1

            seq = (seq + 1) & 0xFF
            await asyncio.sleep(C4_PROVISION_DELAY)

        return ok, fail, seq
