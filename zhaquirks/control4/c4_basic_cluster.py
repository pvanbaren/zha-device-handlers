"""C4BasicCluster — satisfies ZHA model/manufacturer reads from the device cache.

ZHA calls read_attributes(['model_identifier', 'manufacturer_name']) on the
Basic cluster during async_initialize on every HA reboot.  C4 devices do not
implement the ZCL Basic cluster, so those reads normally return empty and ZHA
loses the model string.

This cluster resolves model/manufacturer from (in priority order):
  0. _C4_IEEE_MODEL_MAP — populated by the broadcast sniffer patch
  1. device.model / device.manufacturer — zigpy DB, survives reboots
  2. ZCL attribute cache on EP 2 cluster 0x0001 — populated on first packet
  3. ZCL attribute cache on EP 196 cluster 0x0001 — fallback
  4. Live read of attr 0x0007 from the device (5-second timeout)
"""

import asyncio
import logging
import os
import sys

_QUIRK_DIR = os.path.dirname(os.path.abspath(__file__))
if _QUIRK_DIR not in sys.path:
    sys.path.insert(0, _QUIRK_DIR)

from zigpy.quirks import CustomCluster
from zigpy.zcl import foundation
from zigpy.zcl.foundation import Status as ZCLStatus
from zigpy.zcl.clusters.general import Basic

import c4_helpers as C4
from c4_helpers import (
    C4_ATTR_MODEL,
    C4_CLUSTER_ID,
    _INVALID_MODELS,
    _c4_persist_device,
    _sync_ep1_model,
    get_model_from_ieee,
    set_model_for_ieee,
)

_LOGGER = logging.getLogger(__name__)


