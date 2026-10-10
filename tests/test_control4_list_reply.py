"""The c4.ln.gi reply, byte for byte, in both models' measured dialects."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

import c4_helpers  # noqa: E402
from c4_helpers import _c4_send_list_items_response, c4_list_dialect  # noqa: E402


class _Device:
    def __init__(self, model):
        self.model = model
        self.ieee = "00:0f:ff:00:00:12:34:56"
        self.sent = []

    def get_sequence(self):
        return 1

    async def request(self, **kwargs):
        self.sent.append(kwargs)


def _reply(model, items, seq="081a"):
    device = _Device(model)
    dialect = c4_list_dialect(device)
    asyncio.run(
        _c4_send_list_items_response(
            device,
            seq,
            items,
            icon=dialect["item_icon"],
            endpoint=dialect["reply_ep"],
            form=dialect["gi_form"],
        )
    )
    assert len(device.sent) == 1
    return device.sent[0]


def _payload(frame):
    return bytes(frame["data"])


def test_sr260_status_form_with_glyph():
    """Test sr260 status form with glyph."""
    sent = _reply("C4-SR260", ["Source A", "Source B"])
    assert _payload(sent) == b'0r081a 000 "\x01Source A" "\x01Source B"\r\n'
    assert (sent["src_ep"], sent["dst_ep"]) == (1, 1)
    assert sent["profile"] == c4_helpers.C4_PROFILE_BUTTON


def test_sr250_count_form_without_glyph():
    """Test sr250 count form without glyph."""
    sent = _reply("C4-SR250B", ["Source A", "Source B", "Source C"])
    assert _payload(sent) == b'0r081a 000 0003 "Source A" "Source B" "Source C"\r\n'
    assert (sent["src_ep"], sent["dst_ep"]) == (1, 1)


def test_count_describes_the_frame_not_the_request():
    # Six long labels cannot fit in one ~70-byte frame; the count must say how
    # many went, so the remote pages for the rest.
    """Test count describes the frame not the request."""
    items = [f"A fairly long source name {i}" for i in range(6)]
    payload = _payload(_reply("C4-SR250B", items))
    sent_labels = payload.count(b'"') // 2
    assert 0 < sent_labels < 6
    assert payload.startswith(b"0r081a 000 %04x " % sent_labels)
    assert len(payload) <= 70


def test_unknown_model_falls_back_to_sr260():
    """Test unknown model falls back to sr260."""
    assert c4_list_dialect(_Device("C4-SOMETHING")) is c4_list_dialect(
        _Device("C4-SR260")
    )
