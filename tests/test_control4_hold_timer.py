"""A held SR-260 or SR-250 key reports the remote's own hold timer.

The remote re-sends `c4.zr.bh` about every 500 ms; its 4th field is the time
held in ms. Hold events pass it on as `hold_ms` so an automation can
accelerate a held volume key the way a director does.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

from c4_button_cluster import C4RemoteButtonCluster  # noqa: E402
from c4_helpers import C4_BUTTON_CLUSTER_ID, SR260_BUTTON_EP_MAP  # noqa: E402

VOLUME_UP = 0x0C


def _remote():
    """A button cluster wired to one virtual EP, recording what it fires."""
    fired = []
    ep_id = SR260_BUTTON_EP_MAP[VOLUME_UP]
    button = SimpleNamespace(listener_event=lambda *args: fired.append(args))
    device = SimpleNamespace(
        endpoints={ep_id: SimpleNamespace(in_clusters={C4_BUTTON_CLUSTER_ID: button})}
    )
    cluster = object.__new__(C4RemoteButtonCluster)
    cluster._endpoint = SimpleNamespace(device=device)
    return cluster, fired


def test_a_hold_carries_the_hold_timer():
    """Test a hold carries the hold timer."""
    cluster, fired = _remote()
    cluster._handle_state_announcement(
        "c4.zr.bh", ["0c", "0000", "0000", "00000597"]
    )
    (_, action, args), = fired
    assert action == "remote_button_long_press"
    assert args["hold_ms"] == 1431


def test_a_press_carries_no_hold_timer():
    """Test a press carries no hold timer."""
    cluster, fired = _remote()
    cluster._handle_state_announcement(
        "c4.zr.bb", ["0c", "0000", "0000", "00000000"]
    )
    (_, action, args), = fired
    assert action == "remote_button_short_press"
    assert "hold_ms" not in args


def _sr250_module(monkeypatch):
    """Import control4_sr250 with c4_hooks stubbed out.

    Importing it at collection time registers the Control4 quirks (it pulls in
    c4_hooks, which imports its siblings) before test_quirks builds its list,
    and the real c4_hooks patches zigpy for every test that follows.
    """
    hooks = ModuleType("c4_hooks")
    hooks._C4_MODEL_QUIRK_MAP = {}
    monkeypatch.setitem(sys.modules, "c4_hooks", hooks)
    return importlib.import_module("control4_sr250")


def _sr250(module):
    """An SR-250 raw cluster recording the commands and params it emits."""
    emitted = []
    cluster = object.__new__(module.C4SR250RawCluster)
    cluster._emit = lambda command, params: emitted.append((command, params))
    return cluster, emitted


# The SR-250's bh has no hold-timer field, unlike the SR-260's.
VOL_UP = ["0d", "0000", "0000"]


def test_an_sr250_hold_is_timed_from_the_press(monkeypatch):
    """Test an SR-250 hold is timed from the press."""
    control4_sr250 = _sr250_module(monkeypatch)
    clock = iter([100.0, 100.514, 101.027])
    monkeypatch.setattr(control4_sr250._time, "monotonic", lambda: next(clock))
    cluster, emitted = _sr250(control4_sr250)
    cluster._emit_button("down", "0d", "Vol+", "c4.zr.bb", VOL_UP)
    cluster._emit_button("hold", "0d", "Vol+", "c4.zr.bh", VOL_UP)
    cluster._emit_button("hold", "0d", "Vol+", "c4.zr.bh", VOL_UP)
    assert [(c, p.get("hold_ms")) for c, p in emitted] == [
        ("volume_up_press", None),
        ("volume_up_hold", 514),
        ("volume_up_hold", 1027),
    ]


def test_an_sr250_hold_after_a_lost_press_counts_from_one_tick(monkeypatch):
    """Test an SR-250 hold after a lost press counts from one tick."""
    control4_sr250 = _sr250_module(monkeypatch)
    monkeypatch.setattr(control4_sr250._time, "monotonic", lambda: 200.0)
    cluster, emitted = _sr250(control4_sr250)
    cluster._emit_button("down", "0d", "Vol+", "c4.zr.bb", VOL_UP)
    cluster._emit_button("up", "0d", "Vol+", "c4.zr.be", VOL_UP)
    cluster._emit_button("hold", "0d", "Vol+", "c4.zr.bh", VOL_UP)
    command, params = emitted[-1]
    assert command == "volume_up_hold"
    assert params["hold_ms"] == 500
