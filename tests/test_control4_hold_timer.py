"""A held SR-260 key reports the remote's own hold timer.

The remote re-sends `c4.zr.bh` about every 500 ms; its 4th field is the time
held in ms. Hold events pass it on as `hold_ms` so an automation can
accelerate a held volume key the way a director does.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

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
