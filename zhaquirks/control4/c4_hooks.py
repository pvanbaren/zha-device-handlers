"""Monkey-patches installed at import time for all C4 devices.

Patch 1 — Endpoint.initialize
  Fills in known defaults for C4 endpoints that never respond to
  Simple_Desc_req, skipping the request entirely for them.

Patch 2 — Device.custom_profile_packet_received
  Intercepts C4-profile packets at the Application level (before zigpy's
  Device-level handle_message drops them) and routes them to the correct
  endpoint/cluster.

Patch 3 — zigpy.quirks.get_device
  Guarantees specific-model C4 quirks win over the (None, None) catch-all,
  even when device.manufacturer is None at startup.

Patch 4 — ControllerApplication.packet_received
  Intercepts broadcast C4 packets to sniff the model string and call
  custom_profile_packet_received on initialised devices.

_C4_MODEL_QUIRK_MAP is populated by each device module at import time via
  _C4_MODEL_QUIRK_MAP["model_string"] = QuirkClass
"""

import logging
import os
import sys
import time

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

import c4_helpers as C4
from c4_helpers import (
    C4_CLUSTER_ID,
    C4_IEEE_PREFIX,
    C4_PROFILE_BUTTON,
    C4_PROFILE_NETWORK,
    C4_PROFILE_OUTLET,
    C4_PROFILES,
    C4_ENDPOINT_DEFAULTS,
    _INVALID_MODELS,
    _c4_sniff_model,
    get_model_from_ieee,
)

_LOGGER = logging.getLogger(__name__)

_LOGGER.info("=== C4 QUIRK FILE LOADED (multi-device) ===")

# Populated at the bottom of each device module, e.g.:
#   from c4_hooks import _C4_MODEL_QUIRK_MAP
#   _C4_MODEL_QUIRK_MAP["C4-APD120"] = Control4APD120Dimmer
# ---------------------------------------------------------------------------
_C4_MODEL_QUIRK_MAP: dict = {}

# ---------------------------------------------------------------------------
# Patch 1: Auto-complete C4 endpoint interviews (skip Simple_Desc_req)
# ---------------------------------------------------------------------------
try:
    from zigpy.endpoint import Endpoint as _ZigpyEndpoint, Status as _EpStatus

    if not getattr(_ZigpyEndpoint, '_c4_interview_patch', False):
        _original_ep_initialize = _ZigpyEndpoint.initialize

        async def _c4_patched_ep_initialize(self):
            device_ieee = str(getattr(self.device, 'ieee', '')).lower()
            ep_id = self._endpoint_id

            if (
                device_ieee.startswith(C4_IEEE_PREFIX)
                and ep_id in C4_ENDPOINT_DEFAULTS
            ):
                defaults = C4_ENDPOINT_DEFAULTS[ep_id]
                _LOGGER.debug(
                    "C4: ep %s on %s — injecting defaults "
                    "(skipping Simple_Desc_req): profile=0x%04X clusters=%s",
                    ep_id, device_ieee,
                    defaults["profile_id"],
                    defaults["in_clusters"],
                )
                self.profile_id  = defaults["profile_id"]
                self.device_type = defaults["device_type"]
                for cluster_id in defaults.get("in_clusters", []):
                    self.add_input_cluster(cluster_id)
                for cluster_id in defaults.get("out_clusters", []):
                    self.add_output_cluster(cluster_id)
                self.status = _EpStatus.ZDO_INIT
                return

            return await _original_ep_initialize(self)

        _ZigpyEndpoint.initialize           = _c4_patched_ep_initialize
        _ZigpyEndpoint._c4_interview_patch  = True
        _LOGGER.info("C4: Installed endpoint interview patch")
    else:
        _LOGGER.debug("C4: Endpoint interview patch already installed")

except Exception as e:
    _LOGGER.error("C4: Failed to install interview patch: %s", e)


