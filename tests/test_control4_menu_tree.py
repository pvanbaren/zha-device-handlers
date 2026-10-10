"""The HA-defined SR-260 List menu: navigation over a tree.

`c4_menu_tree` has no zigpy import, so it is driven directly. Navigation copies
what a Control4 director does (measured): Select or
Right opens a submenu, `lb` goes back with the cursor on the item left, and a
leaf runs while its list stays up.
"""

from __future__ import annotations

from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
QUIRKS = REPO / "zhaquirks" / "control4"
sys.path.insert(0, str(QUIRKS))

from c4_menu_tree import MAX_LABEL, MenuTree, clean_label, plain_rows  # noqa: E402
from c4_settings_menu import (  # noqa: E402
    GLYPH_CHECKED,
    GLYPH_LEAF,
    GLYPH_SUBMENU,
    SettingsMenu,
)

TREE = {
    "title": "Living Room",
    "items": [
        {
            "label": "Security",
            "items": [
                {
                    "label": "Locks",
                    "items": [
                        {
                            "label": "Front Door: Locked",
                            "action": "lock.unlock|lock.front_door",
                        },
                    ],
                },
            ],
        },
        {
            "label": "Comfort",
            "items": [
                {"label": "Now 70F"},
                {
                    "label": "Mode",
                    "items": [
                        {"label": "Heat", "checked": False, "action": "m|heat"},
                        {"label": "Cool", "checked": True, "action": "m|cool"},
                    ],
                },
            ],
        },
        {"label": "Share", "items": []},
        {"label": "Settings", "settings": True},
    ],
}


def _tree(tree=TREE):
    return MenuTree(tree, lambda: SettingsMenu({}, lambda: [], lambda: None))


def _labels(screen):
    return [s[1:] for s in screen.items]


def test_root_glyphs():
    """Test root glyphs."""
    root = _tree().open()
    assert root.title == "Living Room"
    assert _labels(root) == ["Security", "Comfort", "Share", "Settings"]
    assert all(s.startswith(GLYPH_SUBMENU) for s in root.items)


def test_leaf_returns_action_with_path_and_stays():
    """Test leaf returns action with path and stays."""
    menu = _tree()
    menu.open()
    menu.select(0)
    menu.select(0)
    picked = menu.select(0)
    assert picked.kind == "action"
    assert picked.action == "lock.unlock|lock.front_door"
    assert picked.path == ["Security", "Locks"]
    assert picked.selected == 0
    assert menu.path() == ["Security", "Locks"]  # still on that list


def test_info_row_does_nothing_and_checked_lands_cursor():
    """Test info row does nothing and checked lands cursor."""
    menu = _tree()
    menu.open()
    comfort = menu.select(1)
    assert comfort.items[0] == GLYPH_LEAF + "Now 70F"
    assert menu.select(0) is None
    mode = menu.select(1)
    assert mode.items == [GLYPH_LEAF + "Heat", GLYPH_CHECKED + "Cool"]
    assert mode.selected == 1


def test_back_keeps_cursor_and_closes_at_root():
    """Test back keeps cursor and closes at root."""
    menu = _tree()
    menu.open()
    menu.select(1)
    menu.select(1)
    assert menu.back().selected == 1  # on Mode
    assert menu.back().selected == 1  # on Comfort
    assert menu.back().kind == "close"


def test_empty_submenu_shows_empty_like_the_director():
    """Test empty submenu shows empty like the director."""
    menu = _tree()
    menu.open()
    assert _labels(menu.select(2)) == ["Empty"]


def test_settings_is_nested_and_back_returns_to_it():
    """Test settings is nested and back returns to it."""
    menu = _tree()
    menu.open()
    settings = menu.select(3)
    assert settings.title == "Settings"
    menu.select(0)  # Config
    assert menu.back().title == "Settings"
    back = menu.back()
    assert back.title == "Living Room" and back.selected == 3


def test_reopen_at_path_after_an_action():
    """Test reopen at path after an action."""
    menu = _tree()
    screen = menu.open(["Comfort", "Mode"], 0)
    assert screen.title == "Mode" and screen.selected == 0
    assert menu.path() == ["Comfort", "Mode"]
    # A tree that changed shape settles at the deepest list that still exists.
    screen = menu.open(["Comfort", "Gone"], 4)
    assert screen.title == "Comfort" and screen.selected == 0


def test_labels_are_clean_ascii_and_short():
    """Test labels are clean ascii and short."""
    assert clean_label('Maybe Man \U0001f174 "x"\\') == "Maybe Man  x"
    assert len(clean_label("x" * 40)) == MAX_LABEL
    assert clean_label("68°F") == "68F"


def test_sr250_rows_spell_the_glyph_out():
    # The SR-250 takes no glyph, so a submenu and a checked row say so in text.
    """Test sr250 rows spell the glyph out."""
    tree = {
        "title": "Den",
        "items": [
            {
                "label": "Find Remote",
                "items": [{"label": "Den remote", "action": "find|x"}],
            },
            {"label": "Heat", "checked": True, "action": "a"},
            {"label": "Now 68F"},
        ],
    }
    rows = plain_rows(MenuTree(tree, lambda: None).open().items)
    assert rows == ["Find Remote >", "* Heat", "Now 68F"]
    assert not any(c < " " or c > "~" for r in rows for c in r)
