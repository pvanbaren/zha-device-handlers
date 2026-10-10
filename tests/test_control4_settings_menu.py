"""The SR-260 List > Settings menu, checked against the director capture it copies.

`c4_settings_menu` has no zigpy import, so it is driven directly. The wire
regexes in `c4_helpers` are read out of the source, so these tests do not
need zigpy.

Source: a director-paired SR-260 walked through List > Settings.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

from c4_settings_menu import (  # noqa: E402
    CHOICES,
    GLYPH_CHECKED,
    GLYPH_LEAF,
    GLYPH_SUBMENU,
    SLIDER_DISPLAY,
    SLIDER_KEYPAD,
    TEXT_COLORS,
    SettingsMenu,
)


def _menu(settings=None, battery=None, firmware="2.2.50"):
    return SettingsMenu(
        settings if settings is not None else {},
        about=lambda: [("Firmware version:", firmware)],
        battery=lambda: battery,
    )


def _labels(screen):
    return [s[1:] for s in screen.items]


def _checked(screen):
    return [s[1:] for s in screen.items if s.startswith(GLYPH_CHECKED)]


def test_root_and_config_match_the_director():
    """Test root and config match the director."""
    menu = _menu()
    root = menu.open()
    assert (root.kind, root.title, _labels(root)) == (
        "list",
        "Settings",
        ["Config", "About"],
    )
    config = menu.select(0)
    assert config.title == "Config"
    # The director's Config list, minus Factory Defaults, with Recharge Station
    # renamed for what it does.
    assert _labels(config) == [
        "Display Brightness",
        "Keypad Brightness",
        "Motion Detect",
        "Light Sensor",
        "Text Color",
        "Battery Level",
        "Battery Type",
    ]
    assert all(s.startswith(GLYPH_SUBMENU) for s in config.items)


def test_no_factory_defaults_anywhere():
    """Test no factory defaults anywhere."""
    menu = _menu({"wom": "01", "ls": "00", "bt": "01", "tc": "7b"})
    seen = []

    def walk(screen):
        if screen is None or screen.kind != "list":
            return
        seen.extend(_labels(screen))
        for i, item in enumerate(screen.items):
            if item.startswith(GLYPH_SUBMENU):
                walk(menu.select(i))
                menu.back()

    walk(menu.open())
    assert "Factory Defaults" not in seen
    assert "Ok" not in seen


def test_motion_detect_checks_current_and_writes_measured_values():
    """Test motion detect checks current and writes measured values."""
    menu = _menu({"wom": "01"})
    menu.open()
    menu.select(0)
    screen = menu.select(2)
    # Remote order is Off, Low, Medium, High; wom 01 is High (not strength order).
    assert _labels(screen) == ["Off", "Low", "Medium", "High"]
    assert _checked(screen) == ["High"]
    assert screen.selected == 3
    after = menu.select(0)
    assert after.write == ("wom", "00")
    assert _checked(after) == ["Off"]
    assert after.title == "Motion Detect"  # stays on the list, as the director does


def test_battery_type_is_bt():
    """Test battery type is bt."""
    menu = _menu({"bt": "00"})
    menu.open()
    menu.select(0)
    screen = menu.select(6)
    assert _labels(screen) == ["Rechargeable", "AA Batteries"]
    assert _checked(screen) == ["AA Batteries"]
    assert menu.select(0).write == ("bt", "01")


def test_text_color_two_levels_and_back_keeps_cursor():
    """Test text color two levels and back keeps cursor."""
    menu = _menu({"tc": "7b"})
    menu.open()
    menu.select(0)
    groups = menu.select(4)
    assert _labels(groups) == ["Purple", "Orange", "Blue", "Green", "Red", "Grey"]
    blue = menu.select(2)
    assert _labels(blue) == ["Cerulean", "Blue", "Sky Blue"]
    assert _checked(blue) == ["Sky Blue"]
    assert menu.select(0).write == ("tc", "02")
    back = menu.back()
    assert back.title == "Text Color" and back.selected == 2
    back = menu.back()
    assert back.title == "Config" and back.selected == 4


def test_back_from_root_closes():
    """Test back from root closes."""
    menu = _menu()
    menu.open()
    assert menu.back().kind == "close"


def test_sliders_use_measured_ids_and_resume_on_their_row():
    """Test sliders use measured ids and resume on their row."""
    menu = _menu()
    menu.open()
    menu.select(0)
    display = menu.select(0)
    assert (
        (display.kind, display.slider_id)
        == ("slider", SLIDER_DISPLAY)
        == ("slider", 0x00)
    )
    keypad = menu.select(1)
    assert (
        (keypad.kind, keypad.slider_id) == ("slider", SLIDER_KEYPAD) == ("slider", 0x01)
    )
    resumed = menu.resume("Keypad Brightness")
    assert resumed.title == "Config" and resumed.selected == 1


def test_battery_level_gauge_or_placeholder():
    """Test battery level gauge or placeholder."""
    menu = _menu(battery=None)
    menu.open()
    menu.select(0)
    pending = menu.select(5)
    assert (pending.kind, _labels(pending)) == ("list", ["Not reported yet"])
    assert menu.select(0) is None
    assert menu.back().title == "Config"
    menu = _menu(battery=91)
    menu.open()
    menu.select(0)
    gauge = menu.select(5)
    assert (gauge.kind, gauge.value) == ("gauge", 91)


def test_about_reads_firmware_late():
    """Test about reads firmware late."""
    rows = {"fw": "unknown"}
    menu = SettingsMenu(
        {}, about=lambda: [("Firmware version:", rows["fw"])], battery=lambda: None
    )
    menu.open()
    rows["fw"] = "2.2.50"  # the c4.sy.fwv reply lands after the menu opened
    about = menu.select(1)
    assert _labels(about) == ["Firmware version:", "  2.2.50"]
    assert all(s.startswith(GLYPH_LEAF) for s in about.items)
    assert menu.select(0) is None  # info rows do nothing


def test_every_written_value_passes_the_frame_validator():
    """Test every written value passes the frame validator."""
    src = (QUIRKS / "c4_helpers.py").read_text()
    value_re = re.compile(
        re.search(r'_C4_SETTING_VALUE = re\.compile\(r"([^"]+)"\)', src).group(1)
    )
    values = [v for _verb, opts in CHOICES.values() for _l, v in opts]
    values += [v for _g, colors in TEXT_COLORS for _l, v in colors]
    for value in values:
        assert value_re.fullmatch(value), value


def test_text_colors_match_the_composer_bytes():
    """Test text colors match the composer bytes."""
    colors = {label: v for _g, cs in TEXT_COLORS for label, v in cs}
    assert len(colors) == 19
    assert colors["Sky Blue"] == "7b" and colors["Manatee"] == "92"
    assert colors["Royal Purple"] == "66" and colors["Wild Watermelon"] == "ed"


def _reply_regex():
    src = (QUIRKS / "c4_helpers.py").read_text()
    pat = re.search(r'_C4_SETTING_REPLY = re\.compile\(\s*r"([^"]+)"\s*\)', src)
    assert pat, "reply regex not found"
    return re.compile(pat.group(1))


def test_reply_regex_reads_captured_replies():
    """Test reply regex reads captured replies."""
    rx = _reply_regex()
    m = rx.fullmatch("0r3f44 000 c4.zr.wom 01")
    assert m.group(2, 4, 5) == ("000", "wom", " 01")
    assert rx.fullmatch("0r42f6 000 c4.zr.st 0f 0f").group(5) == " 0f 0f"  # SR-250
    assert rx.fullmatch("0r4359 000 c4.sy.fwv 03.26.17").group(4, 5) == (
        "fwv",
        " 03.26.17",
    )
    assert rx.fullmatch("0r4063 e00").group(2, 4) == ("e00", None)  # refused bt write
    assert (
        rx.fullmatch('0ra0a7 000 "\x01Media Player"') is None
    )  # a gi answer, not ours
    src = (QUIRKS / "c4_helpers.py").read_text()
    assert '.replace("\\x00", "")' in src  # live fwv reply: "2.2.50\r\n\x00\x00"


def test_battery_regex_reads_a_captured_status_report():
    """Test battery regex reads a captured status report."""
    src = (QUIRKS / "c4_helpers.py").read_text()
    pat = re.search(r'_C4_STATUS_BATTERY = re\.compile\(\s*rb"([^"]+)"', src).group(1)
    rx = re.compile(pat.encode("latin-1"), re.DOTALL)  # re parses the \x escapes
    # SR-260 -> coordinator, profile 0xC25D, a captured status report.
    frame = (
        b"\x1d@\x10\xa9\x03\x12\x00A\x02\x00\x00\x00\x00 \x03\x01\x00!#\x01"
        b"\x02\x00!#\x01\x03\x00 \x00\x0c\x00 \x14\x13\x00(\xc9\x14\x00 "
        b"\xfe\x15\x00(U"
    )
    assert rx.search(frame).group(1) == b"U"  # 0x55 = 85 %


def test_list_menu_reaches_settings():
    # show_settings is command 7; List is button 0x04 -> EP 104.
    """Test list menu reaches settings."""
    src = (QUIRKS / "c4_display_cluster.py").read_text()
    assert re.search(r"show_settings = ZCLCommandDef\(\s*id=0x07", src)
    helpers = (QUIRKS / "c4_helpers.py").read_text()
    assert re.search(r'0x04: "list"', helpers), "List is button 0x04 -> EP 104"


def test_battery_gauge_reads_bl_on_open():
    # The director reads `0g c4.zr.bl` and draws exactly that on its gauge
    # (`bl 5b` -> `sc 64 00 64 5b`), so bl must be refreshed when List opens.
    """Test battery gauge reads bl on open."""
    from c4_settings_menu import READ_ON_OPEN

    assert "bl" in READ_ON_OPEN
    src = (QUIRKS / "c4_display_cluster.py").read_text()
    assert 'int(getattr(device, "_c4_settings", {})["bl"], 16)' in src
