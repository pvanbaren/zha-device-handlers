"""The SR-260's on-remote Settings menu, rebuilt from a Control4 director capture.

A real director draws every screen of List > Settings itself: the remote only
renders lists, sliders and gauges it is told to. This module is that menu as
pure data plus a cursor, with no zigpy import, so the tests can drive it
directly. `C4SR260DisplayCluster` turns each `Screen` into wire frames.

Measured off a director-paired SR-260:

* Lists: items carry a glyph - 0x82 opens a submenu, 0x01 is a leaf, 0x03 is
  the leaf holding the current value (drawn as a check mark).
* Sliders: `c4.ln.slc 04 <id>`; the remote stores the level itself and reports
  `cc` while moving and `cs` (Select) or `cn` (Cancel) on exit. The director
  sends no `blb`/`kbl` afterwards, so neither do we.
* Gauge: `c4.ln.sc 64 00 64 <pct> "Battery Level"`, where <pct> is exactly
  the `c4.zr.bl` value the director read just before (bl is battery percent).
* A pick sends `c4.zr.<verb> <value>` and the director redraws the same list.

Factory Defaults is deliberately absent: nothing here should be able to wipe a
remote, and its Ok was never captured.
"""

from __future__ import annotations

from dataclasses import dataclass, field

GLYPH_SUBMENU = "\x82"
GLYPH_LEAF = "\x01"
GLYPH_CHECKED = "\x03"

SLIDER_DISPLAY = 0x00
SLIDER_KEYPAD = 0x01

# Composer's order. Each value is the RGB332 byte `c4.zr.tc` takes.
TEXT_COLORS = [
    ("Purple", [("Royal Purple", "66"), ("Blue Violet", "8b"), ("Wisteria", "d3")]),
    (
        "Orange",
        [
            ("Classic Amber", "fc"),
            ("Raw Sienna", "8c"),
            ("Orange", "f0"),
            ("Atomic Tangerine", "f9"),
        ],
    ),
    ("Blue", [("Cerulean", "02"), ("Blue", "03"), ("Sky Blue", "7b")]),
    ("Green", [("Fern", "10"), ("Screaming Green", "1c"), ("Sea Green", "9e")]),
    ("Red", [("Brick Red", "80"), ("Red", "e0"), ("Wild Watermelon", "ed")]),
    ("Grey", [("White", "bb"), ("Shadow", "6d"), ("Manatee", "92")]),
]

# Order as the remote lists them. wom is NOT ordered by strength.
CHOICES = {
    "Motion Detect": (
        "wom",
        [("Off", "00"), ("Low", "02"), ("Medium", "03"), ("High", "01")],
    ),
    "Light Sensor": ("ls", [("On", "01"), ("Off", "00")]),
    # bt tells the remote which battery is fitted; the director labels it
    # "Recharge Station", which read as "charging on/off" and misled us once.
    "Battery Type": ("bt", [("Rechargeable", "01"), ("AA Batteries", "00")]),
}

# Read when the Settings menu opens, so check marks and the battery gauge are
# current by the time a submenu is reached. Same verbs the director reads.
READ_ON_OPEN = ("wom", "ls", "bt", "tc", "bl")

CONFIG_ITEMS = [
    "Display Brightness",
    "Keypad Brightness",
    "Motion Detect",
    "Light Sensor",
    "Text Color",
    "Battery Level",
    "Battery Type",
]


@dataclass
class Screen:
    """What to put on the LCD next. `kind` picks the wire frame."""

    kind: str  # "list" | "slider" | "gauge" | "action" | "close"
    title: str = ""
    items: list[str] = field(default_factory=list)  # glyph-prefixed labels
    selected: int = 0
    slider_id: int = 0
    value: int = 0
    # A setting to write before drawing, as (verb, hex value).
    write: tuple[str, str] | None = None
    # kind "action": what HA should do, and where in the tree it was picked.
    action: str | None = None
    path: list[str] = field(default_factory=list)


def _norm(value):
    return (value or "").strip().lower()