# ---------------------------------------------------------------------------
# Patch 2: Intercept C4 packets in Device.custom_profile_packet_received
# ---------------------------------------------------------------------------
try:
    from zigpy.device import Device as _ZigpyDevice

    if not getattr(_ZigpyDevice, '_c4_custom_profile_patch', False):
        _original_custom_profile = _ZigpyDevice.custom_profile_packet_received

        def _c4_patched_custom_profile(self, packet):
            device_ieee = str(getattr(self, 'ieee', '')).lower()

            if (
                packet.profile_id in C4_PROFILES
                and device_ieee.startswith(C4_IEEE_PREFIX)
            ):
                # Update last_seen so ZHA considers the device available.
                # Without this, ZHA never sees C4-profile traffic (it's
                # intercepted here) and all entities stay "unavailable".
                self.last_seen = time.time()

                msg = packet.data
                if hasattr(msg, 'serialize'):
                    msg = msg.serialize()
                elif not isinstance(msg, (bytes, bytearray)):
                    msg = bytes(msg)

                _LOGGER.debug(
                    "C4 intercept: profile=0x%04X cluster=0x%04X "
                    "src_ep=%s dst_ep=%s ieee=%s len=%d",
                    packet.profile_id, packet.cluster_id,
                    packet.src_ep, packet.dst_ep, device_ieee,
                    len(msg) if msg else 0,
                )

                if packet.profile_id == C4_PROFILE_BUTTON:
                    target_ep_id = 197
                elif packet.profile_id == C4_PROFILE_OUTLET:
                    target_ep_id = 198
                else:
                    for candidate in [packet.src_ep, 2, 196]:
                        if candidate in self.endpoints and candidate != 0:
                            target_ep_id = candidate
                            break
                    else:
                        target_ep_id = 196

                target_ep = self.endpoints.get(target_ep_id)
                if target_ep is not None:
                    in_clusters = getattr(target_ep, 'in_clusters', {})

                    # Prefer clusters marked _c4_custom_handler (handles
                    # reassigned cluster IDs, e.g. 0xFC42 for button clusters).
                    target_cluster = next(
                        (c for c in in_clusters.values()
                         if getattr(c, '_c4_custom_handler', False)),
                        None,
                    )
                    # Fall back to wire cluster ID
                    if target_cluster is None:
                        target_cluster = in_clusters.get(packet.cluster_id)

                    if target_cluster is not None:
                        try:
                            target_cluster.handle_message(None, msg)
                            _LOGGER.debug(
                                "C4 intercept: handle_message succeeded on ep %s cluster %s",
                                target_ep_id, type(target_cluster).__name__,
                            )
                        except Exception as e2:
                            _LOGGER.warning(
                                "C4 intercept: handle_message failed on ep %s: %s",
                                target_ep_id, e2,
                            )
                    else:
                        _LOGGER.debug(
                            "C4 intercept: no marked or matching cluster on ep %s",
                            target_ep_id,
                        )
                else:
                    _LOGGER.debug(
                        "C4 intercept: no ep %s on %s", target_ep_id, device_ieee
                    )

                try:
                    self.listener_event(
                        "handle_message",
                        packet.profile_id, packet.cluster_id,
                        packet.src_ep, packet.dst_ep,
                        msg,
                    )
                except Exception:
                    pass
                return

            return _original_custom_profile(self, packet)

        _ZigpyDevice.custom_profile_packet_received = _c4_patched_custom_profile
        _ZigpyDevice._c4_custom_profile_patch       = True
        _LOGGER.info("C4: Installed custom_profile_packet_received patch")
    else:
        _LOGGER.debug("C4: custom_profile_packet_received patch already installed")

except Exception as e:
    _LOGGER.error("C4: Failed to install custom_profile patch: %s", e)


