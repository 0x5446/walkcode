"""Tests for tty input-box parsing — the close-the-loop submit check.

inject() returning success only means tmux accepted the keystrokes, not that the
TUI submitted. classify_input_box inspects ONLY the bottom-most framed box so a
submitted prompt echoed back into the transcript (which also carries `>` and the
original text) is never mistaken for an unsent draft. Only INPUT_HAS_OURS makes
the caller re-send Enter; a menu/dialog must never receive a stray Enter.
"""

import unittest

from walkcode import tty


def _pane(*lines: str) -> str:
    return "\n".join(lines)


# Reusable box fragments (rounded + square corners both seen in the wild).
_EMPTY_BOX = _pane(
    "╭────────────────────────────╮",
    "│ >                          │",
    "╰────────────────────────────╯",
    "  ? for shortcuts",
)
_OURS = "仔细全面的更新adr了吗？"
_OURS_BOX = _pane(
    "╭────────────────────────────╮",
    f"│ > {_OURS}        │",
    "╰────────────────────────────╯",
)

# Real Claude Code v2.1.x framing: two horizontal rules bracket the ``❯`` prompt
# (no corner box). The empty prompt is "❯\xa0" (trailing non-breaking space).
_RULE = "─" * 60


def _rule_box(inner: str) -> str:
    return _pane(_RULE, f"❯\xa0{inner}", _RULE, "  ⏵⏵ bypass permissions on")


class ExtractInputBoxTests(unittest.TestCase):
    def test_rounded_box(self):
        inner = tty._extract_input_box(_OURS_BOX)
        self.assertIsNotNone(inner)
        self.assertTrue(any(_OURS in ln for ln in inner))

    def test_square_box(self):
        pane = _pane("┌─────┐", "│ > hi │", "└─────┘")
        inner = tty._extract_input_box(pane)
        self.assertEqual(inner, ["> hi"])

    def test_no_box_returns_none(self):
        self.assertIsNone(tty._extract_input_box(_pane("just output", "no box here")))

    def test_only_bottom_most_box_is_taken(self):
        pane = _pane(
            "╭─────╮", "│ > old draft │", "╰─────╯",
            "...transcript...",
            "╭─────╮", "│ > │", "╰─────╯",
        )
        inner = tty._extract_input_box(pane)
        # The bottom box is empty; the older "old draft" box above is ignored.
        self.assertEqual(tty._norm(" ".join(inner)).replace(">", "").strip(), "")