class SettingsMenu:
    """One remote's walk through Settings. `path` names the open list."""

    def __init__(self, settings: dict, about, battery):
        """Wrap the remote's live settings cache and the About/battery readers."""
        # `settings` is the live cache of c4.zr values the remote reported, so
        # a write elsewhere shows up here without rebuilding the menu. `about`
        # (rows of (label, value)) and `battery` (percent or None) are
        # callables for the same reason: their replies land after the open.
        self.settings = settings
        self.about = about
        self.battery = battery
        self.path: list[str] = []

    # -- screens -------------------------------------------------------------

    def open(self) -> Screen:
        """Open the top Settings list."""
        self.path = ["Settings"]
        return self._screen()

    def _items(self) -> tuple[str, list[str]]:
        node = self.path[-1]
        if node == "Settings":
            return node, [GLYPH_SUBMENU + "Config", GLYPH_SUBMENU + "About"]
        if node == "Config":
            return node, [GLYPH_SUBMENU + label for label in CONFIG_ITEMS]
        if node == "About":
            rows = []
            for label, value in self.about():
                rows += [GLYPH_LEAF + label, GLYPH_LEAF + "  " + value]
            return node, rows
        if node == "Text Color":
            return node, [GLYPH_SUBMENU + group for group, _ in TEXT_COLORS]
        if node == "Battery Level":
            # Only reached before the first 0xC25D report after a restart.
            return node, [GLYPH_LEAF + "Not reported yet"]
        if node in CHOICES:
            verb, options = CHOICES[node]
            return node, self._checked(verb, options)
        for group, colors in TEXT_COLORS:
            if node == group:
                return node, self._checked("tc", colors)
        raise KeyError(node)

    def _checked(self, verb, options):
        current = _norm(self.settings.get(verb))
        return [
            (GLYPH_CHECKED if value == current else GLYPH_LEAF) + label
            for label, value in options
        ]

    def _options(self, node):
        if node in CHOICES:
            return CHOICES[node]
        for group, colors in TEXT_COLORS:
            if node == group:
                return "tc", colors
        return None

    def _screen(self, selected: int | None = None, write=None) -> Screen:
        title, items = self._items()
        if selected is None:
            # Land on the current value, as the director does.
            selected = next(
                (i for i, s in enumerate(items) if s.startswith(GLYPH_CHECKED)),
                0,
            )
        return Screen(
            "list", title, items, max(0, min(selected, len(items) - 1)), write=write
        )

    # -- remote events -------------------------------------------------------

    def select(self, index: int) -> Screen | None:
        """`c4.ln.is` on the open list. None means draw nothing."""
        if not self.path:
            return None
        node = self.path[-1]
        _, items = self._items()
        if not 0 <= index < len(items):
            return None
        label = items[index][1:]

        options = self._options(node)
        if options is not None:
            verb, values = options
            value = values[index][1]
            self.settings[verb] = value
            return self._screen(index, write=(verb, value))

        if node in ("About", "Battery Level"):
            return None
        if label == "Display Brightness":
            return Screen("slider", label, slider_id=SLIDER_DISPLAY)
        if label == "Keypad Brightness":
            return Screen("slider", label, slider_id=SLIDER_KEYPAD)
        if label == "Battery Level":
            battery = self.battery()
            if battery is not None:
                return Screen("gauge", label, value=int(battery))
        self.path.append(label)
        return self._screen()

    def back(self) -> Screen:
        """`c4.ln.lb`: up one level, cursor on the item we came from."""
        if len(self.path) <= 1:
            self.path = []
            return Screen("close")
        child = self.path.pop()
        _, items = self._items()
        index = next((i for i, s in enumerate(items) if s[1:] == child), 0)
        return self._screen(index)

    def resume(self, label: str) -> Screen:
        """Redraw the list a slider or gauge was opened from, after it closed."""
        _, items = self._items()
        index = next((i for i, s in enumerate(items) if s[1:] == label), 0)
        return self._screen(index)
