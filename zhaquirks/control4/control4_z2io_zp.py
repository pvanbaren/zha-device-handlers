"""ZHA quirk for the Control4 C4-Z2IO-ZP Zigbee IO / Garage Door Module.

ZHA entities created
────────────────────
  switch.c4_z2io_zp_relay_1/2         EP 211/212  (OnOff cluster)
  binary_sensor.c4_z2io_zp_contact_1…5  EPs 201-205  (BinaryInput cluster)
  sensor.c4_z2io_zp_temperature         EP 221  (TemperatureMeasurement — internal)
  sensor.c4_z2io_zp_temperature_2       EP 222  (TemperatureMeasurement — external probe)
  sensor.c4_z2io_zp_humidity            EP 223  (RelativeHumidity)

Packet routing
──────────────
  Inbound C4 ASCII frames (profile=0xC25C, cluster=0x0001, ep=197):
    c4_hooks Patch 2 → C4Z2IOCluster.handle_message()
    → strip 8-byte C4 APS header
    → C4Z2IOZPHandler.handle_packet()
    → push changed relay/contact state to synthetic ZHA cluster caches
    → HA entity state updated

  EP 197 uses C4_PROFILE_BUTTON (0xC25C) so ZHA skips it entirely
  ("Skipping endpoint, profile is not ZLL or ZHA: 0xC25C").
  Relay/contact clusters live on separate ZHA-profile endpoints.

  Outbound (sent by handler):
    app.request(profile=0xC25C, cluster=0x0001, src_ep=197, dst_ep=197)
    data = raw ASCII, e.g. "0s0040 c4.z2x.rlc 1\r\n"
"""

from __future__ import annotations

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
from zigpy.zcl.clusters.general import (
    BinaryInput, Identify, OnOff,
)
from zigpy.zcl.clusters.measurement import (
    RelativeHumidity, TemperatureMeasurement,
)

from zhaquirks.const import (
    DEVICE_TYPE,
    ENDPOINTS,
    INPUT_CLUSTERS,
    MODELS_INFO,
    OUTPUT_CLUSTERS,
    PROFILE_ID,
)

import c4_hooks  # noqa: F401

import c4_helpers as C4
from c4_helpers import (
    C4_CLUSTER_ID,
    C4_MANUF_CLUSTER,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4DimmerManufCluster,
    C4ConfigCluster,
)
from c4_basic_cluster import C4BasicCluster
from c4_hooks import _C4_MODEL_QUIRK_MAP