# ---------------------------------------------------------------------------
# Patch 3: zigpy.quirks.get_device — guarantee specific-model C4 quirks win
# ---------------------------------------------------------------------------
try:
    import zigpy.quirks as _zq

    if not getattr(_zq, '_c4_get_device_patch', False):
        _orig_zq_get_device = _zq.get_device

        # Locate the registry object (name varies across zigpy versions)
        _ZQ_REGISTRY = None
        for _reg_name in ('_DEVICE_REGISTRY', 'DEVICE_REGISTRY', '_registry',
                          'registry', '_devices'):
            if hasattr(_zq, _reg_name):
                _ZQ_REGISTRY = getattr(_zq, _reg_name)
                _LOGGER.debug(
                    "C4: found quirk registry as zigpy.quirks.%s (type=%s)",
                    _reg_name, type(_ZQ_REGISTRY).__name__,
                )
                break

        if _ZQ_REGISTRY is None:
            import inspect
            for obj in (
                list((_orig_zq_get_device.__defaults__ or [])) +
                list((_orig_zq_get_device.__kwdefaults__ or {}).values())
            ):
                if isinstance(obj, dict):
                    _ZQ_REGISTRY = obj
                    _LOGGER.debug(
                        "C4: found quirk registry via get_device defaults (type=dict)"
                    )
                    break

        if _ZQ_REGISTRY is None:
            raise RuntimeError(
                "Cannot locate zigpy quirks registry — "
                "tried _DEVICE_REGISTRY, DEVICE_REGISTRY, _registry, "
                "registry, _devices, and get_device defaults"
            )

        # Find the internal dict inside a DeviceRegistry wrapper
        _ZQ_REGISTRY_DICT = None
        _registry_obj = _ZQ_REGISTRY
        for _inner_name in ('_registry', '_devices', '_quirks', 'registry',
                            'devices', 'quirks', '__dict__'):
            candidate = getattr(_registry_obj, _inner_name, None)
            if isinstance(candidate, dict) and candidate:
                _ZQ_REGISTRY_DICT = candidate
                _LOGGER.debug(
                    "C4: DeviceRegistry internal dict found at .%s (keys sample: %s)",
                    _inner_name, list(candidate.keys())[:3],
                )
                break

        if _ZQ_REGISTRY_DICT is None:
            attrs = {k: type(v).__name__ for k, v in vars(_registry_obj).items()}
            _LOGGER.warning("C4: DeviceRegistry attributes: %s", attrs)
            raise RuntimeError(
                "Cannot find internal dict inside DeviceRegistry — "
                "see attribute dump above"
            )

        def _c4_patched_get_device(device, registry=_ZQ_REGISTRY):
            ieee  = str(getattr(device, 'ieee',  '')).lower()
            model = getattr(device, 'model',        None)
            manuf = getattr(device, 'manufacturer', None)

            if ieee.startswith(C4_IEEE_PREFIX):
                if not model or model in _INVALID_MODELS:
                    model = get_model_from_ieee(ieee)
                    if model is not None:
                        manuf = "Control4"
                        _LOGGER.debug(
                            "C4 get_device: ieee=%s model=%r (from cache)",
                            ieee, model,
                        )
                    else:
                        _LOGGER.debug(
                            "C4 get_device: ieee=%s model missing or uninformative", ieee
                        )

                if model and isinstance(model, str):
                    quirk_cls = _C4_MODEL_QUIRK_MAP.get(model)
                    if quirk_cls is not None:
                        device.model = model
                        device.manufacturer = manuf or "Control4"
                        _LOGGER.debug(
                            "C4 get_device: direct-instantiating %s for "
                            "model=%r manuf=%r ieee=%s",
                            quirk_cls.__name__, model, manuf, ieee,
                        )
                        try:
                            return quirk_cls(
                                device._application,
                                device.ieee,
                                device.nwk,
                                device,
                            )
                        except Exception as exc:
                            _LOGGER.error(
                                "C4 get_device: failed to instantiate %s: %s — "
                                "falling back to default get_device",
                                quirk_cls.__name__, exc,
                            )

            return _orig_zq_get_device(device, registry)

        _zq.get_device           = _c4_patched_get_device
        _zq._c4_get_device_patch = True
        _LOGGER.info("C4: patched zigpy.quirks.get_device")
    else:
        _LOGGER.debug("C4: get_device patch already installed")

