"""C4SR260DisplayCluster — writable LCD-message attribute for the SR260 remote.

Exposes a single ZHA-side attribute (cluster 0xFC47, attribute 0x0000,
CharacterString, RW) named `display_message`.  Writing to this attribute
sends `c4.ln.dm <icon> "<message>"` to the remote's LCD; writing an empty
string sends `c4.ln.le` to dismiss any active splash / menu.

NOTE — ZHA has no `text` platform, so this attribute does NOT surface as
a text-input entity on the device card in HA.  No quirk-side change will
produce one.  Users should bridge an `input_text` helper to the service
call below via an automation if they want a dashboard text input.

How to write the attribute from Home Assistant:

  service: zha.set_zigbee_cluster_attribute
  data:
    ieee: "00:0f:ff:XX:XX:XX:XX:XX"
    endpoint_id: 1
    cluster_id: 0xFC47
    cluster_type: in
    attribute: 0
    value: "Hello from HA"

Cluster ID 0xFC47 is in the ZHA "manufacturer-specific" range (0xFC00..0xFFFF)
and isn't used by any other Control4 quirk in this codebase.
"""

import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    ZCLAttributeDef,
    Status as ZCLStatus,
)

from c4_helpers import (
    C4_DISPLAY_DEFAULT_ICON,
    _c4_send_clear_display,
    _c4_send_display_message,
)

_LOGGER = logging.getLogger(__name__)

C4_DISPLAY_CLUSTER_ID = 0xFC47