from c4_z2io_zp import (
    C4Z2IOZPHandler,
    async_setup_services,
    DOOR_CLOSED,
    NUM_CONTACTS,
    NUM_RELAYS,
    TEMP_NO_PROBE_RAW,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_C4_Z2IO_HANDLER_MAP: dict[str, C4Z2IOZPHandler] = {}
_SERVICES_REGISTERED = False
# IEEE strings for which configure_device() has already been scheduled this
# session, to avoid double-provisioning when both relay clusters bind.
_C4_Z2IO_CONFIGURED: set[str] = set()

# Relay channel (1-indexed) → synthetic ZHA endpoint ID
_RELAY_EP = {1: 211, 2: 212}
# Contact channel (1-indexed) → synthetic ZHA endpoint ID
_CONTACT_EP = {ch: 200 + ch for ch in range(1, NUM_CONTACTS + 1)}
# Sensor endpoint IDs
_TEMP_INTERNAL_EP = 221   # TemperatureMeasurement — c4.z2x.tmpi
_TEMP_EXTERNAL_EP = 222   # TemperatureMeasurement — c4.z2x.tmpe (external probe)
_HUMIDITY_EP      = 223   # RelativeHumidity       — c4.z2x.thumi

# C4 APS application header signatures (bytes 4:6 of raw APS payload)
_C4_APS_HDR_PROFILES = (b'\x5d\xc2', b'\x5c\xc2', b'\x5e\xc2')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_hass(device):
    app = device.application
    for attr in ('hass', '_hass'):
        h = getattr(app, attr, None)
        if h is not None:
            return h
    gw = getattr(app, 'zha_gateway', None)
    if gw is not None:
        return getattr(gw, 'hass', None)
    return None


def _strip_c4_header(data: bytes) -> bytes:
    """Remove the 8-byte C4 APS application header if present."""
    if len(data) >= 8 and data[4:6] in _C4_APS_HDR_PROFILES:
        return data[8:]
    return data


async def _register_services_once(hass) -> None:
    global _SERVICES_REGISTERED
    if _SERVICES_REGISTERED:
        return
    try:
        await async_setup_services(hass, _C4_Z2IO_HANDLER_MAP)
        _SERVICES_REGISTERED = True
        _LOGGER.info("C4 Z2IO: registered HA services")
    except Exception as exc:
        _LOGGER.error("C4 Z2IO: service registration failed — %s", exc)


# ---------------------------------------------------------------------------
# Relay switch clusters  (EPs 211, 212)
# ---------------------------------------------------------------------------

class C4RelayCluster(CustomCluster, OnOff):
    """OnOff cluster driving a single C4-Z2IO-ZP relay via rlc/rlo.

    ep_attribute is intentionally NOT overridden; it inherits "on_off" from
    OnOff so that zigpy's endpoint.on_off lookup succeeds and ZHA creates
    an OnOffClusterHandler — which is what triggers switch entity discovery.
    """

    _relay_channel: int = 1
    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _handler(self) -> C4Z2IOZPHandler | None:
        return _C4_Z2IO_HANDLER_MAP.get(str(self.endpoint.device.ieee).lower())

    async def command(self, command_id, *args,
                      manufacturer=None, expect_reply=False, tsn=None, **kwargs):
        """Any on/off/toggle command triggers a momentary relay pulse.

        The physical relay energizes for DEFAULT_PULSE_MS then releases
        automatically.  We fire the pulse and immediately reset the HA
        switch state to off so the toggle appears momentary in the UI.
        """
        h = self._handler()
        if h is None:
            _LOGGER.warning("C4 relay%d: no handler for %s",
                            self._relay_channel, self.endpoint.device.ieee)
            return self._SUCCESS

        # Always pulse — ignore on/off/toggle direction.
        asyncio.ensure_future(self._pulse_and_reset(h))
        return self._SUCCESS

    async def _pulse_and_reset(self, h: C4Z2IOZPHandler) -> None:
        """Trigger the relay pulse, then reset the switch entity to off."""
        on_off_id = OnOff.AttributeDefs.on_off.id
        # Show "on" briefly while the pulse is in flight.
        self._update_attribute(on_off_id, True)
        try:
            await h.trigger_relay(self._relay_channel)
        finally:
            # Always snap back to off once the pulse completes (or fails).
            self._update_attribute(on_off_id, False)

    async def read_attributes(self, attributes,
                              allow_cache=False, only_cache=False, manufacturer=None):
        h = self._handler()
        on_off_id = OnOff.AttributeDefs.on_off.id
        results, failures = {}, {}
        for attr in attributes:
            attr_id = self.find_attribute(attr).id if isinstance(attr, str) else int(attr)
            key = attr if isinstance(attr, str) else attr_id
            if attr_id == on_off_id:
                val = h.relay_active(self._relay_channel) if h else False
                self._update_attribute(on_off_id, val)
                results[key] = val
            else:
                failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
        return results, failures

    async def bind(self):
        """Create the per-device handler and run provisioning (once per device).

        ZHA calls bind() on relay clusters during async_configure.  Since
        C4Z2IOCluster lives on a non-ZHA-profile endpoint that ZHA skips,
        this is the only lifecycle hook we can rely on to create the handler
        and send configure_device() to the physical device.
        """
        ieee_str = str(self.endpoint.device.ieee).lower()
        device   = self.endpoint.device

        # Ensure the handler exists in the shared map.
        if ieee_str not in _C4_Z2IO_HANDLER_MAP:
            _C4_Z2IO_HANDLER_MAP[ieee_str] = C4Z2IOZPHandler(
                ieee_str, device, _get_hass(device), device.application
            )
            _LOGGER.info("C4 Z2IO: created handler for %s from relay bind()", ieee_str)

        # Run provisioning exactly once per session per device.
        if ieee_str not in _C4_Z2IO_CONFIGURED:
            _C4_Z2IO_CONFIGURED.add(ieee_str)
            handler = _C4_Z2IO_HANDLER_MAP[ieee_str]
            _LOGGER.info("C4 Z2IO: scheduling configure_device() for %s", ieee_str)
            async def _do_configure(h=handler, ieee=ieee_str):
                try:
                    await h.configure_device()
                    await h.poll_state()
                    _LOGGER.info("C4 Z2IO: provisioning complete for %s fw=%s",
                                 ieee, h.fw_version)
                except Exception as exc:
                    _LOGGER.error("C4 Z2IO: configure_device() failed for %s — %s",
                                  ieee, exc)
            asyncio.ensure_future(_do_configure())

        hass = _get_hass(device)
        if hass is not None:
            asyncio.ensure_future(_register_services_once(hass))

        return [ZCLStatus.SUCCESS]

    async def configure_reporting(self, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
                for _ in range(len(records) if records else 1)]


class C4Relay1Cluster(C4RelayCluster):
    _relay_channel = 1


class C4Relay2Cluster(C4RelayCluster):
    _relay_channel = 2


# ---------------------------------------------------------------------------
# Contact binary-sensor clusters  (EPs 201-205)
# ---------------------------------------------------------------------------

class C4ContactCluster(CustomCluster, BinaryInput):
    """BinaryInput cluster for one C4-Z2IO-ZP contact input.

    present_value=True → CLOSED,  present_value=False → OPEN/unknown.

    ep_attribute inherits "binary_input" from BinaryInput so that
    endpoint.binary_input resolves correctly and ZHA creates a
    BinaryInputClusterHandler → binary_sensor entity.
    """

    _contact_channel: int = 1

    def _handler(self) -> C4Z2IOZPHandler | None:
        return _C4_Z2IO_HANDLER_MAP.get(str(self.endpoint.device.ieee).lower())

    async def read_attributes(self, attributes,
                              allow_cache=False, only_cache=False, manufacturer=None):
        h = self._handler()
        pv_id = BinaryInput.AttributeDefs.present_value.id
        results, failures = {}, {}
        for attr in attributes:
            attr_id = self.find_attribute(attr).id if isinstance(attr, str) else int(attr)
            key = attr if isinstance(attr, str) else attr_id
            if attr_id == pv_id:
                closed = (h.contact_state(self._contact_channel) == DOOR_CLOSED
                          if h else False)
                self._update_attribute(pv_id, closed)
                results[key] = closed
            else:
                failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
        return results, failures

    async def bind(self):
        return [ZCLStatus.SUCCESS]

    async def configure_reporting(self, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
                for _ in range(len(records) if records else 1)]


class C4Contact1Cluster(C4ContactCluster):
    _contact_channel = 1

class C4Contact2Cluster(C4ContactCluster):
    _contact_channel = 2

class C4Contact3Cluster(C4ContactCluster):
    _contact_channel = 3

class C4Contact4Cluster(C4ContactCluster):
    _contact_channel = 4

class C4Contact5Cluster(C4ContactCluster):
    _contact_channel = 5


# ---------------------------------------------------------------------------
# Temperature / humidity sensor clusters  (EPs 221, 222, 223)
# ---------------------------------------------------------------------------

class C4TempCluster(CustomCluster, TemperatureMeasurement):
    """TemperatureMeasurement cluster backed by C4-Z2IO-ZP sensor data.

    ZCL measured_value unit is 0.01 °C (int16).  Conversion from C4 encoding:
      raw (centikelvins integer) → ZCL = raw − 27315

    Example: raw=0x71f8 (29176) → 29176 − 27315 = 1861 → 18.61 °C
    Probe-absent sentinel: raw=0x5b13 (23315) → −4000 → −40.00 °C
    """

    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _handler(self) -> C4Z2IOZPHandler | None:
        return _C4_Z2IO_HANDLER_MAP.get(str(self.endpoint.device.ieee).lower())

    def _raw_value(self) -> int | None:
        """Return the raw centikelvins integer for this sensor channel."""
        raise NotImplementedError

    async def read_attributes(self, attributes,
                              allow_cache=False, only_cache=False, manufacturer=None):
        mv_id = TemperatureMeasurement.AttributeDefs.measured_value.id
        results, failures = {}, {}
        for attr in attributes:
            attr_id = (self.find_attribute(attr).id if isinstance(attr, str)
                       else int(attr))
            key = attr if isinstance(attr, str) else attr_id
            if attr_id == mv_id:
                raw = self._raw_value()
                if raw is not None:
                    zcl_val = raw - 27315   # centikelvins → 0.01 °C
                    self._update_attribute(mv_id, zcl_val)
                    results[key] = zcl_val
                else:
                    failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
            else:
                failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
        return results, failures

    async def bind(self):
        return [ZCLStatus.SUCCESS]

    async def configure_reporting(self, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
                for _ in range(len(records) if records else 1)]


class C4TempInternalCluster(C4TempCluster):
    """Internal temperature sensor (c4.z2x.tmpi) on EP 221."""

    def _raw_value(self) -> int | None:
        h = self._handler()
        return h._temp_internal_raw if h else None


class C4TempExternalCluster(C4TempCluster):
    """External probe temperature (c4.z2x.tmpe) on EP 222.

    Reports −40 °C (ZCL −4000) when no probe is connected (sentinel 0x5b13).
    """

    def _raw_value(self) -> int | None:
        h = self._handler()
        return h._temp_external_raw if h else None


class C4HumidityCluster(CustomCluster, RelativeHumidity):
    """RelativeHumidity cluster backed by c4.z2x.thumi sensor data.

    ZCL measured_value unit is 0.01 % RH (uint16).  Conversion from C4:
      raw (integer) / 100 = % RH  →  ZCL = raw  (no scaling needed)

    Example: raw=0x1993 (6547) → 6547 / 100 = 65.47 % RH → ZCL = 6547
    """

    _SUCCESS = (foundation.GeneralCommand.Default_Response, ZCLStatus.SUCCESS)

    def _handler(self) -> C4Z2IOZPHandler | None:
        return _C4_Z2IO_HANDLER_MAP.get(str(self.endpoint.device.ieee).lower())

    async def read_attributes(self, attributes,
                              allow_cache=False, only_cache=False, manufacturer=None):
        mv_id = RelativeHumidity.AttributeDefs.measured_value.id
        results, failures = {}, {}
        for attr in attributes:
            attr_id = (self.find_attribute(attr).id if isinstance(attr, str)
                       else int(attr))
            key = attr if isinstance(attr, str) else attr_id
            if attr_id == mv_id:
                h = self._handler()
                raw = h._humidity_raw if h else None
                if raw is not None:
                    self._update_attribute(mv_id, raw)
                    results[key] = raw
                else:
                    failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
            else:
                failures[key] = foundation.Status.UNSUPPORTED_ATTRIBUTE
        return results, failures

    async def bind(self):
        return [ZCLStatus.SUCCESS]

    async def configure_reporting(self, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
                for _ in range(len(records) if records else 1)]


# ---------------------------------------------------------------------------
# C4Z2IOCluster — EP 197 (C4_PROFILE_BUTTON, skipped by ZHA entity discovery)
# ---------------------------------------------------------------------------

class C4Z2IOCluster(CustomCluster):
    """Packet-receiver shim on EP 197.

    EP 197 is given C4_PROFILE_BUTTON (0xC25C) so ZHA skips it entirely
    during cluster handler creation and entity discovery, avoiding the
    PowerConfigurationClusterHandler collision on cluster_id=0x0001.

    Patch 2 in c4_hooks hardcodes EP 197 as the target for C4_PROFILE_BUTTON
    packets and selects this cluster by _c4_custom_handler=True, so neither
    the profile nor the cluster_id matters for packet routing.

    State changes are pushed directly into the relay/contact cluster caches
    on the synthetic ZHA-profile endpoints (211, 212, 201-205).
    """

    cluster_id         = C4_CLUSTER_ID  # 0x0001
    name               = "Control4 Z2IO"
    ep_attribute       = "c4_z2io"
    _c4_custom_handler = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._handler: C4Z2IOZPHandler | None = None
        self._provisioned = False
        self._polled_on_restart = False

    # ------------------------------------------------------------------
    # Handler lifecycle
    # ------------------------------------------------------------------

    def _ensure_handler(self) -> C4Z2IOZPHandler:
        ieee_str = str(self.endpoint.device.ieee).lower()
        existing = _C4_Z2IO_HANDLER_MAP.get(ieee_str)
        if existing is not None:
            self._handler = existing
            return existing
        device  = self.endpoint.device
        handler = C4Z2IOZPHandler(
            ieee_str, device, _get_hass(device), device.application
        )
        _C4_Z2IO_HANDLER_MAP[ieee_str] = handler
        self._handler = handler
        _LOGGER.info("C4 Z2IO: created handler for %s", ieee_str)
        return handler

    # ------------------------------------------------------------------
    # State push helpers
    # ------------------------------------------------------------------

    def _push_relay(self, channel: int, on: bool) -> None:
        ep = self.endpoint.device.endpoints.get(_RELAY_EP[channel])
        if ep is None:
            return
        for cluster in ep.in_clusters.values():
            if isinstance(cluster, C4RelayCluster):
                cluster._update_attribute(OnOff.AttributeDefs.on_off.id, on)
                return

    def _push_contact(self, channel: int, state: str) -> None:
        ep = self.endpoint.device.endpoints.get(_CONTACT_EP[channel])
        if ep is None:
            return
        closed = (state == DOOR_CLOSED)
        for cluster in ep.in_clusters.values():
            if isinstance(cluster, C4ContactCluster):
                cluster._update_attribute(
                    BinaryInput.AttributeDefs.present_value.id, closed
                )
                return

    def _push_temp(self, ep_id: int, raw: int) -> None:
        """Push a raw centikelvins value into a C4TempCluster attribute cache.

        ZCL measured_value = raw − 27315  (converts centikelvins → 0.01 °C).
        """
        ep = self.endpoint.device.endpoints.get(ep_id)
        if ep is None:
            return
        zcl_val = raw - 27315
        mv_id = TemperatureMeasurement.AttributeDefs.measured_value.id
        for cluster in ep.in_clusters.values():
            if isinstance(cluster, C4TempCluster):
                cluster._update_attribute(mv_id, zcl_val)
                return

    def _push_humidity(self, raw: int) -> None:
        """Push a raw humidity value into the C4HumidityCluster attribute cache.

        ZCL measured_value = raw  (raw already in 0.01 % RH units).
        """
        ep = self.endpoint.device.endpoints.get(_HUMIDITY_EP)
        if ep is None:
            return
        mv_id = RelativeHumidity.AttributeDefs.measured_value.id
        for cluster in ep.in_clusters.values():
            if isinstance(cluster, C4HumidityCluster):
                cluster._update_attribute(mv_id, raw)
                return

    # ------------------------------------------------------------------
    # ZHA cluster hooks
    # ------------------------------------------------------------------

    async def bind(self) -> list:
        handler = self._ensure_handler()
        _LOGGER.info("C4 Z2IO: bind() → configure_device() for %s", handler.ieee)
        try:
            await handler.configure_device()
            self._provisioned = True
            await handler.poll_state()
            _LOGGER.info("C4 Z2IO: provisioning complete for %s  fw=%s",
                         handler.ieee, handler.fw_version)
        except Exception as exc:
            _LOGGER.error("C4 Z2IO: configure_device() failed for %s — %s",
                          handler.ieee, exc)

        hass = _get_hass(self.endpoint.device)
        if hass is not None:
            await _register_services_once(hass)
        return [ZCLStatus.SUCCESS]

    async def configure_reporting(self, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)]

    async def configure_reporting_multiple(self, records, *args, **kwargs):
        return [foundation.ConfigureReportingResponseRecord(ZCLStatus.SUCCESS)
                for _ in range(len(records) if records else 1)]

    async def read_attributes(self, attributes, *args, **kwargs):
        return {}, {a: foundation.Status.UNSUPPORTED_ATTRIBUTE for a in attributes}

    # ------------------------------------------------------------------
    # Inbound packet dispatch
    # ------------------------------------------------------------------

    def handle_message(self, hdr, args):
        if not isinstance(args, (bytes, bytearray)):
            return

        handler = self._ensure_handler()

        if not _SERVICES_REGISTERED:
            hass = _get_hass(self.endpoint.device)
            if hass is not None:
                asyncio.ensure_future(_register_services_once(hass))

        if not self._polled_on_restart and not self._provisioned:
            self._polled_on_restart = True
            asyncio.ensure_future(handler.poll_state())

        prev_relays   = list(handler._relay_on)
        prev_contacts = list(handler._contact_state)
        prev_temp_i   = handler._temp_internal_raw
        prev_temp_e   = handler._temp_external_raw
        prev_humi     = handler._humidity_raw

        handler.handle_packet(_strip_c4_header(bytes(args)))

        for ch_idx in range(NUM_RELAYS):
            if handler._relay_on[ch_idx] != prev_relays[ch_idx]:
                self._push_relay(ch_idx + 1, handler._relay_on[ch_idx])

        for ch_idx in range(NUM_CONTACTS):
            if handler._contact_state[ch_idx] != prev_contacts[ch_idx]:
                self._push_contact(ch_idx + 1, handler._contact_state[ch_idx])

        if handler._temp_internal_raw != prev_temp_i and handler._temp_internal_raw is not None:
            self._push_temp(_TEMP_INTERNAL_EP, handler._temp_internal_raw)

        if handler._temp_external_raw != prev_temp_e and handler._temp_external_raw is not None:
            self._push_temp(_TEMP_EXTERNAL_EP, handler._temp_external_raw)

        if handler._humidity_raw != prev_humi and handler._humidity_raw is not None:
            self._push_humidity(handler._humidity_raw)


