"""An HA-defined List menu for the SR-260, navigated by the quirk.

A Control4 director answers every List screen itself: Select or Right opens a
submenu, `c4.ln.lb` goes back with the cursor on the item left, and a leaf runs
its action while the list stays up (measured). This is the same
behaviour over a tree Home Assistant supplies, so automations decide what the
menu holds and what a pick does without handling navigation.

The tree is JSON:

    {"title": "Living Room",
     "items": [
       {"label": "Security", "items": [
         {"label": "Front Door: Locked", "action": "lock.unlock|lock.front_door"}]},
       {"label": "Now 68F"},                       # info row, does nothing
       {"label": "Heat", "checked": true, "action": "..."},
       {"label": "Settings", "settings": true}]}   # the quirk's own Settings

A leaf with an `action` comes back to HA as a `menu_action` event carrying the
action string and the path to it, so HA can redraw the same list with fresh
labels. No zigpy import, so the tests drive it directly.
"""

from __future__ import annotations

from c4_settings_menu import GLYPH_CHECKED, GLYPH_LEAF, GLYPH_SUBMENU, Screen

GLYPHS = (GLYPH_CHECKED, GLYPH_LEAF, GLYPH_SUBMENU)

# The SR-260 row fits about this many characters, and a longer item can push a
# gi reply past the ~70-byte frame ceiling on its own.
MAX_LABEL = 24


def clean_label(text) -> str:
    r"""Reduce a label to what the row can show.

    Printable ASCII only: the LCD drops anything above 0x7E, and `"` / `\`
    break the quoting the frame relies on.
    """
    s = "".join(c for c in str(text or "") if " " <= c <= "~" and c not in '"\\')
    return s[:MAX_LABEL]


def plain_rows(items) -> list[str]:
    """Render rows for a model whose items take no glyph (the SR-250).

    The glyph is spelled out instead, `>` after a submenu and `*` before a
    checked row.
    """
    out = []
    for item in items:
        glyph, text = (item[:1], item[1:]) if item[:1] in GLYPHS else ("", item)
        if glyph == GLYPH_SUBMENU:
            text = text + " >"
        elif glyph == GLYPH_CHECKED:
            text = "* " + text
        out.append(text)
    return out


class MenuTree:
    """One remote's walk through an HA-supplied tree."""

    def __init__(self, tree: dict, make_settings):
        """Start at the root of `tree`; `make_settings` builds a SettingsMenu."""
        self.tree = tree if isinstance(tree, dict) else {}
        self.make_settings = make_settings
        self.stack: list[dict] = []
        self.sub = None  # a SettingsMenu while the Settings subtree is open

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _children(node):
        items = node.get("items")
        return (
            [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []
        )

    @staticmethod
    def _glyph(child):
        if "items" in child or child.get("settings"):
            return GLYPH_SUBMENU
        return GLYPH_CHECKED if child.get("checked") else GLYPH_LEAF

    def _title(self, node):
        return clean_label(node.get("title") or node.get("label") or "Menu")

    def path(self) -> list[str]:
        """Return the labels of the submenus opened below the root."""
        return [clean_label(n.get("label")) for n in self.stack[1:]]

    def _screen(self, selected=None) -> Screen:
        node = self.stack[-1]
        children = self._children(node)
        items = [self._glyph(c) + clean_label(c.get("label")) for c in children]
        if not items:
            items = [GLYPH_LEAF + "Empty"]  # what the director shows, e.g. Share
        if selected is None:
            selected = next(
                (i for i, s in enumerate(items) if s.startswith(GLYPH_CHECKED)),
                0,
            )
        return Screen(
            "list", self._title(node), items, max(0, min(int(selected), len(items) - 1))
        )

    # -- navigation ----------------------------------------------------------

    def open(self, path=None, selected=None) -> Screen:
        """Root, or the list at `path` (labels) when HA redraws after an action."""
        self.stack = [self.tree]
        self.sub = None
        for label in path or []:
            child = next(
                (
                    c
                    for c in self._children(self.stack[-1])
                    if clean_label(c.get("label")) == clean_label(label)
                    and "items" in c
                ),
                None,
            )
            if child is None:
                selected = None  # the tree changed shape; settle where we got to
                break
            self.stack.append(child)
        return self._screen(selected)

    def select(self, index: int):
        """Act on the row at `index`: open a submenu or return its action."""
        if self.sub is not None:
            return self.sub.select(index)
        children = self._children(self.stack[-1])
        if not 0 <= index < len(children):
            return None
        child = children[index]
        if child.get("settings"):
            self.sub = self.make_settings()
            return self.sub.open()
        if "items" in child:
            self.stack.append(child)
            return self._screen()
        action = child.get("action")
        if isinstance(action, str) and action:
            return Screen(
                "action",
                clean_label(child.get("label")),
                selected=index,
                action=action,
                path=self.path(),
            )
        return None  # info row

    def back(self) -> Screen:
        """Step up one level and return the screen to draw."""
        if self.sub is not None:
            screen = self.sub.back()
            if screen.kind != "close":
                return screen
            self.sub = None
            index = next(
                (
                    i
                    for i, c in enumerate(self._children(self.stack[-1]))
                    if c.get("settings")
                ),
                0,
            )
            return self._screen(index)
        if len(self.stack) <= 1:
            self.stack = []
            return Screen("close")
        child = self.stack.pop()
        index = next(
            (i for i, c in enumerate(self._children(self.stack[-1])) if c is child),
            0,
        )
        return self._screen(index)

    def resume(self, label: str) -> Screen:
        """Redraw after a slider or gauge closed; those exist only inside Settings."""
        if self.sub is not None:
            return self.sub.resume(label)
        return self._screen()
