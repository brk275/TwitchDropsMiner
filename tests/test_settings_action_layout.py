"""Source contracts supplement rendered settings action-row layout checks."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUTTONS = (
    "save-discord-btn",
    "test-discord-btn",
    "remove-discord-btn",
    "save-telegram-btn",
    "test-telegram-btn",
    "select-all-btn",
    "deselect-all-btn",
    "add-game-btn",
)


class ActionParents(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.parents = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") in BUTTONS:
            self.parents[attributes["id"]] = tuple(classes for _, classes in self.stack)
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, frozenset(attributes.get("class", "").split())))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_settings_action_groups_allow_wrapping_without_clipping():
    css = (ROOT / "web/static/styles.css").read_text(encoding="utf-8")
    rule = re.search(
        r"\.settings-section\s+\.button-group\s*,\s*"
        r"\.settings-section\s+\.filter-actions\s*\{([^{}]*)\}",
        css,
    )
    assert rule is not None, "Keep wrapping scoped to the settings action groups"
    assert re.search(r"\bflex-wrap\s*:\s*wrap\s*;", rule[1])
    assert not re.search(r"\boverflow(?:-x)?\s*:\s*(?:hidden|clip)\b", rule[1])


@pytest.mark.parametrize("button", BUTTONS)
def test_action_buttons_stay_inside_the_wrapping_settings_groups(button):
    parser = ActionParents()
    parser.feed((ROOT / "web/index.html").read_text(encoding="utf-8"))
    parents = parser.parents[button]
    assert any("settings-section" in classes for classes in parents)
    assert parents[-1] & {"button-group", "filter-actions"}