# ---------------------------------------------------------------------------
# CustomDevice quirk
# ---------------------------------------------------------------------------

class Control4Z2IOZP(CustomDevice):
    """Control4 C4-Z2IO-ZP Zigbee IO and Garage Door Module.

    Entities:
      switch        relay_1, relay_2          (EPs 211, 212)
      binary_sensor contact_1 … contact_5    (EPs 201-205)
    """

    signature = {
        MODELS_INFO: [
            ("Control4", "C4-Z2IO-ZP"),
            (None,        "C4-Z2IO-ZP"),
        ],
        ENDPOINTS: {
            1: {
                PROFILE_ID:      zha.PROFILE_ID,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [0x0001, 0x0099],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    replacement = {
        ENDPOINTS: {
            # EP 1 — ZHA identity
            1: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS: [
                    C4BasicCluster,
                    Identify.cluster_id,
                    C4DimmerManufCluster,
                ],
                OUTPUT_CLUSTERS: [C4_MANUF_CLUSTER],
            },
            # EPs 2 / 196 — C4 config / model-string
            2: {
                PROFILE_ID:      C4_PROFILE_NETWORK,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            196: {
                PROFILE_ID:      C4_PROFILE_NETWORK,
                DEVICE_TYPE:     0x0000,
                INPUT_CLUSTERS:  [C4ConfigCluster],
                OUTPUT_CLUSTERS: [],
            },
            # EP 197 — C4 IO packet receiver (ZHA skips non-ZHA profiles)
            197: {
                PROFILE_ID:  C4_PROFILE_BUTTON,  # 0xC25C — ZHA skips it
                DEVICE_TYPE: 0x0000,
                INPUT_CLUSTERS:  [C4Z2IOCluster],
                OUTPUT_CLUSTERS: [],
            },
            # EPs 211 / 212 — relay switch entities
            211: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0002,  # ON_OFF_OUTPUT
                INPUT_CLUSTERS:  [C4Relay1Cluster],
                OUTPUT_CLUSTERS: [],
            },
            212: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0002,
                INPUT_CLUSTERS:  [C4Relay2Cluster],
                OUTPUT_CLUSTERS: [],
            },
            # EPs 201-205 — contact binary-sensor entities
            201: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x000C,  # SIMPLE_SENSOR
                INPUT_CLUSTERS:  [C4Contact1Cluster],
                OUTPUT_CLUSTERS: [],
            },
            202: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x000C,
                INPUT_CLUSTERS:  [C4Contact2Cluster],
                OUTPUT_CLUSTERS: [],
            },
            203: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x000C,
                INPUT_CLUSTERS:  [C4Contact3Cluster],
                OUTPUT_CLUSTERS: [],
            },
            204: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x000C,
                INPUT_CLUSTERS:  [C4Contact4Cluster],
                OUTPUT_CLUSTERS: [],
            },
            205: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x000C,
                INPUT_CLUSTERS:  [C4Contact5Cluster],
                OUTPUT_CLUSTERS: [],
            },
            # EPs 221 / 222 — temperature sensor entities
            # ZHA device type 0x0302 = Temperature Sensor
            221: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0302,
                INPUT_CLUSTERS:  [C4TempInternalCluster],
                OUTPUT_CLUSTERS: [],
            },
            222: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0302,
                INPUT_CLUSTERS:  [C4TempExternalCluster],
                OUTPUT_CLUSTERS: [],
            },
            # EP 223 — humidity sensor entity
            # ZHA device type 0x0307 = Humidity Sensor (non-standard; 0x000C also works)
            223: {
                PROFILE_ID:  zha.PROFILE_ID,
                DEVICE_TYPE: 0x0307,
                INPUT_CLUSTERS:  [C4HumidityCluster],
                OUTPUT_CLUSTERS: [],
            },
        },
    }

    device_automation_triggers = {}


# ---------------------------------------------------------------------------
# Register with the c4_hooks get_device patch
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP["C4-Z2IO-ZP"] = Control4Z2IOZP
_LOGGER.info("C4 Z2IO: registered C4-Z2IO-ZP in _C4_MODEL_QUIRK_MAP")
