"""Check-in recognition for both remote models, against captured bytes.

`c4_checkin` has no zigpy import, so it is driven directly.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

from c4_checkin import CHECKIN_ACK_EP, checkin_ack_frame, checkin_tsn  # noqa: E402

# Director-paired SR-260 -> director, EP 2, profile 0xC25D; the director
# answered `01 fa 04` on EP 196.
SR260_CHECKIN = (
    b"\x1d@\x10\xa9\x03\x12\x00A\x02\x00\x00\x00\x00 \x03\x01\x00!#\x01"
    b"\x02\x00!#\x01\x03\x00 \x00\x0c\x00 \x14\x13\x00(\xc9\x14\x00 "
    b"\xfe\x15\x00(U"
)


def test_sr260_manufacturer_specific_checkin_is_recognised():
    """Test sr260 manufacturer specific checkin is recognised."""
    assert checkin_tsn(SR260_CHECKIN) == 0xA9


def test_sr250_plain_checkin_is_recognised():
    # Cluster-specific, no manufacturer bit: <fc> <tsn> 03 ...
    """Test sr250 plain checkin is recognised."""
    assert checkin_tsn(bytes((0x19, 0x54, 0x03, 0x00))) == 0x54


def test_other_frames_are_not_checkins():
    """Test other frames are not checkins."""
    assert checkin_tsn(bytes((0x18, 0x54, 0x03))) is None  # profile-wide
    assert checkin_tsn(bytes((0x19, 0x54, 0x02))) is None  # cmd 0x02
    assert checkin_tsn(b"\x1d@\x10\xa9\x02") is None  # mfr-specific cmd 0x02
    assert checkin_tsn(b"") is None


def test_ack_matches_the_director():
    """Test ack matches the director."""
    assert checkin_ack_frame(0xFA) == bytes.fromhex("01fa04")
    assert checkin_ack_frame(0x1FA) == bytes.fromhex("01fa04")
    assert CHECKIN_ACK_EP == 196


def test_sr260_quirk_uses_the_acking_cluster_on_ep2():
    """Test sr260 quirk uses the acking cluster on ep2."""
    src = (QUIRKS / "control4_remote.py").read_text()
    replacement = src[src.index("replacement = {") :]
    ep2 = re.search(r"\n\s+2: \{(.*?)\}", replacement, re.S).group(1)
    assert "C4SR260ConfigCluster" in ep2


def test_heartbeat_is_throttled_to_one_per_interval():
    """Test heartbeat is throttled to one per interval."""
    from c4_checkin import HEARTBEAT_INTERVAL_S, heartbeat_due

    assert heartbeat_due(None, 0.0)
    assert not heartbeat_due(100.0, 100.0 + HEARTBEAT_INTERVAL_S - 1)
    assert heartbeat_due(100.0, 100.0 + HEARTBEAT_INTERVAL_S)
