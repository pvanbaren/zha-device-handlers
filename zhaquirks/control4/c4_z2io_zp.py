"""
Control4 C4-Z2IO-ZP IO / Garage Door Module handler.

Protocol documented from Wireshark captures:
  - provisioning capture (original)
  - open/close operation capture (new)

  APS:  profile=0xC25C  cluster=0x0001  endpoint=197 (0xC5)
  Payload: raw ASCII — NO ZCL framing

Command formats
───────────────
  Coordinator → Device:
    0s[4hex-seq] [prop] [args...]\r\n    ← set
    0g[4hex-seq] [prop] [args...]\r\n   ← get (args optional)

  Device → Coordinator:
    0r[4hex-seq] 000 [prop] [val]\r\n   ← success response (prop/val may be absent)
    0r[4hex-seq] n01 [prop]\r\n         ← not-available response (e.g. analog inputs)
    0t[4hex-seq] sa [prop] [val]\r\n    ← unsolicited announce

  NOTE: rlc/rlo responses are short: "0r[seq] 000" with NO property echoed.

Full property table
───────────────────
  c4.z2x.cts    R      hex bitmask   Contact status (bit N = input N+1)
  c4.z2x.rls    R      hex bitmask   Relay status   (bit N = relay N+1) — READ only
  c4.z2x.rlc N  W      decimal ch    Relay latch Close (energize relay N)  ← from capture
  c4.z2x.rlo N  W      decimal ch    Relay latch Open  (de-energize relay N) ← from capture
  c4.z2x.opt    R/W    decimal       IO mode:
                                        1 = 2 relays (SPST)
                                        2 = 4 contacts (binary sensors)
                                        3 = 1 relay (SPST) + 2 contacts
                                        4 = 1 relay (SPDT)
                                        5 = 1 relay (DPST)
  c4.z2x.ctd    R/W    ch ms-hex     Contact debounce (ch, duration in hex ms)
  c4.z2x.rlp N  R/W    ch ticks      Relay pulse duration for relay N
                          observed: ch=01 ticks=02 and ch=02 ticks=02
  c4.z2x.ana0   R      (n/a)         Analog input 0 — returns n01 (not supported)
  c4.z2x.ana1   R      (n/a)         Analog input 1 — returns n01
  c4.z2x.ldm    R      hex           Link density metric (diagnostic, read-only)
  c4.z2x.zmac   R      str           Zigbee MAC address
  c4.z2x.zpid   R      str           Zigbee PAN ID
  c4.z2x.zepid  R      str           Zigbee Extended PAN ID
  c4.sy.fwv     R      str           Firmware version (observed: 1.2.0)
  c4.sy.blv     R      decimal       Bootloader version (observed: 3)
  c4.z2x.tmpi   R/annc hex           Internal temperature — centikelvins; °C = (raw × 0.01) − 273.15
  c4.z2x.tmpe   R/annc hex           External probe temperature — same encoding;
                                      0x5b13 (233.15 K = −40 °C) is the sentinel for probe not connected
  c4.z2x.thumi  R/annc hex           Relative humidity — raw / 100 = % RH;
                                      announced unsolicited ~every 5 min, also gettable

Relay actuation protocol (from open/close capture)
───────────────────────────────────────────────────
  The actual Control4 controller does NOT use opt=1 + rls bitmask for toggling.
  Instead it uses an explicit two-step sequence:

    1. 0s[seq] c4.z2x.rlc N        (energize relay N)
       → device announces: 0t[seq] sa c4.z2x.rls 0N
       → device responds:  0r[seq] 000          ← SHORT form, no property echoed
    2. <wait ~500 ms>
    3. 0s[seq] c4.z2x.rlo N        (de-energize relay N)
       → device announces: 0t[seq] sa c4.z2x.rls 00
       → device responds:  0r[seq] 000

  Observed pulse widths: ~440–500 ms (relay 1 and relay 2 both).
  opt=1 is still set during provisioning as originally observed.

Hardware note: 2 relay outputs, 5-bit contact input bitmask (5 inputs).
Unconnected inputs float high via pull-ups → cts=0x1f when nothing wired.

Contact wiring
──────────────
  NC (default / security):  bit=0 → CLOSED   bit=1 → OPEN
  NO:                       bit=1 → CLOSED   bit=0 → OPEN
  Set CONTACT_BIT_CLOSED[N] = True to invert channel N.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# ── APS constants ──────────────────────────────────────────────────────────────
C4_PROFILE  = 0xC25C
C4_CLUSTER  = 0x0001
C4_ENDPOINT = 197          # 0xC5

# ── C4 property names ─────────────────────────────────────────────────────────
_PROP_CTS   = "c4.z2x.cts"
_PROP_RLS   = "c4.z2x.rls"
_PROP_RLC   = "c4.z2x.rlc"   # relay latch close  (energize)
_PROP_RLO   = "c4.z2x.rlo"   # relay latch open   (de-energize)
_PROP_OPT   = "c4.z2x.opt"
_PROP_CTD   = "c4.z2x.ctd"
_PROP_RLP   = "c4.z2x.rlp"
_PROP_ANA0  = "c4.z2x.ana0"
_PROP_ANA1  = "c4.z2x.ana1"
_PROP_LDM   = "c4.z2x.ldm"
_PROP_ZMAC  = "c4.z2x.zmac"
_PROP_ZPID  = "c4.z2x.zpid"
_PROP_ZEPID = "c4.z2x.zepid"
_PROP_FWV   = "c4.sy.fwv"
_PROP_BLV   = "c4.sy.blv"
_PROP_ZPC   = "c4.sy.zpc"
_PROP_ZNID  = "c4.z2x.znid"
_PROP_ZCHAN = "c4.z2x.zchan"
_PROP_TMPI  = "c4.z2x.tmpi"
_PROP_TMPE  = "c4.z2x.tmpe"
_PROP_THUMI = "c4.z2x.thumi"

# ── Hardware limits ────────────────────────────────────────────────────────────
NUM_RELAYS_HW   = 2   # physical relay outputs on the board
NUM_CONTACTS_HW = 5   # physical contact input bits (0-4 of cts bitmask)

# ── IO mode (c4.z2x.opt) ────────────────────────────────────────────────────
# Configurable options for contacts and relays:
#   opt=1  →  2 relays (SPST), 0 contacts
#   opt=2  →  0 relays, 4 contacts
#   opt=3  →  1 relay (SPST), 2 contacts
#   opt=4  →  1 relay (SPDT), 0 contacts
#   opt=5  →  1 relay (DPST), 0 contacts
#
# Each mode is described by (num_relays, num_contacts).
OPT_MODE_RELAYS_2      = 1   # 2 relays (SPST)
OPT_MODE_CONTACTS_4    = 2   # 4 contacts
OPT_MODE_RELAY_1_CTS_2 = 3   # 1 relay (SPST) + 2 contacts
OPT_MODE_RELAY_SPDT    = 4   # 1 relay (SPDT)
OPT_MODE_RELAY_DPST    = 5   # 1 relay (DPST)

OPT_MODES: dict[int, tuple[int, int]] = {
    #  opt: (relays, contacts)
    OPT_MODE_RELAYS_2:      (2, 0),
    OPT_MODE_CONTACTS_4:    (0, 4),
    OPT_MODE_RELAY_1_CTS_2: (1, 2),
    OPT_MODE_RELAY_SPDT:    (1, 0),
    OPT_MODE_RELAY_DPST:    (1, 0),
}
DEFAULT_OPT_MODE = OPT_MODE_CONTACTS_4

# ── HA events ─────────────────────────────────────────────────────────────────
ZHA_EVENT       = "zha_event"
C4_DEVICE_TYPE  = "C4-Z2IO-ZP"

# ── Wiring polarity (per contact channel, 0-indexed) ─────────────────────────
# False = NC (bit=1 → OPEN)   True = NO (bit=1 → CLOSED)
CONTACT_BIT_CLOSED: dict[int, bool] = {
    0: True,   # contact input 1 — NO wiring (bit=1 → CLOSED)
    1: True,
    2: True,
    3: True,
    4: True,
}

# ── Door state constants ───────────────────────────────────────────────────────
DOOR_OPEN    = "open"
DOOR_CLOSED  = "closed"
DOOR_UNKNOWN = "unknown"

# ── Relay pulse duration (ms) — observed ~440-500 ms in capture ───────────────
DEFAULT_PULSE_MS = 500

# ── Sensor encoding ───────────────────────────────────────────────────────────
# Temperatures are reported as centikelvins: °C = (raw × 0.01) − 273.15
# ZCL TemperatureMeasurement measured_value unit is 0.01 °C, so:
#   ZCL value = raw − 27315
# Humidity: raw / 100 = % RH; ZCL RelativeHumidity unit is 0.01% RH, so:
#   ZCL value = raw  (no conversion needed)
TEMP_NO_PROBE_RAW = 0x5b13   # 233.15 K = −40 °C — sentinel for disconnected probe


class C4Z2IOZPHandler:
    """Per-device handler for a Control4 C4-Z2IO-ZP IO / garage door module.

    Lifecycle
    ─────────
    • Instantiated by zha_c4_patch when the device model is confirmed.
    • configure_device() called once on first join or HA restart.
    • handle_packet() called for every frame from this device.
    • trigger_relay(N) / poll_state() called by services / automations.

    IO modes (c4.z2x.opt)
    ─────────────────────
    The device supports three configurable IO modes, selected by the ``opt``
    parameter passed to __init__ (and written to the device during provisioning):

      opt=1  →  2 relays (SPST), 0 contacts
      opt=2  →  0 relays, 4 contacts (binary sensors)
      opt=3  →  1 relay (SPST), 2 contacts
      opt=4  →  1 relay (SPDT), 0 contacts
      opt=5  →  1 relay (DPST), 0 contacts

    The number of relay and contact entities exposed is determined by the mode.
    ``num_relays`` and ``num_contacts`` are set at init time accordingly.

    Relay outputs
    ─────────────
    Actuation uses an explicit rlc/rlo sequence (from open/close capture):
      1. Send  c4.z2x.rlc N  — energize relay N
      2. Wait  DEFAULT_PULSE_MS
      3. Send  c4.z2x.rlo N  — de-energize relay N
    The device announces rls changes for both steps.
    Only relay channels within the mode's relay count are accepted.

    Contact inputs
    ──────────────
    Bits 0..N-1 of the cts bitmask, where N = num_contacts for the mode.
    Unconnected inputs float high via pull-ups.
    Default wiring is NC; flip CONTACT_BIT_CLOSED[N] for NO sensors.
    """

    MODEL = "C4-Z2IO-ZP"

    def __init__(
        self,
        ieee: str,
        device,       # zigpy.device.Device
        hass,         # homeassistant.core.HomeAssistant
        app,          # ZHA application (zigpy AppBase)
        opt_mode: int = DEFAULT_OPT_MODE,
    ) -> None:
        self.ieee   = ieee
        self._dev   = device
        self._hass  = hass
        self._app   = app

        # IO mode — determines how many relays/contacts are active
        if opt_mode not in OPT_MODES:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: unknown opt_mode %d, falling back to %d",
                ieee, opt_mode, DEFAULT_OPT_MODE,
            )
            opt_mode = DEFAULT_OPT_MODE
        self.opt_mode: int = opt_mode
        self.num_relays, self.num_contacts = OPT_MODES[opt_mode]
        _LOGGER.info(
            "C4-Z2IO-ZP [%s]: opt_mode=%d → %d relay(s), %d contact(s)",
            ieee, opt_mode, self.num_relays, self.num_contacts,
        )

        self._seq: int = 0x0040

        # Pending response futures keyed by seq
        self._pending: dict[int, asyncio.Future] = {}

        # Contact state per channel (0-indexed), relay state per channel
        self._contact_state: list[str]  = [DOOR_UNKNOWN] * self.num_contacts
        self._relay_on:      list[bool] = [False] * self.num_relays
        self._cts_raw: int = 0
        self._rls_raw: int = 0

        # Read-only device info (populated after configure_device)
        self.fw_version:   str | None = None
        self.bl_version:   str | None = None
        self.zigbee_mac:   str | None = None
        self.zigbee_pid:   str | None = None
        self.zigbee_epid:  str | None = None
        self.zigbee_nid:   str | None = None
        self.zigbee_chan:  int | None = None
        self.link_density: str | None = None

        # Sensor state: raw integer values as received from the device, or None
        # before the first reading arrives.  Conversion formulas:
        #   °C  = (raw × 0.01) − 273.15   →  ZCL = raw − 27315
        #   %RH = raw / 100                →  ZCL = raw
        self._temp_internal_raw: int | None = None   # c4.z2x.tmpi
        self._temp_external_raw: int | None = None   # c4.z2x.tmpe
        self._humidity_raw:      int | None = None   # c4.z2x.thumi

    # ── Sequence counter ──────────────────────────────────────────────────────

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    # ── Frame builders ────────────────────────────────────────────────────────

    def _get_frame(self, prop: str, *args: str) -> tuple[int, bytes]:
        seq = self._next_seq()
        parts = [f"0g{seq:04x}", prop, *args]
        return seq, (" ".join(parts) + "\r\n").encode("ascii")

    def _set_frame(self, prop: str, *args: str) -> tuple[int, bytes]:
        seq = self._next_seq()
        parts = [f"0s{seq:04x}", prop, *args]
        return seq, (" ".join(parts) + "\r\n").encode("ascii")

    # ── Frame transmission ────────────────────────────────────────────────────

    def _controller_ready(self) -> bool:
        """Return False if the zigpy ApplicationController is known to be down."""
        is_running = getattr(self._app, "is_running", None)
        if is_running is not None:
            return bool(is_running)
        # Fallback: treat as ready if the attribute doesn't exist
        return True

    async def _send(
        self,
        seq: int,
        data: bytes,
        await_response: bool = True,
        timeout: float = 5.0,
    ) -> dict | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future | None = None

        # Pre-flight: skip the send if the radio controller isn't running.
        # This avoids cascading error logs when ZHA is starting or the
        # coordinator is reconnecting — the command simply can't be delivered.
        if not self._controller_ready():
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: skipping TX (seq=0x%04x) — "
                "Zigbee controller is not running",
                self.ieee, seq,
            )
            self._pending.pop(seq, None)
            return None

        if await_response:
            future = loop.create_future()
            self._pending[seq] = future

        try:
            await self._app.request(
                device=self._dev,
                profile=C4_PROFILE,
                cluster=C4_CLUSTER,
                src_ep=C4_ENDPOINT,
                dst_ep=C4_ENDPOINT,
                sequence=seq & 0xFF,
                data=data,
                expect_reply=False,
            )
        except Exception as exc:
            exc_str = str(exc)
            if "not running" in exc_str.lower():
                _LOGGER.warning(
                    "C4-Z2IO-ZP [%s]: TX skipped (seq=0x%04x) — "
                    "Zigbee controller not running: %s",
                    self.ieee, seq, exc,
                )
            else:
                _LOGGER.error("C4-Z2IO-ZP [%s]: TX error: %s", self.ieee, exc)
            self._pending.pop(seq, None)
            return None

        if future is None:
            return None

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: timeout for seq 0x%04x", self.ieee, seq
            )
            self._pending.pop(seq, None)
            return None

    # ── Incoming packet dispatch ──────────────────────────────────────────────

    def handle_packet(self, data: bytes) -> None:
        """Process a raw inbound C4 serial frame."""
        try:
            text = data.decode("ascii", errors="replace").strip()
        except Exception:
            return

        _LOGGER.debug("C4-Z2IO-ZP [%s] RX: %r", self.ieee, text)

        parts = text.split()
        if len(parts) < 2 or len(parts[0]) < 2:
            return

        prefix = parts[0]
        ftype  = prefix[1]   # 'r' = response, 't' = announce

        try:
            cmd_id = int(prefix[2:] or "0", 16)
        except ValueError:
            cmd_id = 0

        if ftype == "r":
            self._on_response(cmd_id, parts)
        elif ftype == "t":
            self._on_announce(parts)

    def _on_response(self, cmd_id: int, parts: list[str]) -> None:
        """Handle  0r[seq] [status] [prop] [value...]

        rlc / rlo responses are SHORT: "0r[seq] 000" with no property echoed.
        All other responses include the property name at parts[2].

        Status codes:
          000  → success
          n01  → property not available / not supported
        """
        if len(parts) < 2:
            return

        status = parts[1]                               # "000" or "n01"
        prop   = parts[2] if len(parts) > 2 else None  # absent for rlc/rlo
        values = parts[3:] if len(parts) > 3 else []

        parsed: dict[str, Any] = {
            "type": "response", "cmd_id": cmd_id,
            "status": status, "prop": prop, "values": values,
        }

        # Always resolve the waiting future, even for short responses.
        fut = self._pending.pop(cmd_id, None)
        if fut is not None and not fut.done():
            fut.set_result(parsed)

        if status == "n01":
            _LOGGER.debug(
                "C4-Z2IO-ZP [%s]: prop %s not available", self.ieee, prop
            )
            return

        if status != "000":
            return

        # rlc / rlo responses have no prop — nothing further to parse.
        if prop is None:
            return

        # Update cached state from get/set responses.
        if prop == _PROP_CTS and values:
            self._update_contacts(values[0])
        elif prop == _PROP_RLS and values:
            self._update_relays(values[0])
        elif prop == _PROP_FWV and values:
            self.fw_version = values[0]
            _LOGGER.info("C4-Z2IO-ZP [%s]: firmware %s", self.ieee, self.fw_version)
        elif prop == _PROP_BLV and values:
            self.bl_version = values[0]
            _LOGGER.debug("C4-Z2IO-ZP [%s]: bootloader %s", self.ieee, self.bl_version)
        elif prop == _PROP_ZMAC and values:
            self.zigbee_mac = values[0]
            _LOGGER.debug("C4-Z2IO-ZP [%s]: zmac %s", self.ieee, self.zigbee_mac)
        elif prop == _PROP_ZPID and values:
            self.zigbee_pid = values[0]
        elif prop == _PROP_ZEPID and values:
            self.zigbee_epid = values[0]
        elif prop == _PROP_LDM and values:
            self.link_density = values[0]
            _LOGGER.debug("C4-Z2IO-ZP [%s]: ldm %s", self.ieee, self.link_density)
        elif prop == _PROP_RLP and len(values) >= 2:
            ch = int(values[0], 16) - 1
            ticks = int(values[1], 16)
            _LOGGER.debug(
                "C4-Z2IO-ZP [%s]: relay %d pulse ticks=%d", self.ieee, ch + 1, ticks
            )
        elif prop == _PROP_TMPI and values:
            self._update_temp_internal(values[0])
        elif prop == _PROP_TMPE and values:
            self._update_temp_external(values[0])
        elif prop == _PROP_THUMI and values:
            self._update_humidity(values[0])

    def _on_announce(self, parts: list[str]) -> None:
        """Handle  0t[seq] sa [prop] [value]"""
        if len(parts) < 3:
            return
        prop  = parts[2]
        value = parts[3] if len(parts) > 3 else None

        if prop == _PROP_CTS and value is not None:
            self._update_contacts(value)
        elif prop == _PROP_RLS and value is not None:
            self._update_relays(value)
        elif prop == _PROP_TMPI and value is not None:
            self._update_temp_internal(value)
        elif prop == _PROP_TMPE and value is not None:
            self._update_temp_external(value)
        elif prop == _PROP_THUMI and value is not None:
            self._update_humidity(value)

    # ── State helpers ─────────────────────────────────────────────────────────

    def _update_contacts(self, value_str: str) -> None:
        """Parse cts hex bitmask and update per-channel contact state."""
        try:
            cts = int(value_str, 16)
        except ValueError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: bad cts value %r", self.ieee, value_str
            )
            return

        self._cts_raw = cts
        changed: list[dict] = []

        for ch in range(self.num_contacts):
            bit = bool(cts & (1 << ch))
            if CONTACT_BIT_CLOSED.get(ch, False):
                new_state = DOOR_CLOSED if bit else DOOR_OPEN
            else:
                new_state = DOOR_OPEN if bit else DOOR_CLOSED

            if new_state != self._contact_state[ch]:
                prev = self._contact_state[ch]
                self._contact_state[ch] = new_state
                changed.append({"channel": ch + 1, "state": new_state, "previous": prev})
                _LOGGER.info(
                    "C4-Z2IO-ZP [%s]: contact %d %s → %s  (cts=0x%02x)",
                    self.ieee, ch + 1, prev, new_state, cts,
                )

        if changed:
            self._fire_event("contact_state_changed", {
                "changes": changed,
                "cts_raw": cts,
                "door_state": self._contact_state[0],
            })

    def _update_relays(self, value_str: str) -> None:
        """Parse rls hex bitmask and update per-relay state."""
        try:
            rls = int(value_str, 16)
        except ValueError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: bad rls value %r", self.ieee, value_str
            )
            return

        self._rls_raw = rls
        changed: list[dict] = []

        for ch in range(self.num_relays):
            on = bool(rls & (1 << ch))
            if on != self._relay_on[ch]:
                self._relay_on[ch] = on
                changed.append({"channel": ch + 1, "on": on})
                _LOGGER.debug(
                    "C4-Z2IO-ZP [%s]: relay %d %s  (rls=0x%02x)",
                    self.ieee, ch + 1, "ON" if on else "OFF", rls,
                )

        if changed:
            self._fire_event("relay_state_changed", {
                "changes": changed,
                "rls_raw": rls,
            })

    def _update_temp_internal(self, value_str: str) -> None:
        """Parse tmpi hex centikelvins and update internal temperature state."""
        try:
            raw = int(value_str, 16)
        except ValueError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: bad tmpi value %r", self.ieee, value_str
            )
            return
        self._temp_internal_raw = raw
        temp_c = raw * 0.01 - 273.15
        _LOGGER.debug(
            "C4-Z2IO-ZP [%s]: tmpi raw=0x%04x → %.2f °C", self.ieee, raw, temp_c
        )

    def _update_temp_external(self, value_str: str) -> None:
        """Parse tmpe hex centikelvins and update external probe temperature state."""
        try:
            raw = int(value_str, 16)
        except ValueError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: bad tmpe value %r", self.ieee, value_str
            )
            return
        self._temp_external_raw = raw
        if raw == TEMP_NO_PROBE_RAW:
            _LOGGER.debug(
                "C4-Z2IO-ZP [%s]: tmpe sentinel (probe not connected)", self.ieee
            )
        else:
            temp_c = raw * 0.01 - 273.15
            _LOGGER.debug(
                "C4-Z2IO-ZP [%s]: tmpe raw=0x%04x → %.2f °C", self.ieee, raw, temp_c
            )

    def _update_humidity(self, value_str: str) -> None:
        """Parse thumi hex value and update humidity state.

        Encoding: raw / 100 = % RH  (e.g. 0x1993 = 6547 → 65.47 % RH)
        """
        try:
            raw = int(value_str, 16)
        except ValueError:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: bad thumi value %r", self.ieee, value_str
            )
            return
        self._humidity_raw = raw
        _LOGGER.debug(
            "C4-Z2IO-ZP [%s]: thumi raw=0x%04x → %.2f %% RH",
            self.ieee, raw, raw / 100.0,
        )

    def _fire_event(self, sub_type: str, extra: dict) -> None:
        """Fire a zha_event on the HA bus."""
        if self._hass is None:
            _LOGGER.debug(
                "C4-Z2IO-ZP [%s]: _fire_event(%s) skipped — hass not available",
                self.ieee, sub_type,
            )
            return
        self._hass.bus.fire(
            ZHA_EVENT,
            {
                "device_ieee": self.ieee,
                "unique_id":   self.ieee,
                "device_name": f"{C4_DEVICE_TYPE} {self.ieee[-5:]}",
                "event_type":  C4_DEVICE_TYPE,
                "event_data":  {"sub_type": sub_type, **extra},
            },
        )

    # ── Public properties ─────────────────────────────────────────────────────

    def contact_state(self, channel: int = 1) -> str:
        """Return 'open', 'closed', or 'unknown' for contact channel (1-indexed)."""
        if self.num_contacts == 0 or channel < 1 or channel > self.num_contacts:
            return DOOR_UNKNOWN
        return self._contact_state[channel - 1]

    def relay_active(self, channel: int = 1) -> bool:
        """True while relay N (1-indexed) is energised."""
        if self.num_relays == 0 or channel < 1 or channel > self.num_relays:
            return False
        return self._relay_on[channel - 1]

    # ── Public commands ───────────────────────────────────────────────────────

    async def close_relay(self, channel: int) -> bool:
        """Energize relay N via c4.z2x.rlc.

        Sends the latch-close command and waits for the device's 000 response.
        The device will announce rls with the relay bit set.
        Call open_relay() after your desired dwell time to release.
        """
        if self.num_relays == 0:
            _LOGGER.error(
                "C4-Z2IO-ZP [%s]: close_relay called but opt_mode=%d has no relays",
                self.ieee, self.opt_mode,
            )
            return False
        if channel < 1 or channel > self.num_relays:
            _LOGGER.error(
                "close_relay: channel must be 1..%d for opt_mode=%d, got %d",
                self.num_relays, self.opt_mode, channel,
            )
            return False
        seq, data = self._set_frame(_PROP_RLC, str(channel))
        resp = await self._send(seq, data)
        ok = resp is not None and resp.get("status") == "000"
        if not ok:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: rlc %d failed (resp=%s)", self.ieee, channel, resp
            )
        return ok

    async def open_relay(self, channel: int) -> bool:
        """De-energize relay N via c4.z2x.rlo.

        Sends the latch-open command and waits for the 000 response.
        The device will announce rls 00 when the relay is released.
        """
        if self.num_relays == 0:
            _LOGGER.error(
                "C4-Z2IO-ZP [%s]: open_relay called but opt_mode=%d has no relays",
                self.ieee, self.opt_mode,
            )
            return False
        if channel < 1 or channel > self.num_relays:
            _LOGGER.error(
                "open_relay: channel must be 1..%d for opt_mode=%d, got %d",
                self.num_relays, self.opt_mode, channel,
            )
            return False
        seq, data = self._set_frame(_PROP_RLO, str(channel))
        resp = await self._send(seq, data)
        ok = resp is not None and resp.get("status") == "000"
        if not ok:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: rlo %d failed (resp=%s)", self.ieee, channel, resp
            )
        return ok

    async def trigger_relay(
        self, channel: int = 1, pulse_ms: int = DEFAULT_PULSE_MS
    ) -> bool:
        """Pulse relay N using the rlc / rlo sequence observed in capture.

        Sequence:
          1. Send c4.z2x.rlc N  (energize)
          2. Wait pulse_ms milliseconds
          3. Send c4.z2x.rlo N  (de-energize)

        Returns True only if both commands were acknowledged with status 000.
        The default pulse width of 500 ms matches the ~440-500 ms observed in
        the open/close Wireshark capture.
        """
        if self.num_relays == 0:
            _LOGGER.error(
                "C4-Z2IO-ZP [%s]: trigger_relay called but opt_mode=%d has no relays",
                self.ieee, self.opt_mode,
            )
            return False
        if channel < 1 or channel > self.num_relays:
            _LOGGER.error(
                "trigger_relay: channel must be 1..%d for opt_mode=%d, got %d",
                self.num_relays, self.opt_mode, channel,
            )
            return False

        _LOGGER.info(
            "C4-Z2IO-ZP [%s]: triggering relay %d (pulse %d ms)",
            self.ieee, channel, pulse_ms,
        )

        if not await self.close_relay(channel):
            return False

        await asyncio.sleep(pulse_ms / 1000.0)

        if not await self.open_relay(channel):
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: relay %d rlc succeeded but rlo failed — "
                "relay may remain energized!",
                self.ieee, channel,
            )
            return False

        return True

    async def set_relay_pulse(self, channel: int, ticks: int) -> bool:
        """Set the momentary pulse duration for relay N (provisioning parameter)."""
        if self.num_relays == 0 or channel < 1 or channel > self.num_relays:
            _LOGGER.error(
                "set_relay_pulse: channel %d invalid for opt_mode=%d (%d relay(s))",
                channel, self.opt_mode, self.num_relays,
            )
            return False
        seq, data = self._set_frame(
            _PROP_RLP, f"{channel:02x}", f"{ticks:02x}"
        )
        resp = await self._send(seq, data)
        return resp is not None and resp.get("status") == "000"

    async def poll_state(self) -> None:
        """Query the device for current contact, relay, and sensor state.

        Only polls properties relevant to the current IO mode:
          - cts  if num_contacts > 0
          - rls  if num_relays > 0
          - tmpi, tmpe, thumi  always (sensors are mode-independent)
        """
        _LOGGER.debug("C4-Z2IO-ZP [%s]: polling state (opt_mode=%d)", self.ieee, self.opt_mode)
        props: list[str] = []
        if self.num_contacts > 0:
            props.append(_PROP_CTS)
        if self.num_relays > 0:
            props.append(_PROP_RLS)
        props.extend((_PROP_TMPI, _PROP_TMPE, _PROP_THUMI))
        for prop in props:
            seq, data = self._get_frame(prop)
            await self._send(seq, data)
            await asyncio.sleep(0.3)

    async def read_device_info(self) -> dict:
        """Query all read-only device info properties."""
        for prop in (
            _PROP_FWV, _PROP_BLV,
            _PROP_LDM, _PROP_ZMAC, _PROP_ZPID, _PROP_ZEPID,
            _PROP_ZNID, _PROP_ZCHAN,
            _PROP_TMPI, _PROP_TMPE, _PROP_THUMI,
        ):
            seq, data = self._get_frame(prop)
            await self._send(seq, data, timeout=3.0)
            await asyncio.sleep(0.05)

        return {
            "opt_mode":     self.opt_mode,
            "num_relays":   self.num_relays,
            "num_contacts": self.num_contacts,
            "fw_version":   self.fw_version,
            "bl_version":   self.bl_version,
            "zigbee_mac":   self.zigbee_mac,
            "zigbee_pid":   self.zigbee_pid,
            "zigbee_epid":  self.zigbee_epid,
            "zigbee_nid":   self.zigbee_nid,
            "zigbee_chan":  self.zigbee_chan,
            "temp_internal_raw": self._temp_internal_raw,
            "temp_external_raw": self._temp_external_raw,
            "humidity_raw":      self._humidity_raw,
            "link_density":      self.link_density,
        }

    async def change_opt_mode(self, new_mode: int) -> bool:
        """Change the IO mode at runtime and send the command to the device.

        Updates internal state and sends c4.z2x.opt to the device.
        Returns True on success.  Does NOT re-provision (call configure_device
        for a full re-init, or re-interview the device in ZHA to update entities).
        """
        if new_mode not in OPT_MODES:
            _LOGGER.error(
                "C4-Z2IO-ZP [%s]: invalid opt_mode %d", self.ieee, new_mode,
            )
            return False

        old_mode = self.opt_mode
        self.opt_mode = new_mode
        self.num_relays, self.num_contacts = OPT_MODES[new_mode]

        # Resize state arrays to match new mode
        self._contact_state = [DOOR_UNKNOWN] * self.num_contacts
        self._relay_on      = [False] * self.num_relays
        self._cts_raw = 0
        self._rls_raw = 0

        _LOGGER.info(
            "C4-Z2IO-ZP [%s]: opt_mode changed %d → %d (%d relay(s), %d contact(s))",
            self.ieee, old_mode, new_mode, self.num_relays, self.num_contacts,
        )

        # Send to device
        seq, data = self._set_frame(_PROP_OPT, str(new_mode))
        resp = await self._send(seq, data)
        ok = bool(resp and resp.get("status") == "000")
        if not ok:
            _LOGGER.warning(
                "C4-Z2IO-ZP [%s]: opt set to %d TX response: %s",
                self.ieee, new_mode, resp,
            )

        # Poll fresh state for the new mode
        await self.poll_state()
        return ok

    async def configure_device(self) -> None:
        """Two-pass provisioning sequence matching the observed provisioning capture.

        Pass 1 — configuration & discovery:
          1.  ctd = 1, 1f4      (contact 1 debounce = 500 ms)
          2.  opt = <mode>       (IO mode: 1=2 relays, 2=4 contacts, 3=1 relay+2 contacts)
          3.  cts / rls          (read initial state)
          4.  rlp query 1 / 2   (read relay pulse durations — if mode has relays)
          5.  ana0 / ana1        (queried, returns n01 — expected)
          6.  Full device info read
          7.  zpc = 348          (set Zigbee poll counter)

        Pass 2 — verification:
          8.  ctd = 1, 1f4      (re-apply debounce)
          9.  opt = <mode>      (re-apply IO mode)
          10. cts / rls / opt   (verify state)
          11. rlp 1, rlp 2      (re-read pulse durations — if mode has relays)
          12. ana0               (n01 expected)

        Note: relay actuation during normal operation uses rlc/rlo (not rls),
        regardless of the opt setting applied here.
        """
        opt_str = str(self.opt_mode)
        _LOGGER.info(
            "C4-Z2IO-ZP [%s]: provisioning (pass 1) opt_mode=%s",
            self.ieee, opt_str,
        )
        delay = 0.05

        # 1. Contact debounce — channel 1, 0x1f4 = 500 ms
        seq, data = self._set_frame(_PROP_CTD, "1", "1f4")
        resp = await self._send(seq, data)
        if not (resp and resp.get("status") == "000"):
            _LOGGER.warning("C4-Z2IO-ZP [%s]: ctd set failed: %s", self.ieee, resp)
        await asyncio.sleep(delay)

        # 2. IO mode
        seq, data = self._set_frame(_PROP_OPT, opt_str)
        resp = await self._send(seq, data)
        if not (resp and resp.get("status") == "000"):
            _LOGGER.warning("C4-Z2IO-ZP [%s]: opt set failed: %s", self.ieee, resp)
        await asyncio.sleep(delay)

        # 3. Read initial contact and relay state
        seq, data = self._get_frame(_PROP_CTS)
        await self._send(seq, data)
        await asyncio.sleep(delay)
        seq, data = self._get_frame(_PROP_RLS)
        await self._send(seq, data)
        await asyncio.sleep(delay)

        # 4. Relay pulse durations (only meaningful when mode has relays)
        if self.num_relays > 0:
            for ch in range(1, self.num_relays + 1):
                seq, data = self._get_frame(_PROP_RLP, str(ch))
                await self._send(seq, data)
                await asyncio.sleep(delay)

        # 5. Ana inputs (n01 expected)
        for prop in (_PROP_ANA0, _PROP_ANA1):
            seq, data = self._get_frame(prop)
            await self._send(seq, data, timeout=2.0)
            await asyncio.sleep(delay)

        # 6. Full device info
        await self.read_device_info()

        # 7. Zigbee poll counter
        seq, data = self._set_frame(_PROP_ZPC, "348")
        await self._send(seq, data)
        await asyncio.sleep(delay)

        # Pass 2
        _LOGGER.debug("C4-Z2IO-ZP [%s]: provisioning (pass 2)", self.ieee)

        seq, data = self._set_frame(_PROP_CTD, "1", "1f4")
        await self._send(seq, data)
        await asyncio.sleep(delay)

        seq, data = self._set_frame(_PROP_OPT, opt_str)
        await self._send(seq, data)
        await asyncio.sleep(delay)

        # Verify state
        await self.poll_state()
        seq, data = self._get_frame(_PROP_OPT)
        await self._send(seq, data)
        await asyncio.sleep(delay)

        # Re-query rlp and ana (only if mode has relays)
        if self.num_relays > 0:
            for ch in range(1, self.num_relays + 1):
                seq, data = self._get_frame(_PROP_RLP, str(ch))
                await self._send(seq, data)
                await asyncio.sleep(delay)

        seq, data = self._get_frame(_PROP_ANA0)
        await self._send(seq, data, timeout=2.0)

        _LOGGER.info(
            "C4-Z2IO-ZP [%s]: provisioning complete  fw=%s bl=%s ch=%s nid=%s",
            self.ieee, self.fw_version, self.bl_version,
            self.zigbee_chan, self.zigbee_nid,
        )


# ── HA service registration ────────────────────────────────────────────────────

async def async_setup_services(hass, handler_map: dict[str, "C4Z2IOZPHandler"]) -> None:
    """Register HA services for the C4-Z2IO-ZP.

    Services registered under domain 'zha_c4_patch':

      garage_door_trigger
        data:
          ieee:     "xx:xx:xx:xx:xx:xx:xx:xx"  (required)
          relay:    1 or 2                      (optional, default 1)
          pulse_ms: integer ms                  (optional, default 500)

      garage_door_poll
        data:
          ieee: "xx:xx:xx:xx:xx:xx:xx:xx"
    """
    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol

    ATTR_IEEE     = "ieee"
    ATTR_RELAY    = "relay"
    ATTR_PULSE_MS = "pulse_ms"

    TRIGGER_SCHEMA = vol.Schema({
        vol.Required(ATTR_IEEE):                       cv.string,
        vol.Optional(ATTR_RELAY,    default=1):        vol.All(int, vol.Range(min=1, max=2)),
        vol.Optional(ATTR_PULSE_MS, default=DEFAULT_PULSE_MS):
            vol.All(int, vol.Range(min=100, max=5000)),
    })

    POLL_SCHEMA = vol.Schema({
        vol.Required(ATTR_IEEE): cv.string,
    })

    def _get_handler(ieee: str) -> "C4Z2IOZPHandler | None":
        key = ieee.lower()
        h = handler_map.get(key)
        if h is None:
            _LOGGER.error(
                "C4-Z2IO-ZP service: no handler for %s (known: %s)",
                key, list(handler_map),
            )
        return h

    async def handle_trigger(call) -> None:
        handler = _get_handler(call.data[ATTR_IEEE])
        if handler:
            await handler.trigger_relay(
                call.data[ATTR_RELAY],
                call.data[ATTR_PULSE_MS],
            )

    async def handle_poll(call) -> None:
        handler = _get_handler(call.data[ATTR_IEEE])
        if handler:
            await handler.poll_state()

    for svc, func, schema in (
        ("garage_door_trigger", handle_trigger, TRIGGER_SCHEMA),
        ("garage_door_poll",    handle_poll,    POLL_SCHEMA),
    ):
        hass.services.async_register(
            domain="zha_c4_patch",
            service=svc,
            service_func=func,
            schema=schema,
        )
        _LOGGER.debug("Registered service zha_c4_patch.%s", svc)
