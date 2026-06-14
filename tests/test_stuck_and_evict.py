"""Tests for the tty-ownership gate, the resume double-instance guard, and the
stuck-turn watchdog.

Background: a nested child agent (e.g. a deep-review sub-agent in the parent's
background terminal) inherits the parent's $TMUX and would fire hooks reporting
the parent's tmux — hijacking the parent's Feishu thread, orphaning it, and
double-resuming the still-alive parent. Root fix: the hook CLIENT decides whether
it is the pane's real owner by controlling terminal (tty.is_tmux_pane_owner) and a
non-owner's hooks are never reported, so the server needs no takeover heuristics
at all. _resume_agent additionally injects instead of resuming when the old tmux
is still alive, and the watchdog warns once per stuck turn (only after a confirmed
send, treating a capture failure as unknown rather than idle).
"""

import unittest
from unittest.mock import patch

from walkcode import server, tty
from walkcode.state import Session


def _footer(minutes=None, *, seconds=0, text=None):
    if text is not None:
        return text
    return f"• Working ({minutes}m {seconds}s • esc to interrupt)"


class CttyOwnsPaneTests(unittest.TestCase):
    def test_match(self):
        self.assertTrue(tty._ctty_owns_pane("ttys047", "/dev/ttys047"))

    def test_mismatch(self):
        self.assertFalse(tty._ctty_owns_pane("ttys047", "/dev/ttys099"))

    def test_no_controlling_terminal(self):
        self.assertFalse(tty._ctty_owns_pane("??", "/dev/ttys047"))
        self.assertFalse(tty._ctty_owns_pane("?", "/dev/ttys047"))
        self.assertFalse(tty._ctty_owns_pane("", "/dev/ttys047"))

    def test_empty_pane(self):
        self.assertFalse(tty._ctty_owns_pane("ttys047", ""))

    def test_no_partial_match(self):
        # The "/" anchor must prevent ttys4 from matching /dev/ttys47.
        self.assertFalse(tty._ctty_owns_pane("ttys4", "/dev/ttys47"))


class IsTmuxPaneOwnerTests(unittest.TestCase):
    def test_disabled_by_env_is_owner(self):
        with patch.dict("os.environ", {"WALKCODE_OWNER_CHECK": "0"}):
            self.assertTrue(tty.is_tmux_pane_owner())

    def test_not_in_tmux_is_owner(self):
        import os
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        env["WALKCODE_OWNER_CHECK"] = "1"
        with patch.dict("os.environ", env, clear=True):
            self.assertTrue(tty.is_tmux_pane_owner())

    def test_probe_failure_fails_open(self):
        env = dict(__import__("os").environ)
        env["TMUX"] = "/tmp/x,1,0"
        env["WALKCODE_OWNER_CHECK"] = "1"
        with patch.dict("os.environ", env), \
             patch.object(tty.subprocess, "run", side_effect=OSError("boom")):
            self.assertTrue(tty.is_tmux_pane_owner())  # fail-open

    def test_nonzero_returncode_fails_open(self):
        # A successful-but-nonzero probe (or empty pane_tty) must NOT be read as
        # "non-owner" — that would silently disconnect a real owner. Fail open.
        import subprocess as _sp
        env = dict(__import__("os").environ)
        env["TMUX"] = "/tmp/x,1,0"
        env["WALKCODE_OWNER_CHECK"] = "1"

        def fake_run(cmd, **kw):
            if cmd[0] == "tmux":  # pane_tty probe returns nonzero / empty
                return _sp.CompletedProcess(cmd, 1, "", "no server")
            return _sp.CompletedProcess(cmd, 0, "ttys047", "")

        with patch.dict("os.environ", env), \
             patch.object(tty.subprocess, "run", fake_run):
            self.assertTrue(tty.is_tmux_pane_owner())  # fail-open on nonzero probe


