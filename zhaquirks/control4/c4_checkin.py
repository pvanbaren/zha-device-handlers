"""Recognising a Control4 remote's EP-2 check-in, and the director's answer.

No zigpy import, so the tests can drive it directly.

A director answers every check-in with `01 <tsn> 04` on EP 196 -> 196,
profile 0xC25D, cluster 0x0001, about 10 ms later; the tsn is its own counter.
Left unanswered, a remote gives up after a few and rejoins.

The two models send the same command in different ZCL framing:

* SR-250: plain cluster-specific, `<fc> <tsn> 03 ...` (fc low bits 01, no
  manufacturer bit). Measured 530 of 530 acked.
* SR-260: manufacturer-specific, `1d 40 10 <tsn> 03 ...` - manufacturer code
  0x1040 sits between the frame control and the tsn. Measured off a
  director-paired SR-260 (report -> ack `01 fa 04` 38 ms later). A matcher
  that rejects the manufacturer bit leaves this form unanswered.
"""

CHECKIN_CMD = 0x03
CHECKIN_ACK_EP = 196
# The remote retransmits a report it believes was lost; answer each tsn once.
CHECKIN_DEDUP_S = 5.0


def checkin_tsn(raw: bytes):
    """Return the report's tsn if `raw` is a check-in in either framing, else None."""
    raw = bytes(raw or b"")
    if len(raw) < 3 or (raw[0] & 0x03) != 0x01:
        return None  # not cluster-specific
    if raw[0] & 0x04:
        # Manufacturer-specific: fc, mfr code (2 bytes LE), tsn, cmd.
        if len(raw) >= 5 and raw[4] == CHECKIN_CMD:
            return raw[3]
        return None
    return raw[1] if raw[2] == CHECKIN_CMD else None


def checkin_ack_frame(tsn: int) -> bytes:
    """Build the director's reply to a check-in, byte for byte."""
    return bytes((0x01, tsn & 0xFF, 0x04))


# -- Heartbeat ---------------------------------------------------------------
#
# A check-in is the only frame a remote sends while nobody touches it, so it
# is the liveness signal: ZHA's own availability check never marked a remote
# that had gone flat overnight unavailable. It surfaces as a `checkin`
# zha_event, throttled so each remote adds a logbook line every ten minutes
# rather than every minute.

HEARTBEAT_COMMAND = "checkin"
HEARTBEAT_INTERVAL_S = 600.0


def heartbeat_due(last, now: float) -> bool:
    """Return True when no heartbeat has gone out for HEARTBEAT_INTERVAL_S."""
    return last is None or now - last >= HEARTBEAT_INTERVAL_S


# -- The other questions a waking SR-260 asks --------------------------------
#
# Besides the identity read (attrs 0x0008/0x0009/0x000A), a remote that wakes
# or rejoins reads attrs 0x000C and 0x0001 on EP 2. A director answers
# `10 <tsn> 01 0c 00 00 20 14 01 00 00 21 23 01` from EP 196 to EP 1: 0x000C =
# 0x14 and 0x0001 = 0x0123, identical in every capture and for every remote,
# so they are the controller's values. 0x14 matched the director network's
# own channel in the captures; that reading is unconfirmed, so HA answers with
# its own channel rather than copy a value that could point the remote
# elsewhere. Answered with the identity reply instead, the remote keeps asking
# every 10-40 min; a director-paired remote asks only after a wake.

CONFIG_REPLY_SRC_EP = 196
CONFIG_REPLY_DST_EP = 1
ATTR_CHANNEL = 0x000C
ATTR_0001 = 0x0001
ATTR_0001_VALUE = 0x0123
IDENTITY_ATTRS = (0x0008, 0x0009, 0x000A)


def read_attr_ids(body: bytes) -> list[int]:
    """Attribute ids from a Read Attributes body (uint16 LE each)."""
    body = bytes(body or b"")
    return [
        int.from_bytes(body[i : i + 2], "little") for i in range(0, len(body) - 1, 2)
    ]


def is_config_read(attr_ids) -> bool:
    """Return True for the 0x000C/0x0001 read (not the identity read)."""
    ids = list(attr_ids or [])
    return (
        bool(ids)
        and not any(a in IDENTITY_ATTRS for a in ids)
        and any(a in (ATTR_CHANNEL, ATTR_0001) for a in ids)
    )


def config_read_reply(tsn: int, attr_ids, channel: int) -> bytes:
    """Build a director-style Read Attributes Response for the 0x000C/0x0001 read.

    Answers each asked attribute in order; any other id gets
    UNSUPPORTED_ATTRIBUTE (0x86). Frame control 0x10 is what the director sends.
    """
    out = bytearray((0x10, tsn & 0xFF, 0x01))
    for attr in attr_ids:
        out += int(attr).to_bytes(2, "little")
        if attr == ATTR_CHANNEL:
            out += bytes((0x00, 0x20, int(channel) & 0xFF))
        elif attr == ATTR_0001:
            out += bytes((0x00, 0x21)) + ATTR_0001_VALUE.to_bytes(2, "little")
        else:
            out += b"\x86"
    return bytes(out)


# The SR-260 also sends `19 <tsn> 00 03 00 20 01 01 00 42 06 "2.2.50"` on
# profile 0xC25E, cluster 0x0003 - attrs 0x0003 = 1 and 0x0001 = its firmware
# version - after every rejoin and about hourly. A director answers
# `01 <tsn> 01 00 00 20 01` on EP 240 -> 240 with its own tsn, the same bytes
# in every capture, and nothing follows.

FIRMWARE_PROFILE = 0xC25E
FIRMWARE_CLUSTER = 0x0003
FIRMWARE_REPLY_EP = 240


def is_firmware_query(raw: bytes) -> bool:
    """Return True for the remote's 0xC25E firmware-version query."""
    raw = bytes(raw or b"")
    return len(raw) >= 3 and raw[0] == 0x19 and raw[2] == 0x00


def firmware_reply_frame(tsn: int) -> bytes:
    """Build the director's answer to the 0xC25E query, byte for byte."""
    return bytes((0x01, tsn & 0xFF, 0x01, 0x00, 0x00, 0x20, 0x01))
