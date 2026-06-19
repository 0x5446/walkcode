"""Tests for tty.verify_submitted — the close-the-loop Enter-retry after inject.

inject() pastes + sends one Enter, but a loaded/attached TUI can drop that Enter,
leaving the text in the input box. verify_submitted re-checks the bottom box and
re-sends a BARE Enter (never re-pastes) until the box clears or retries run out.
Pinned here with a fake clock and scripted pane frames (no real tmux, no waiting).
"""

import unittest
from unittest.mock import patch

from walkcode import tty


def _box(inner: str) -> str:
    return "\n".join([
        "╭────────────────────────────╮",
        f"│ > {inner}",
        "╰────────────────────────────╯",
    ])


_EMPTY = _box("")
_OURS = _box("PING please reply OK")
_OURS_TEXT = "PING please reply OK"
_MENU = "\n".join(["Do you want to proceed?", "❯ 1. Yes", "  2. No"])
_OTHER = _box("user is typing something else")


class _Clock:
    def __init__(self):
        self.now = 0.0

    def sleep(self, s):
        self.now += s


def _scripted(frames):
    """capture_pane stub: yield each frame once, then repeat the last forever."""
    seq = list(frames)

    def _cap(session_name, lines=30):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _cap


class VerifySubmittedTests(unittest.TestCase):
    def _run(self, frames, text=_OURS_TEXT, attempts=3):
        clock = _Clock()
        enters = {"n": 0}

        def _enter(session_name):
            enters["n"] += 1
            return True

        with patch.object(tty.time, "sleep", clock.sleep), \
             patch.object(tty, "capture_pane", _scripted(frames)), \
             patch.object(tty, "send_enter", _enter):
            result = tty.verify_submitted("sess", text, attempts=attempts)
        return result, enters["n"]

    def test_already_submitted_no_enter(self):
        result, enters = self._run([_EMPTY])
        self.assertEqual(result, tty.INPUT_EMPTY)
        self.assertEqual(enters, 0)

    def test_dropped_enter_retried_once_then_submits(self):
        result, enters = self._run([_OURS, _EMPTY])
        self.assertEqual(result, tty.INPUT_EMPTY)
        self.assertEqual(enters, 1)

    def test_stuck_after_exhausting_retries(self):
        result, enters = self._run([_OURS], attempts=3)
        self.assertEqual(result, tty.STUCK)
        self.assertEqual(enters, 3)

    def test_menu_never_pressed(self):
        result, enters = self._run([_MENU])
        self.assertEqual(result, tty.INPUT_MENU)
        self.assertEqual(enters, 0)

    def test_other_draft_not_touched(self):
        result, enters = self._run([_OTHER])
        self.assertEqual(result, tty.INPUT_HAS_OTHER)
        self.assertEqual(enters, 0)

    def test_send_enter_failure_returns_stuck(self):
        # If the bare Enter can't even be sent, don't silently degrade to a later
        # INPUT_UNKNOWN (which the caller treats as success) — return STUCK.
        clock = _Clock()
        with patch.object(tty.time, "sleep", clock.sleep), \
             patch.object(tty, "capture_pane", _scripted([_OURS])), \
             patch.object(tty, "send_enter", lambda s: False):
            result = tty.verify_submitted("sess", _OURS_TEXT, attempts=3)
        self.assertEqual(result, tty.STUCK)


if __name__ == "__main__":
    unittest.main()