class ParseWorkingSecondsTests(unittest.TestCase):
    def test_codex_hms(self):
        self.assertEqual(
            server._parse_working_seconds(
                "• Working (6h 29m 16s • esc to interrupt) · 1 background terminal"
            ),
            6 * 3600 + 29 * 60 + 16,
        )

    def test_codex_ms(self):
        self.assertEqual(
            server._parse_working_seconds("• Working (8m 31s • esc to interrupt)"),
            8 * 60 + 31,
        )

    def test_claude_with_tokens(self):
        self.assertEqual(
            server._parse_working_seconds(
                "✻ Crunching… (1m 12s · ↑ 1.2k tokens · esc to interrupt)"
            ),
            72,
        )

    def test_idle_returns_none(self):
        self.assertIsNone(server._parse_working_seconds("› Explain this codebase"))
        self.assertIsNone(server._parse_working_seconds(""))

    def test_footer_only_ignores_scrollback(self):
        pane = (
            "• Working (40m 0s • esc to interrupt)\n"
            + "\n".join(f"output line {i}" for i in range(8))
            + "\n› idle prompt"
        )
        self.assertIsNone(server._parse_working_seconds(pane))

    def test_footer_timer_is_detected(self):
        pane = "some earlier output\n• Working (3m 0s • esc to interrupt)"
        self.assertEqual(server._parse_working_seconds(pane), 180)


class ResumeGuardTests(unittest.TestCase):
    def test_alive_old_session_injects_instead_of_resuming(self):
        old = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        injected = []
        with patch.object(server, "validate_target", lambda t: None), \
             patch.object(server, "is_agent_alive", lambda t: True), \
             patch.object(server, "inject", lambda tty_, text: injected.append((tty_, text))), \
             patch.object(server, "_register_pending_inject", lambda *a, **k: None), \
             patch.object(server.subprocess, "run") as mock_run:
            server._resume_agent("sid-1", old, "hello", "msg-1")
        self.assertEqual(injected, [("walkcode-1", "hello")])
        mock_run.assert_not_called()  # must NOT spawn a second instance


class _FakeStore:
    def __init__(self, sessions=None):
        self._sessions = dict(sessions or {})

    def items(self):
        return [(sid, s) for sid, s in self._sessions.items()]


class StuckWatchdogTests(unittest.TestCase):
    def _run_scans(self, panes, *, reply_ok=True, alive=True, target=None):
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        idx = {"i": 0}
        sent = []

        def cap(t, lines=40):
            return panes[idx["i"]]

        def reply(*a, **k):
            sent.append(a)
            return f"m{len(sent)}" if reply_ok else None

        counts = []
        with patch.object(server, "session_store", _FakeStore({"sid-1": sess})), \
             patch.object(server, "validate_target", lambda t: target), \
             patch.object(server, "is_agent_alive", lambda t: alive), \
             patch.object(server, "capture_pane", cap), \
             patch.object(server, "_reply", reply):
            with server._stuck_lock:
                server._stuck_alerted.clear()
            for i in range(len(panes)):
                idx["i"] = i
                server._check_stuck_sessions()
                counts.append(len(sent))
        return counts

    def test_alerts_once_per_stuck_turn(self):
        self.assertEqual(self._run_scans([_footer(40), _footer(40)]), [1, 1])

    def test_no_alert_below_threshold(self):
        self.assertEqual(self._run_scans([_footer(2)]), [0])

    def test_no_alert_when_idle(self):
        self.assertEqual(self._run_scans([_footer(text="› idle prompt")]), [0])

    def test_no_alert_when_dead(self):
        self.assertEqual(self._run_scans([_footer(40)], target="not found"), [0])

    def test_failed_send_is_retried(self):
        self.assertEqual(self._run_scans([_footer(40), _footer(40)], reply_ok=False), [1, 2])

    def test_new_turn_after_timer_reset_alerts_again(self):
        self.assertEqual(self._run_scans([_footer(40), _footer(5), _footer(40)]), [1, 1, 2])

    def test_capture_failure_does_not_reset_dedup(self):
        # 40m (alert) → "" capture fails (keep state) → 40m (already alerted, no
        # duplicate). Treating "" as idle would re-alert.
        self.assertEqual(self._run_scans([_footer(40), _footer(text=""), _footer(40)]), [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
