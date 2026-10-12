"""The 0x000C/0x0001 read and the 0xC25E firmware query, against captured bytes.

From a capture of a director-paired SR-260 rejoining after a battery pull, and
the director's answers. `c4_checkin` has no zigpy import, so it is driven
directly.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

from c4_checkin import (  # noqa: E402
    CONFIG_REPLY_DST_EP,
    CONFIG_REPLY_SRC_EP,
    FIRMWARE_REPLY_EP,
    checkin_tsn,
    config_read_reply,
    firmware_reply_frame,
    is_config_read,
    is_firmware_query,
    read_attr_ids,
)

CONFIG_READ = bytes.fromhex("008b000c000100")  # EP 2
DIRECTOR_CONFIG_REPLY = bytes.fromhex("101a010c00002014010000212301")  # 196 -> 1
IDENTITY_READ = bytes.fromhex("003100080009000a00")  # EP 2
FW_QUERY = bytes.fromhex("1991000300200101004206") + b"2.2.50"  # 0xC25E
DIRECTOR_FW_REPLY = bytes.fromhex("011b0100002001")  # EP 240 -> 240


def test_config_read_is_told_apart_from_the_identity_read():
    """Test config read is told apart from the identity read."""
    assert read_attr_ids(CONFIG_READ[3:]) == [0x000C, 0x0001]
    assert is_config_read(read_attr_ids(CONFIG_READ[3:]))
    assert read_attr_ids(IDENTITY_READ[3:]) == [0x0008, 0x0009, 0x000A]
    assert not is_config_read(read_attr_ids(IDENTITY_READ[3:]))


def test_config_reply_matches_the_director_apart_from_the_channel():
    """Test config reply matches the director apart from the channel."""
    ids = read_attr_ids(CONFIG_READ[3:])
    # With the director's 0x14 the bytes are the director's exactly.
    assert config_read_reply(0x1A, ids, 0x14) == DIRECTOR_CONFIG_REPLY
    # Any other coordinator answers with its own channel.
    assert config_read_reply(0x8B, ids, 25)[7] == 25
    assert (CONFIG_REPLY_SRC_EP, CONFIG_REPLY_DST_EP) == (196, 1)


def test_unknown_attribute_is_unsupported():
    """Test unknown attribute is unsupported."""
    assert config_read_reply(1, [0x000C, 0x0042], 25) == bytes.fromhex(
        "1001010c00002019420086"
    )


def test_firmware_query_and_reply():
    """Test firmware query and reply."""
    assert is_firmware_query(FW_QUERY)
    assert not is_firmware_query(CONFIG_READ)
    assert checkin_tsn(FW_QUERY) is None  # never mistaken for a check-in
    assert firmware_reply_frame(0x1B) == DIRECTOR_FW_REPLY
    assert FIRMWARE_REPLY_EP == 240


def test_sr260_quirk_wires_both_answers():
    """Test sr260 quirk wires both answers."""
    src = (QUIRKS / "control4_remote.py").read_text()
    assert "self._c4_answer_config_read(bytes(args))" in src
    assert "_c4_sr260_answer_firmware_query(self, packet)" in src
