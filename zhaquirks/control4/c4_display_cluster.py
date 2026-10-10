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

import json
import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import zigpy.exceptions
import zigpy.types as t
from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.foundation import (
    BaseAttributeDefs,
    BaseCommandDefs,
    ZCLAttributeDef,
    ZCLCommandDef,
    Status as ZCLStatus,
)

# c4.ln.sl DOES accept item labels appended after the title - 000
# from the live remote - and then ignores them: the remote asks gi for the
# items 70 ms later regardless. Off, because the bytes buy nothing and they
# push the establishing frame to the edge of the ~70-byte ceiling. Kept as a
# flag rather than deleted so the negative stays visible in the code that
# would otherwise invite retrying it.
_C4_SL_CARRIES_ITEMS = False

# The c4.ln.sl title glyph is PER MODEL and comes from c4_helpers'
# `c4_list_dialect` — SR-250 takes none, SR-260 takes 0x81, both measured off a
# real director. Do not reintroduce a global default here: it silently breaks
# whichever model it does not describe.

from c4_helpers import (
    C4_DISPLAY_CLUSTER_ID,
    C4_DISPLAY_DEFAULT_ICON,
    C4_GAUGE_DEFAULT_LABEL,
    _c4_send_battery_gauge,
    _c4_send_clear_display,
    _c4_send_display_message,
    _c4_send_gauge,
    _c4_send_list_header,
    _c4_send_room_info,
    _c4_send_setting_frame,
    _c4_send_slider,
    c4_current_room,
    c4_request_beep,
    c4_setting_frame,
    sr260_room_for,
    c4_list_dialect,
)
from c4_menu_tree import MenuTree, plain_rows
from c4_settings_menu import READ_ON_OPEN, Screen, SettingsMenu

