"""Regression tests for permission card button labels.

Background: prior to this fix, button text was scraped from the user's tmux
screen via `tmux capture-pane`. Any numbered list Claude happened to print
(plan steps, todo items, ...) could be misidentified as the permission
prompt's options, producing buttons whose text was completely unrelated to
their actual behavior — e.g. a button labeled "AWS SSO 登录..." that in fact
sent `behavior=always_allow`.

The fix is to derive button text exclusively from walkcode's own i18n table,
since button **semantics** (allow / always_allow / deny) are owned by walkcode
and do not depend on what Claude renders in the TUI.

These tests pin that contract: button labels are stable, behaviors are stable,
and permission_suggestions (when present) are rendered into the card body.
"""

import unittest

from walkcode.server import (
    _PERM_BEHAVIORS,
    _PERM_BUTTON_LABELS,
    _build_permission_card,
    _format_permission_suggestions,
)
from walkcode.i18n import t


def _button_texts(card: dict) -> list[str]:
    # Card schema: elements[-1] is the action row of buttons
    actions = card["elements"][-1]["actions"]
    return [b["text"]["content"] for b in actions]


def _button_behaviors(card: dict) -> list[str]:
    actions = card["elements"][-1]["actions"]
    return [b["value"]["b"] for b in actions]


def _card_body(card: dict) -> str:
    return card["elements"][0]["text"]["content"]


class ButtonLabelsTest(unittest.TestCase):
    """Buttons must always show i18n fallback text — never anything else."""

    def test_addRules_labels_from_i18n(self):
        card = _build_permission_card("rid1", "addRules", "Edit", {"file_path": "/foo"})
        self.assertEqual(_button_texts(card), [
            t("feishu.perm.allow"),
            t("feishu.perm.always_allow"),
            t("feishu.perm.deny"),
        ])

    def test_setMode_labels_from_i18n(self):
        card = _build_permission_card("rid2", "setMode", "Write", {"file_path": "/foo"})
        self.assertEqual(_button_texts(card), [
            t("feishu.setmode.yes"),
            t("feishu.setmode.accept_edits"),
            t("feishu.setmode.no"),
        ])

    def test_plan_labels_from_i18n(self):
        card = _build_permission_card("rid3", "plan", "ExitPlanMode", {"plan": "do stuff"})
        self.assertEqual(_button_texts(card), [
            t("feishu.plan.auto_accept"),
            t("feishu.plan.manual_approve"),
            t("feishu.plan.tell_claude"),
        ])


class ButtonBehaviorsTest(unittest.TestCase):
    """Behaviors must be stable across the label refactor."""

    def test_addRules_behaviors(self):
        card = _build_permission_card("rid", "addRules", "Edit", {})
        self.assertEqual(_button_behaviors(card), ["allow", "always_allow", "deny"])

    def test_setMode_behaviors(self):
        card = _build_permission_card("rid", "setMode", "Edit", {})
        self.assertEqual(_button_behaviors(card), ["allow", "accept_edits", "deny"])

    def test_plan_behaviors(self):
        card = _build_permission_card("rid", "plan", "ExitPlanMode", {"plan": "x"})
        self.assertEqual(_button_behaviors(card), ["plan_auto_accept", "plan_manual_approve", "deny"])

    def test_behaviors_match_table(self):
        # Every perm_type's button count equals behavior count equals label count
        for perm_type in _PERM_BEHAVIORS:
            self.assertEqual(
                len(_PERM_BEHAVIORS[perm_type]),
                len(_PERM_BUTTON_LABELS[perm_type]),
                f"{perm_type}: behaviors and labels must align"
            )


class PermissionSuggestionsRenderTest(unittest.TestCase):
    """permission_suggestions, when present, render into the card body."""

    def test_empty_suggestions_returns_empty_string(self):
        self.assertEqual(_format_permission_suggestions([]), "")
        self.assertEqual(_format_permission_suggestions(None or []), "")

    def test_addRules_with_ruleContent_renders(self):
        suggestions = [{
            "type": "addRules",
            "rules": [{"toolName": "Edit", "ruleContent": "/.claude/skills/deep-debug/**"}],
            "behavior": "allow",
            "destination": "session",
        }]
        rendered = _format_permission_suggestions(suggestions)
        self.assertIn("Edit", rendered)
        self.assertIn("/.claude/skills/deep-debug/**", rendered)

    def test_setMode_renders(self):
        suggestions = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
        rendered = _format_permission_suggestions(suggestions)
        self.assertIn("acceptEdits", rendered)

    def test_addDirectories_renders(self):
        suggestions = [{
            "type": "addDirectories",
            "directories": ["/Users/alpha/.claude"],
            "destination": "session",
        }]
        rendered = _format_permission_suggestions(suggestions)
        self.assertIn("/Users/alpha/.claude", rendered)

    def test_card_body_includes_suggestions(self):
        suggestions = [{
            "type": "addRules",
            "rules": [{"toolName": "Edit", "ruleContent": "/.claude/skills/deep-debug/**"}],
            "behavior": "allow",
            "destination": "session",
        }]
        card = _build_permission_card(
            "rid", "addRules", "Edit",
            {"file_path": "/x"},
            permission_suggestions=suggestions,
        )
        body = _card_body(card)
        self.assertIn("/.claude/skills/deep-debug/**", body)

    def test_card_body_without_suggestions_is_unchanged(self):
        card_a = _build_permission_card("rid", "addRules", "Edit", {"file_path": "/x"})
        card_b = _build_permission_card(
            "rid", "addRules", "Edit", {"file_path": "/x"}, permission_suggestions=None,
        )
        card_c = _build_permission_card(
            "rid", "addRules", "Edit", {"file_path": "/x"}, permission_suggestions=[],
        )
        self.assertEqual(_card_body(card_a), _card_body(card_b))
        self.assertEqual(_card_body(card_a), _card_body(card_c))


class CardStructureTest(unittest.TestCase):
    """Card schema sanity — buttons stay at element[-1], body at element[0]."""

    def test_three_buttons_each_type(self):
        for perm_type in ("addRules", "setMode", "plan"):
            card = _build_permission_card(
                "rid", perm_type, "Edit",
                {"plan": "x"} if perm_type == "plan" else {"file_path": "/x"},
            )
            actions = card["elements"][-1]["actions"]
            self.assertEqual(len(actions), 3, f"{perm_type}: expected 3 buttons")
            for b in actions:
                self.assertIn("rid", b["value"])
                self.assertIn("b", b["value"])


if __name__ == "__main__":
    unittest.main()