class C4SR260DisplayCluster(CustomCluster):
    """Writable LCD-message attribute for the C4-SR260 remote.

    Writing `display_message` (attr 0x0000) translates into a
    `c4.ln.dm` command pushed to the remote.  Empty string → `c4.ln.le`.

    The icon byte sent with `c4.ln.dm` is taken from the cached value of
    attribute `display_icon` (0x0001, u8); if unset, it defaults to
    C4_DISPLAY_DEFAULT_ICON (0x5A) to match what the official Control4
    controller uses for transient splashes.
    """

    cluster_id   = C4_DISPLAY_CLUSTER_ID
    name         = "Control4 SR260 Display"
    ep_attribute = "c4_sr260_display"

    class AttributeDefs(BaseAttributeDefs):
        display_message = ZCLAttributeDef(
            id=0x0000,
            type=t.CharacterString,
            access="rw",
            is_manufacturer_specific=False,
        )
        display_icon = ZCLAttributeDef(
            id=0x0001,
            type=t.uint8_t,
            access="rw",
            is_manufacturer_specific=False,
        )

    # ------------------------------------------------------------------
    # Startup — seed default + push current value to the LCD
    # ------------------------------------------------------------------

    async def async_initialize(self, from_cache=False):
        """On every HA / ZHA startup, push the cached display message to the LCD.

        On first start (cache empty), seed the cache with the zigpy device
        model name (e.g. ``"C4-SR260"``) so the LCD shows a sensible label
        out of the box.  The user can override at any time by writing
        ``display_message`` — that override persists in ZHA's attribute
        cache and is what gets pushed on the next start.

        Failures are logged at WARNING and swallowed: the SR260 is a sleepy
        end-device, so the very first push may race the device's first
        wake-up. The user just sees no LCD update that one boot.
        """
        msg_id  = self.AttributeDefs.display_message.id
        icon_id = self.AttributeDefs.display_icon.id

        cached = self._attr_cache.get(msg_id)
        if not isinstance(cached, str) or not cached:
            device = self.endpoint.device
            cached = getattr(device, "model", None) or "Remote"
            self._update_attribute(msg_id, cached)
            _LOGGER.info(
                "C4 display [%s]: seeded default message %r",
                device.ieee, cached,
            )

        icon = self._attr_cache.get(icon_id, C4_DISPLAY_DEFAULT_ICON)
        try:
            icon = int(icon) & 0xFF
        except (TypeError, ValueError):
            icon = C4_DISPLAY_DEFAULT_ICON

        try:
            await _c4_send_display_message(
                self.endpoint.device, cached, icon=icon,
            )
            _LOGGER.info(
                "C4 display [%s]: startup push %r (icon=0x%02X)",
                self.endpoint.device.ieee, cached, icon,
            )
        except Exception as e:
            _LOGGER.warning(
                "C4 display [%s]: startup push failed — %s",
                self.endpoint.device.ieee, e,
            )

        await super().async_initialize(from_cache=from_cache)

    # ------------------------------------------------------------------
    # Attribute write — translate to c4.ln.dm / c4.ln.le
    # ------------------------------------------------------------------

    async def write_attributes(self, attributes, manufacturer=None):
        """Push `display_message` to the LCD; cache `display_icon` locally."""
        device = self.endpoint.device
        msg_id  = self.AttributeDefs.display_message.id
        icon_id = self.AttributeDefs.display_icon.id

        # Resolve attribute names → ids and build a unified dict.
        resolved: dict[int, object] = {}
        for attr, value in attributes.items():
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    _LOGGER.debug(
                        "C4 display: ignoring unknown attribute %r", attr,
                    )
                    continue
            else:
                attr_id = int(attr)
            resolved[attr_id] = value

        # Cache the icon write first so a paired (icon, message) write in the
        # same call uses the new icon for the message.
        if icon_id in resolved:
            try:
                icon_val = int(resolved[icon_id])
            except (TypeError, ValueError):
                icon_val = C4_DISPLAY_DEFAULT_ICON
            self._update_attribute(icon_id, icon_val & 0xFF)
            _LOGGER.debug(
                "C4 display: icon = 0x%02X (cached)", icon_val & 0xFF,
            )

        if msg_id in resolved:
            message = resolved[msg_id]
            if message is None:
                message = ""
            message = str(message)

            # Look up icon from the cache; fall back to the default.
            icon = self._attr_cache.get(icon_id, C4_DISPLAY_DEFAULT_ICON)
            try:
                icon = int(icon) & 0xFF
            except (TypeError, ValueError):
                icon = C4_DISPLAY_DEFAULT_ICON

            try:
                if message == "":
                    await _c4_send_clear_display(device)
                    _LOGGER.info(
                        "C4 display [%s]: cleared (c4.ln.le)", device.ieee,
                    )
                else:
                    await _c4_send_display_message(device, message, icon=icon)
                    _LOGGER.info(
                        "C4 display [%s]: %r (icon=0x%02X)",
                        device.ieee, message, icon,
                    )
            except Exception as e:
                _LOGGER.warning(
                    "C4 display: write failed — %s", e, exc_info=True,
                )
                return [
                    [foundation.WriteAttributesStatusRecord(
                        ZCLStatus.FAILURE, attrid=msg_id,
                    )]
                ]

            # Cache the value so HA's read-after-write returns the right thing.
            self._update_attribute(msg_id, message)

        # Standard one-record SUCCESS response shape.
        return [[foundation.WriteAttributesStatusRecord(ZCLStatus.SUCCESS)]]

    # ------------------------------------------------------------------
    # Attribute read — return the cached value (no device round-trip)
    # ------------------------------------------------------------------

    async def read_attributes(
        self, attributes, allow_cache=False, only_cache=False, manufacturer=None,
    ):
        """Return cached values — there is no readback for the LCD message.

        The remote does not echo back the displayed text, so cached state is
        the only ground truth available.
        """
        success: dict = {}
        failure: dict = {}

        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    failure[attr] = foundation.Status.UNSUP_ATTRIBUTE
                    continue
                key = attr
            else:
                attr_id = int(attr)
                key = attr_id

            cached = self._attr_cache.get(attr_id)
            if cached is not None:
                success[key] = cached
            elif attr_id == self.AttributeDefs.display_icon.id:
                # Surface the default icon when nothing's been cached yet.
                success[key] = C4_DISPLAY_DEFAULT_ICON
            else:
                # display_message defaults to an empty string when unread.
                success[key] = ""

        return success, failure