_LOGGER = logging.getLogger(__name__)


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

    class ServerCommandDefs(BaseCommandDefs):
        """Commands callable via `zha.issue_zigbee_cluster_command`."""

        # Push a menu (list) to the SR260 LCD.  `items` is pipe-separated
        # because the ZCL command schema doesn't easily express a list of
        # strings; e.g. `"Watch|Listen|Settings"`.
        show_list = ZCLCommandDef(
            id=0x00,
            schema={
                "title": t.CharacterString,
                "items": t.LongCharacterString,
                "selected_index": t.uint16_t,
            },
            is_manufacturer_specific=True,
        )

        # Dismiss the active menu (sends c4.ln.le).
        close_list = ZCLCommandDef(
            id=0x01,
            schema={},
            is_manufacturer_specific=True,
        )

        # Beep the remote until a key is pressed on it (sends c4.zr.fr 01 ff).
        # Identical on the SR-250 and SR-260, so this needs no dialect entry.
        # `beep` (0x09) is the same frame with a duration, and 0 stops it.
        find_remote = ZCLCommandDef(
            id=0x02,
            schema={},
            is_manufacturer_specific=True,
        )

        # Write the two persistent LCD rows: room title and active source.
        # This is the ONLY HA-callable path that emits `c4.ln.ri`; before it
        # existed nothing outside the quirk could touch either row, so an
        # automation had no way to clear row 2 when it turned a room off.
        # Writing `display_message: ""` is NOT a substitute - that sends
        # `c4.ln.le`, which dismisses an overlay and leaves both rows alone.
        set_room_info = ZCLCommandDef(
            id=0x03,
            schema={
                "room": t.CharacterString,
                "source": t.CharacterString,
            },
            is_manufacturer_specific=True,
        )

        # Draw the native bar-gauge overlay (sends `c4.ln.sc`) -- the volume
        # bar a real Control4 director draws. `value` is a PERCENTAGE, 0-100,
        # because the wire carries max=0x64; it is not a 0..1 fraction and it
        # is not the ZCL 0..254 level scale. It composites over rows 1/2 and
        # self-clears, so there is deliberately no matching dismiss command.
        # Raises for a model with no gauge overlay (the SR-250 has none,
        # measured) rather than sending a frame it ignores -- see
        # `_C4_LIST_DIALECT` in c4_helpers.
        show_gauge = ZCLCommandDef(
            id=0x04,
            schema={
                "value": t.uint8_t,
                "label": t.CharacterString,
            },
            is_manufacturer_specific=True,
        )

        # Queue a `c4.zr.<verb> <value>` settings write (see
        # `c4_setting_frame` for the measured verbs). `value` is hex bytes,
        # e.g. "01" or "5a". Queued, not sent: the remote is sleepy and an
        # unsolicited push fails NO_ROUTE, so it goes out on the next frame
        # the remote sends - a keypress or its periodic check-in.
        set_setting = ZCLCommandDef(
            id=0x05,
            schema={
                "verb": t.CharacterString,
                "value": t.CharacterString,
            },
            is_manufacturer_specific=True,
        )

        # Queue a `c4.zr.<verb>` read. The reply is logged at INFO by the
        # button cluster as "C4 setting reply".
        get_setting = ZCLCommandDef(
            id=0x06,
            schema={"verb": t.CharacterString},
            is_manufacturer_specific=True,
        )

        # Open the quirk-owned List > Settings menu (Config and About). The
        # quirk answers every key in it itself; nothing reaches menu_select.
        show_settings = ZCLCommandDef(
            id=0x07,
            schema={},
            is_manufacturer_specific=True,
        )

        # Open an HA-defined List menu (JSON tree, see c4_menu_tree). The quirk
        # navigates it; a leaf's action comes back as a `menu_action` event.
        # `menu` may carry "path" and "selected" to reopen where a pick was.
        show_menu = ZCLCommandDef(
            id=0x08,
            schema={"menu": t.LongCharacterString},
            is_manufacturer_specific=True,
        )

        # Beep for `seconds` (1-254), 255 until a key is pressed, 0 to stop:
        # Composer's Beep and Stop Beep. A separate id so find_remote keeps
        # its empty schema for the automations that call it.
        beep = ZCLCommandDef(
            id=0x09,
            schema={"seconds": t.uint8_t},
            is_manufacturer_specific=True,
        )

    # ------------------------------------------------------------------
    # Per-device active-menu state
    # ------------------------------------------------------------------
    # `_active_menu` is None when no menu is on screen; otherwise a dict:
    #   {"list_id": int, "title": str, "items": [str, ...],
    #    "selected_index": int}
    # Set by show_list, read by C4RemoteButtonCluster's gi-handler when the
    # remote pages through the items, and cleared by close_list / by a
    # Select-on-list event.
    _active_menu: dict | None = None
    _next_list_id: int = 0  # rolls 2..0xFFFF; see _next_list_id_value

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

    # ------------------------------------------------------------------
    # show_list / close_list — menu commands (called by ZHA via service)
    # ------------------------------------------------------------------

    def _next_list_id_value(self) -> int:
        """Return a 16-bit list id, never 0 or 1 (`0` means "no list").

        1 is skipped: on the SR-260, list 0x0001 draws its first row centered
        instead of left-aligned. It surfaces as "the first list after an HA
        restart is indented", because the counter restarts at 1. No director
        capture has ever used id 1.
        """
        nxt = (self._next_list_id + 1) & 0xFFFF
        nxt = max(nxt, 2)
        # Use a writable instance attribute (the class attribute is just the
        # initial value; subsequent writes shadow it on the instance).
        self._next_list_id = nxt
        return nxt

    async def show_list(self, title, items, selected_index):
        """Push a menu / list to the SR260 LCD.

        `items` is a pipe-separated string ("Watch|Listen|Settings") since
        ZCL command schemas don't natively carry a list of strings.  Empty
        entries between pipes are kept (so `"|Watch||Listen"` is a 4-entry
        list with two blanks).

        After this call the cluster owns an "active menu" and answers the
        remote's `c4.ln.gi` page requests with the cached items.  When the
        user moves the cursor and presses Select, `C4RemoteButtonCluster`
        fires a `menu_select` zha_event with the chosen item and clears
        the menu.

        Calling `show_list` again replaces the previous menu.  Calling
        `close_list` (or pressing any list-dismissing key) clears it.
        """
        items_list: list[str] = []
        if items is not None:
            items_list = [s for s in str(items).split("|")]
        if not items_list:
            raise ValueError("show_list: items must contain at least one entry")

        try:
            sel = int(selected_index or 0)
        except (TypeError, ValueError):
            sel = 0
        if sel < 0:
            sel = 0
        if sel >= len(items_list):
            sel = len(items_list) - 1

        list_id = self._next_list_id_value()
        title_str = str(title or "")

        self._active_menu = {
            "list_id": list_id,
            "title": title_str,
            "items": items_list,
            "selected_index": sel,
        }

        device = self.endpoint.device
        try:
            await _c4_send_list_header(
                device,
                list_id=list_id,
                count=len(items_list),
                sel_idx=sel,
                title=title_str,
                icon=c4_list_dialect(device)["title_icon"],
                items=items_list if _C4_SL_CARRIES_ITEMS else None,
            )
            _LOGGER.info(
                "C4 display [%s]: show_list id=0x%04X items=%d sel=%d title=%r",
                device.ieee, list_id, len(items_list), sel, title_str,
            )
        except zigpy.exceptions.DeliveryError as e:
            # Sleepy-device race — the SR260 only listens when polling.  If
            # the device is asleep when we try to push the menu, bellows
            # times out waiting for the APS ack.  Drop the menu state and
            # warn, but don't propagate: the caller (an HA automation /
            # service call) shouldn't see a hard error for what is in
            # practice a "user wasn't holding the remote" condition.
            _LOGGER.warning(
                "C4 display [%s]: show_list undelivered (%s) — "
                "remote likely asleep; press a key on the remote and "
                "retry while it's awake",
                device.ieee, e,
            )
            self._active_menu = None
        except Exception:
            _LOGGER.warning(
                "C4 display: show_list send failed", exc_info=True,
            )
            self._active_menu = None
            raise

    async def find_remote(self):
        """Beep the remote until a key is pressed on it (`c4.zr.fr 01 ff`)."""
        await self.beep(0xFF)

    async def beep(self, seconds):
        """Beep for `seconds`, 0xFF until a key press, 0 to stop (`c4.zr.fr`).

        A remote that is asleep - the normal case for one that is lost - gets
        the beep after its next check-in instead; see `c4_request_beep`.
        """
        device = self.endpoint.device
        try:
            if await c4_request_beep(device, int(seconds)):
                _LOGGER.info("C4 display [%s]: beep %s", device.ieee, seconds)
        except Exception:
            _LOGGER.warning("C4 display: beep send failed", exc_info=True)

    async def set_room_info(self, room, source):
        """Write the two persistent LCD rows (`c4.ln.ri`).

        Row 1 is the room title, row 2 the active source. Pass `source=""` to
        show the room as off - that is what a real director sends, and it is
        the only way to clear row 2, since `c4.ln.le` dismisses overlays and
        does not touch the rows.

        An empty `room` falls back to whatever was last cached for this device
        rather than blanking row 1, because `_c4_send_room_info` refuses to
        cache an empty room and a caller that omits it almost always means
        "leave the room alone, I only care about the source".
        """
        device = self.endpoint.device
        room_s = room or c4_current_room(device) or sr260_room_for(device, "")
        if not room_s:
            # The room is only learned at bootstrap, so a dashboard route soon
            # after an HA restart has none. Sending would blank row 1; caching
            # the source lets the next bootstrap draw both rows instead.
            device._c4_source = source or ""
            _LOGGER.info(
                "C4 display [%s]: set_room_info deferred to bootstrap, "
                "no room known yet (source=%r)", device.ieee, source or "",
            )
            return
        try:
            await _c4_send_room_info(device, room_s, source or "")
            _LOGGER.info(
                "C4 display [%s]: set_room_info room=%r source=%r",
                device.ieee, room_s, source or "",
            )
        except zigpy.exceptions.DeliveryError as e:
            # Same reasoning as find_remote: a sleeping remote is ordinary,
            # and an automation that turned a room off must not report a
            # failure because the LCD update did not land.
            _LOGGER.warning(
                "C4 display [%s]: set_room_info undelivered (%s) - "
                "remote likely asleep",
                device.ieee, e,
            )
        except Exception:
            _LOGGER.warning(
                "C4 display: set_room_info send failed", exc_info=True,
            )

    async def show_gauge(self, value, label):
        """Draw the native bar-gauge overlay (`c4.ln.sc`).

        `value` is a percentage, 0-100 -- see the command definition. An empty
        `label` falls back to "Volume", which is what the director sends.

        Deliberately does NOT push `c4.ln.le` afterwards: `sc` self-clears, the
        director sends no dismissal, and `le` would tear down an open list.
        """
        device = self.endpoint.device
        try:
            await _c4_send_gauge(device, value, label or C4_GAUGE_DEFAULT_LABEL)
            _LOGGER.info(
                "C4 display [%s]: show_gauge value=%s label=%r",
                device.ieee, value, label or C4_GAUGE_DEFAULT_LABEL,
            )
        except ValueError as e:
            # An unmeasured model. Loud, because this one IS a real
            # misconfiguration rather than a sleepy radio -- but still not
            # raised, so a volume automation never fails on its LCD step.
            _LOGGER.warning("C4 display [%s]: show_gauge - %s", device.ieee, e)
        except zigpy.exceptions.DeliveryError as e:
            # Same reasoning as find_remote / set_room_info: a sleeping remote
            # is ordinary. It matters more here than anywhere else, because
            # this fires on every volume tick and must never turn a volume
            # press into a failed automation.
            _LOGGER.debug(
                "C4 display [%s]: show_gauge undelivered (%s) - "
                "remote likely asleep",
                device.ieee, e,
            )
        except Exception:
            _LOGGER.warning(
                "C4 display: show_gauge send failed", exc_info=True,
            )

    async def close_list(self):
        """Dismiss the active menu (sends `c4.ln.le`)."""
        device = self.endpoint.device
        had_menu = self._active_menu is not None
        self._active_menu = None
        try:
            await _c4_send_clear_display(device)
            if had_menu:
                _LOGGER.info("C4 display [%s]: close_list", device.ieee)
        except zigpy.exceptions.DeliveryError as e:
            _LOGGER.warning(
                "C4 display [%s]: close_list undelivered (%s) — "
                "remote likely asleep",
                device.ieee, e,
            )
        except Exception:
            _LOGGER.warning(
                "C4 display: close_list send failed", exc_info=True,
            )

    # ------------------------------------------------------------------
    # c4.zr.* settings - queued until the remote is awake
    # ------------------------------------------------------------------

    def _queue_setting(self, verb, value, ns="zr"):
        device = self.endpoint.device
        try:
            seq, text = c4_setting_frame(device, verb, value, ns)
        except ValueError as e:
            _LOGGER.warning("C4 display [%s]: %s", device.ieee, e)
            return
        if not hasattr(device, "_c4_pending_settings"):
            device._c4_pending_settings = []
            device._c4_setting_seqs = set()
        device._c4_pending_settings.append(text)
        device._c4_setting_seqs.add(f"{seq:04x}")
        _LOGGER.info(
            "C4 display [%s]: queued %r until the remote wakes",
            device.ieee, text,
        )

    async def set_setting(self, verb, value):
        """Queue a `c4.zr` setting write for the remote's next wake."""
        self._queue_setting(verb, value)

    async def get_setting(self, verb):
        """Queue a `c4.zr` setting read for the remote's next wake."""
        self._queue_setting(verb, None)

    async def flush_pending_settings(self):
        """Send every queued settings frame, in order. Called on wake."""
        device = self.endpoint.device
        pending = getattr(device, "_c4_pending_settings", None)
        # Every inbound frame schedules a flush, so they can overlap.
        if not pending or getattr(device, "_c4_settings_flushing", False):
            return
        device._c4_settings_flushing = True
        try:
            await self._flush_settings(device, pending)
        finally:
            device._c4_settings_flushing = False

    async def _flush_settings(self, device, pending):
        while pending:
            text = pending[0]
            try:
                await _c4_send_setting_frame(device, text)
            except Exception as e:
                # Leave it queued for the next wake rather than dropping it.
                _LOGGER.warning(
                    "C4 display [%s]: setting %r undelivered (%s); "
                    "will retry on next wake",
                    device.ieee, text, e,
                )
                return
            pending.pop(0)

    # ------------------------------------------------------------------
    # List > Settings - drawn and answered entirely by the quirk
    # ------------------------------------------------------------------
    # `_active_menu["owner"] == "settings"` routes the remote's is / lb / cn /
    # cs frames here instead of to menu_select (see C4RemoteButtonCluster).
    # While a slider or gauge is up, `_settings_overlay` holds its label so
    # whichever key closes it returns to the list it was opened from.

    def _new_settings_menu(self):
        device = self.endpoint.device
        if not hasattr(device, "_c4_settings"):
            device._c4_settings = {}
        return SettingsMenu(
            device._c4_settings,
            about=lambda: self._about_rows(device),
            battery=lambda: self._battery_percent(device),
        )

    async def _refresh_settings_cache(self):
        # List was just pressed, so the remote is listening: refresh the
        # check marks now, before the user reaches a submenu.
        for verb in READ_ON_OPEN:
            self._queue_setting(verb, None)
        self._queue_setting("fwv", None, ns="sy")
        await self.flush_pending_settings()

    async def show_settings(self):
        """Open the quirk-owned Settings menu on the remote."""
        menu = self._new_settings_menu()
        self._settings_menu = menu
        self._settings_overlay = None
        await self._draw_settings(menu.open())
        await self._refresh_settings_cache()

    async def show_menu(self, menu):
        """Push an HA-supplied menu tree (JSON) to the remote."""
        device = self.endpoint.device
        text = str(menu or "{}")
        # "json:" stops HA's template engine re-parsing the tree into a mapping.
        if text.startswith("json:"):
            text = text[5:]
        try:
            tree = json.loads(text)
        except ValueError as e:
            _LOGGER.warning("C4 display [%s]: show_menu bad JSON (%s)", device.ieee, e)
            return
        if not isinstance(tree, dict):
            _LOGGER.warning("C4 display [%s]: show_menu wants an object", device.ieee)
            return
        engine = MenuTree(tree, self._new_settings_menu)
        self._settings_menu = engine
        self._settings_overlay = None
        await self._draw_settings(engine.open(tree.get("path"), tree.get("selected")))
        # The cache feeds only the Settings subtree, whose reads are SR-260
        # verbs, so a tree without one (the SR-250's) reads nothing.
        if not tree.get("path") and any(
            isinstance(c, dict) and c.get("settings") for c in tree.get("items") or []
        ):
            await self._refresh_settings_cache()

    @staticmethod
    def _battery_percent(device):
        """`c4.zr.bl` (what the director's gauge shows), else the status report."""
        try:
            return int(getattr(device, "_c4_settings", {})["bl"], 16)
        except (KeyError, TypeError, ValueError):
            return getattr(device, "_c4_battery", None)

    @staticmethod
    def _about_rows(device):
        ieee = str(getattr(device, "ieee", "")).replace(":", "")
        firmware = getattr(device, "_c4_settings", {}).get("fwv") or "unknown"
        return [
            ("Controller:", "Home Assistant"),
            ("Firmware version:", firmware),
            ("MAC address:", ieee),
        ]

    async def settings_event(self, kind, list_id, index):
        """Handle a key the remote reported while a quirk-owned menu was up.

        Returns the picked leaf as a Screen of kind "action", for the button
        cluster to fire as `menu_action`; otherwise None.
        """
        menu = getattr(self, "_settings_menu", None)
        if menu is None:
            return None
        overlay = getattr(self, "_settings_overlay", None)
        if overlay is not None:
            # cs (Select), cn (Cancel) or lb (Back) all leave a slider or gauge.
            if kind in ("cs", "cn", "lb"):
                self._settings_overlay = None
                await self._draw_settings(menu.resume(overlay))
            return
        active = self._active_menu or {}
        if list_id != active.get("list_id"):
            return  # a late frame for a list we have already replaced
        if kind == "is":
            screen = menu.select(index)
        elif kind == "lb":
            screen = menu.back()
        elif kind == "cn":
            screen = Screen("close")
        else:
            return None
        if screen is not None and screen.kind == "action":
            # The director leaves the list up after a leaf runs; HA redraws it.
            return screen
        if screen is not None:
            await self._draw_settings(screen)
        return None

    async def _draw_settings(self, screen):
        device = self.endpoint.device
        try:
            if screen.write is not None:
                verb, value = screen.write
                self._queue_setting(verb, value)
                self._queue_setting(verb, None)  # read back: the remote may refuse
                await self.flush_pending_settings()
            if screen.kind == "list":
                list_id = self._next_list_id_value()
                self._active_menu = {
                    "list_id": list_id,
                    "title": screen.title,
                    "items": plain_rows(screen.items)
                    if c4_list_dialect(device)["item_icon"] is None else screen.items,
                    "selected_index": screen.selected,
                    "owner": "settings",
                }
                await _c4_send_list_header(
                    device,
                    list_id=list_id,
                    count=len(screen.items),
                    sel_idx=screen.selected,
                    title=screen.title,
                    icon=c4_list_dialect(device)["title_icon"],
                )
            elif screen.kind == "slider":
                self._settings_overlay = screen.title
                await _c4_send_slider(device, screen.slider_id, screen.title)
            elif screen.kind == "gauge":
                self._settings_overlay = screen.title
                await _c4_send_battery_gauge(device, screen.value)
            else:
                self._active_menu = None
                self._settings_menu = None
                self._settings_overlay = None
                await _c4_send_clear_display(device)
            _LOGGER.info(
                "C4 display [%s]: settings %s %r", device.ieee, screen.kind,
                screen.title,
            )
        except Exception as e:
            _LOGGER.warning(
                "C4 display [%s]: settings %s undelivered (%s)",
                device.ieee, screen.kind, e,
            )