class ClassifyInputBoxTests(unittest.TestCase):
    def test_empty_box_is_empty(self):
        self.assertEqual(tty.classify_input_box(_EMPTY_BOX, _OURS), tty.INPUT_EMPTY)

    def test_our_short_text_is_has_ours(self):
        self.assertEqual(tty.classify_input_box(_OURS_BOX, _OURS), tty.INPUT_HAS_OURS)

    def test_pasted_placeholder_is_has_ours(self):
        pane = _pane("╭────────╮", "│ > [Pasted text #1 +12 lines] │", "╰────────╯")
        self.assertEqual(tty.classify_input_box(pane, "a very long message"), tty.INPUT_HAS_OURS)

    def test_other_draft_is_has_other(self):
        pane = _pane("╭────────╮", "│ > some unrelated draft │", "╰────────╯")
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_HAS_OTHER)

    def test_transcript_echo_with_empty_box_is_empty(self):
        # The crux anti-false-positive case: our prompt was submitted and is now
        # echoed in the transcript (with a `>` and the text), but the bottom input
        # box is empty. Must read EMPTY, not HAS_OURS.
        pane = _pane(
            f"> {_OURS}",
            "⏺ working on it...",
            "╭────────────────────────────╮",
            "│ >                          │",
            "╰────────────────────────────╯",
        )
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_EMPTY)

    def test_numbered_menu_is_menu(self):
        pane = _pane(
            "Do you want to proceed?",
            "❯ 1. Yes",
            "  2. No",
        )
        self.assertEqual(tty.classify_input_box(pane, "whatever"), tty.INPUT_MENU)

    def test_boxed_permission_menu_is_menu(self):
        pane = _pane(
            "╭────────────────────────────╮",
            "│ Do you want to proceed?     │",
            "│ ❯ 1. Yes                    │",
            "│   2. No                     │",
            "╰────────────────────────────╯",
        )
        self.assertEqual(tty.classify_input_box(pane, "2"), tty.INPUT_MENU)

    def test_codex_placeholder_box_is_empty(self):
        pane = _pane("╭────────╮", '│ › Try "fix the bug" │', "╰────────╯")
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_EMPTY)

    def test_no_box_no_menu_is_unknown(self):
        self.assertEqual(
            tty.classify_input_box(_pane("plain output", "more output"), _OURS),
            tty.INPUT_UNKNOWN,
        )

    # --- Real Claude Code v2.1.x rule-bracketed input (── / ❯ / ──) ---
    def test_rule_box_empty_is_empty(self):
        self.assertEqual(tty.classify_input_box(_rule_box(""), _OURS), tty.INPUT_EMPTY)

    def test_rule_box_with_our_text_is_has_ours(self):
        self.assertEqual(tty.classify_input_box(_rule_box(_OURS), _OURS), tty.INPUT_HAS_OURS)

    def test_rule_box_transcript_echo_with_empty_is_empty(self):
        pane = _pane(
            f"❯ {_OURS}",          # submitted prompt echoed above
            "⏺ working...",
            _RULE,
            "❯\xa0",                # bottom input is empty
            _RULE,
            "  ⏵⏵ bypass permissions on",
        )
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_EMPTY)

    def test_rule_box_numeric_draft_is_has_ours_not_menu(self):
        # User message starting with "1." must stay HAS_OURS (Claude's input cursor
        # is ❯, so a cursor heuristic would wrongly call this a menu).
        self.assertEqual(
            tty.classify_input_box(_rule_box("1. buy milk"), "1. buy milk"),
            tty.INPUT_HAS_OURS,
        )

    def test_short_reply_exact_match_is_has_ours(self):
        self.assertEqual(tty.classify_input_box(_rule_box("2"), "2"), tty.INPUT_HAS_OURS)

    def test_short_reply_does_not_substring_match_menu_option(self):
        # "2" must NOT match a leftover "2. No" line — that would trigger a stray
        # Enter on a misread menu. Short text requires an exact box match.
        pane = _pane("╭────────╮", "│ > 2. No │", "╰────────╯")
        self.assertNotEqual(tty.classify_input_box(pane, "2"), tty.INPUT_HAS_OURS)

    def test_long_message_tiny_unrelated_draft_is_not_has_ours(self):
        # Box shows a 1-char draft "a"; our injected message is long. "a" must not
        # reverse-substring-match into it and trigger a stray Enter.
        msg = "please try updating the tests now"
        self.assertNotEqual(tty.classify_input_box(_rule_box("a"), msg), tty.INPUT_HAS_OURS)

    def test_truncated_box_slice_still_matches(self):
        # A long message rendered truncated in a narrow box (box shows a >=4-char
        # prefix slice of ours) still counts as HAS_OURS.
        self.assertEqual(tty.classify_input_box(_rule_box("仔细全面"), _OURS), tty.INPUT_HAS_OURS)

    def test_user_message_starting_with_try_is_has_ours_not_empty(self):
        # Real message "try updating the tests" must not be swallowed as an empty
        # box by the codex placeholder rule.
        msg = "try updating the tests"
        self.assertEqual(tty.classify_input_box(_rule_box(msg), msg), tty.INPUT_HAS_OURS)

    def test_corner_box_above_rule_box_below_takes_bottom_most(self):
        # An old corner box in the transcript, the live rule-bracketed input below.
        pane = _pane(
            "╭─────╮", f"│ > {_OURS} │", "╰─────╯",
            "⏺ done",
            _RULE, "❯\xa0", _RULE,
        )
        # Bottom-most frame is the empty rule box -> EMPTY, not HAS_OURS from the
        # stale corner box above.
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_EMPTY)

    def test_prose_numbered_list_is_not_menu(self):
        # A plain numbered list in assistant output (no selection cursor) must not
        # be read as a menu — that would wrongly skip a legitimate Enter retry.
        pane = _pane(
            "Steps:",
            "1. do this",
            "2. do that",
            "╭────────────────────────────╮",
            f"│ > {_OURS}        │",
            "╰────────────────────────────╯",
        )
        self.assertEqual(tty.classify_input_box(pane, _OURS), tty.INPUT_HAS_OURS)


if __name__ == "__main__":
    unittest.main()