class C4BasicCluster(CustomCluster, Basic):
    """Basic cluster with C4-aware model/manufacturer resolution."""

    _CONSTANT_ATTRIBUTES = {
        Basic.AttributeDefs.manufacturer.id: "Control4",
    }

    _ATTR_MANUF = Basic.AttributeDefs.manufacturer.id  # 0x0004
    _ATTR_MODEL = Basic.AttributeDefs.model.id          # 0x0005
    _C4_INFO_ATTRS = frozenset({_ATTR_MANUF, _ATTR_MODEL})

    # ------------------------------------------------------------------
    # Startup initialisation
    # ------------------------------------------------------------------

    async def async_initialize(self, from_cache=False):
        """Seed Basic cluster attribute cache on every HA startup."""
        model, manuf = self._resolve_c4_identity()
        _LOGGER.info(
            "C4 Basic async_initialize: ieee=%s from_cache=%s "
            "resolved model=%r manuf=%r",
            self.endpoint.device.ieee, from_cache, model, manuf,
        )

        if not model or model in _INVALID_MODELS:
            _LOGGER.info(
                "C4 Basic async_initialize: model not found in cache, fetching from device"
            )
            model, manuf = await self._fetch_c4_model()

        if model:
            self._update_attribute(self._ATTR_MODEL, model)
            self._update_attribute(self._ATTR_MANUF, manuf or "Control4")
            _c4_persist_device(self.endpoint.device, "c4_basic_async_init")

        await super().async_initialize(from_cache=from_cache)

    # ------------------------------------------------------------------
    # Attribute reads
    # ------------------------------------------------------------------

    async def read_attributes(
        self,
        attributes,
        allow_cache: bool = False,
        only_cache: bool = False,
        manufacturer=None,
    ):
        # Split attributes into ones we handle ourselves vs pass-through.
        # Preserving the original key type (str or int) is critical — ZHA
        # passes string names and expects string keys back.
        c4_map: dict = {}
        passthrough: list = []

        for attr in attributes:
            if isinstance(attr, str):
                try:
                    attr_id = self.find_attribute(attr).id
                except KeyError:
                    attr_id = None
                key = attr
            else:
                attr_id = int(attr)
                key = attr_id

            if attr_id in self._C4_INFO_ATTRS:
                c4_map[attr_id] = key
            else:
                passthrough.append(attr)

        success: dict = {}
        failure: dict = {}

        if passthrough:
            success, failure = await super().read_attributes(
                passthrough,
                allow_cache=allow_cache,
                only_cache=only_cache,
                manufacturer=manufacturer,
            )

        if c4_map:
            model, manuf = self._resolve_c4_identity()

            _LOGGER.debug(
                "C4 Basic read_attributes: ieee=%s raw device.model=%r "
                "device.manufacturer=%r → resolved model=%r manuf=%r",
                self.endpoint.device.ieee,
                self.endpoint.device.model,
                self.endpoint.device.manufacturer,
                model, manuf,
            )

            if not model and not only_cache:
                model, manuf = await self._fetch_c4_model()

            _LOGGER.debug("C4 Basic: resolved identity model=%r manuf=%r", model, manuf)

            if self._ATTR_MODEL in c4_map:
                k = c4_map[self._ATTR_MODEL]
                if model:
                    self._update_attribute(self._ATTR_MODEL, model)
                    success[k] = model
                else:
                    failure[k] = foundation.Status.UNSUP_ATTRIBUTE

            if self._ATTR_MANUF in c4_map:
                k = c4_map[self._ATTR_MANUF]
                m = manuf or "Control4"
                self._update_attribute(self._ATTR_MANUF, m)
                success[k] = m

        return success, failure

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_c4_identity(self):
        """Return (model, manufacturer) from the best available source."""
        device = self.endpoint.device
        _LOGGER.debug(
            "C4 Basic: resolving identity for %s nwk=0x%04X",
            device.ieee, device.nwk,
        )

        # 0 — broadcast sniffer cache (most up-to-date)
        model = get_model_from_ieee(str(device.ieee))
        if model and model not in _INVALID_MODELS:
            _LOGGER.debug(
                "C4 Basic: resolved model=%r from sniffer cache for %s",
                model, device.ieee,
            )
            return model, "Control4"

        # 1 — zigpy device DB (survives reboots)
        model = device.model
        manuf = device.manufacturer
        if model and model not in _INVALID_MODELS:
            _LOGGER.debug(
                "C4 Basic: resolved model=%r from device DB for %s",
                model, device.ieee,
            )
            return model, manuf

        # 2 / 3 — ZCL attribute cache on EP 2 then EP 196
        for ep_id in (2, 196):
            ep = device.endpoints.get(ep_id)
            if ep is None:
                continue
            cfg = ep.in_clusters.get(C4_CLUSTER_ID)
            if cfg is None:
                continue
            raw = cfg.get(C4_ATTR_MODEL)
            if raw and isinstance(raw, str):
                parts = raw.split(":")
                m = parts[2] if len(parts) >= 3 else raw
                _LOGGER.debug(
                    "C4 Basic: resolved model=%r from ZCL cache ep%d for %s",
                    m, ep_id, device.ieee,
                )
                return m, "Control4"

        return None, None

    async def _fetch_c4_model(self):
        """Read attr 0x0007 directly from C4 EP 2 cluster 0x0001 (5-second timeout).

        Falls back to EP 196.  Returns (model_str, 'Control4') or (None, None).
        """
        _LOGGER.debug("C4 Basic: fetching model from device (Control4 query)")
        device = self.endpoint.device
        for ep_id in (2, 196):
            ep = device.endpoints.get(ep_id)
            if ep is None:
                continue
            c4_cluster = ep.in_clusters.get(C4_CLUSTER_ID)
            if c4_cluster is None:
                continue
            try:
                _LOGGER.debug(
                    "C4 Basic: reading attr 0x0007 from ep%d cluster 0x%04X",
                    ep_id, C4_CLUSTER_ID,
                )
                result, _ = await asyncio.wait_for(
                    c4_cluster.read_attributes([C4_ATTR_MODEL], allow_cache=False),
                    timeout=5.0,
                )
                raw = result.get(C4_ATTR_MODEL)
                if raw and isinstance(raw, str):
                    parts = raw.split(':', 2)
                    model = parts[2] if len(parts) >= 3 else raw
                    device.model = model
                    device.manufacturer = "Control4"
                    _sync_ep1_model(device, model, "c4_basic_fetch")
                    try:
                        device.application.listener_event("device_updated", device)
                    except Exception:
                        pass
                    _LOGGER.info(
                        "C4 Basic: fetched model=%r from ep%d attr 0x0007",
                        model, ep_id,
                    )
                    return model, "Control4"
            except asyncio.TimeoutError:
                _LOGGER.debug("C4 Basic: model fetch from ep%d timed out", ep_id)
            except Exception as e:
                _LOGGER.debug("C4 Basic: model fetch from ep%d failed: %s", ep_id, e)
        return None, None