except Exception as e:
    _LOGGER.error("C4: Failed to patch zigpy.quirks.get_device: %s", e)


# ---------------------------------------------------------------------------
# Patch 4: ControllerApplication.packet_received — broadcast intercept
# ---------------------------------------------------------------------------
try:
    from zigpy.application import ControllerApplication as _ZigpyApp

    if not getattr(_ZigpyApp, '_c4_broadcast_patch', False):
        _original_packet_received = _ZigpyApp.packet_received

        def _c4_patched_packet_received(self, packet):
            if packet.profile_id in C4_PROFILES and packet.src_ep != 0:
                try:
                    device = self.get_device_with_address(packet.src)
                    if device is not None:
                        ieee = str(getattr(device, 'ieee', '')).lower()
                        if ieee.startswith(C4_IEEE_PREFIX):
                            if packet.profile_id == C4_PROFILE_NETWORK:
                                msg = packet.data
                                if hasattr(msg, 'serialize'):
                                    msg = msg.serialize()
                                elif not isinstance(msg, (bytes, bytearray)):
                                    msg = bytes(msg)
                                # Strip C4 app header if present
                                _C4_APP_HDR_PROFILES = (
                                    b'\x5d\xc2', b'\x5c\xc2', b'\x5e\xc2'
                                )
                                inner = (
                                    msg[8:]
                                    if len(msg) >= 8
                                    and msg[4:6] in _C4_APP_HDR_PROFILES
                                    else msg
                                )
                                _c4_sniff_model(device, inner)

                        if device.is_initialized:
                            _LOGGER.debug(
                                "C4 broadcast intercept: profile=0x%04X "
                                "src_ep=%s nwk=0x%04X",
                                packet.profile_id,
                                packet.src_ep,
                                packet.src.address,
                            )
                            device.custom_profile_packet_received(packet)
                        return   # always return for C4 — never call _original

                except Exception as exc:
                    _LOGGER.warning(
                        "C4 broadcast intercept: lookup failed: %s", exc
                    )

            return _original_packet_received(self, packet)

        _ZigpyApp.packet_received       = _c4_patched_packet_received
        _ZigpyApp._c4_broadcast_patch   = True
        _LOGGER.info("C4: Installed broadcast packet intercept patch")
    else:
        _LOGGER.debug("C4: Broadcast packet intercept patch already installed")

except Exception as e:
    _LOGGER.error("C4: Failed to install broadcast patch: %s", e)

# ---------------------------------------------------------------------------
# Device module imports — must come AFTER all patches are installed above.
#
# ZHA auto-imports every .py file in the custom_zha_quirks directory, but
# filesystem scan order is not guaranteed.  get_device (Patch 3) is called
# during device initialization which can happen before all files are scanned.
# Importing every device module here guarantees _C4_MODEL_QUIRK_MAP is fully
# populated the moment Patch 3 becomes active.
#
# Each module's bottom-level registration line runs exactly once regardless
# of how many times the module is imported (Python's module cache prevents
# re-execution), so there is no double-registration risk.
# ---------------------------------------------------------------------------

try:
    import control4_fan              # registers "C4-4SF120"
except Exception as _e:
    _LOGGER.error("C4: failed to import control4_fan — %s", _e)

try:
    import control4_z2io_zp          # registers "C4-Z2IO-ZP"
except Exception as _e:
    _LOGGER.error("C4: failed to import control4_z2io_zp — %s", _e)

try:
    import control4_remote           # registers "C4-SR260"
except Exception as _e:
    _LOGGER.error("C4: failed to import control4_remote — %s", _e)

# Other device modules self-register when they import c4_hooks (this file),
# so they are always loaded before any get_device call — no explicit import
# needed for control4_dimmer, control4_switch, control4_outlet, etc.