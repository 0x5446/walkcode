"""Tests for the tty-ownership gate, the resume double-instance guard, and the
stuck-turn watchdog.

Background: a nested child agent (e.g. a deep-review sub-agent in the parent's
background terminal) inherits the parent's $TMUX and would fire hooks reporting
the parent's tmux — hijacking the parent's Feishu thread, orphaning it, and
double-resuming the still-alive parent. Root fix: the hook CLIENT decides whether
it is the pane's real owner by controlling terminal (tty.is_tmux_pane_owner) and a
non-owner's hooks are never reported, so the server needs no takeover heuristics
at all. _resume_agent additionally injects instead of resuming when the old tmux
is still alive, and the watchdog interrupts once per timeout-watchable period.
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
        acks = []
        with patch.object(server, "validate_target", lambda t: None), \
             patch.object(server, "is_agent_alive", lambda t: True), \
             patch.object(server, "inject", lambda tty_, text: injected.append((tty_, text))), \
             patch.object(server, "verify_submitted", lambda tty_, text, **kw: server.INPUT_EMPTY), \
             patch.object(server, "_ack_inject_accepted", lambda mid: acks.append(mid)), \
             patch.object(server, "_register_pending_inject", lambda *a, **k: None), \
             patch.object(server.subprocess, "run") as mock_run:
            server._resume_agent("sid-1", old, "hello", "msg-1")
        self.assertEqual(injected, [("walkcode-1", "hello")])
        self.assertEqual(acks, ["msg-1"])
        mock_run.assert_not_called()  # must NOT spawn a second instance


class _FakeStore:
    def __init__(self, sessions=None):
        self._sessions = dict(sessions or {})
        self.statuses = []
        self.redeliveries = []

    def items(self):
        return [(sid, s) for sid, s in self._sessions.items()]

    def set_stopped(self, session_id, reason="completed", *, interrupt_reason="", running_since=0.0, preserve_terminal=True):
        self.statuses.append((session_id, reason, interrupt_reason))
        if session_id in self._sessions:
            self._sessions[session_id].status = "stopped"
            self._sessions[session_id].stop_reason = reason
            self._sessions[session_id].interrupt_reason = interrupt_reason
            self._sessions[session_id].running_since = running_since

    def mark_waiting(self, session_id, reason, started_at):
        self.set_stopped(session_id, reason, running_since=started_at, preserve_terminal=False)

    def start_running(self, session_id, started_at):
        if session_id in self._sessions:
            self._sessions[session_id].status = "running"
            self._sessions[session_id].stop_reason = ""
            self._sessions[session_id].interrupt_reason = ""
            self._sessions[session_id].running_since = started_at

    def clear_running(self, session_id):
        if session_id in self._sessions:
            self._sessions[session_id].running_since = 0.0

    def add_redelivery(self, session_id, text, key=None):
        self.redeliveries.append((session_id, text, key))


class _FakeRegistry:
    def __init__(self, hitl=False, snaps=None):
        self.hitl = hitl
        self.snaps = list(snaps or [])
        self.invalidated = []

    def has_open_request(self, session_id):
        return self.hitl

    def invalidate_session(self, session_id):
        self.invalidated.append(session_id)
        return list(self.snaps)


class StuckWatchdogTests(unittest.TestCase):
    def _run_scans(
        self, ages_min, *, reply_status="sent", interrupt_ok=True,
        alive=True, target=None, hitl=False, initial_status="running",
        stop_reason="", interrupt_reason="",
    ):
        last_ups = dict(server._session_last_ups)
        last_stop = dict(server._session_last_stop)
        try:
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            return self._run_scans_inner(
                ages_min, reply_status=reply_status, interrupt_ok=interrupt_ok,
                alive=alive, target=target, hitl=hitl,
                initial_status=initial_status,
                stop_reason=stop_reason,
                interrupt_reason=interrupt_reason,
            )
        finally:
            server._session_last_ups.clear()
            server._session_last_ups.update(last_ups)
            server._session_last_stop.clear()
            server._session_last_stop.update(last_stop)

    def _run_scans_inner(
        self, ages_min, *, reply_status, interrupt_ok, alive, target, hitl,
        initial_status, stop_reason, interrupt_reason,
    ):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = initial_status
        sess.stop_reason = stop_reason
        sess.interrupt_reason = interrupt_reason
        store = _FakeStore({"sid-1": sess})
        sent = []
        interrupts = []
        refreshes = []

        def reply_status_fn(*a, **k):
            sent.append(a)
            return (reply_status, f"m{len(sent)}") if reply_status == "sent" else (reply_status, None)

        def interrupt(tmux_name):
            interrupts.append(tmux_name)
            return interrupt_ok

        counts = []
        with patch.object(server, "session_store", store), \
             patch.object(server, "registry", _FakeRegistry(hitl)), \
             patch.object(server, "validate_target", lambda t: target), \
             patch.object(server, "is_agent_alive", lambda t: alive), \
             patch.object(server, "_interrupt_agent_turn", interrupt), \
             patch.object(server, "_reply_status", reply_status_fn), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or False), \
             patch.object(server.time, "time", lambda: now["t"]):
            with server._stuck_lock:
                server._stuck_alerted.clear()
            for age in ages_min:
                if age is None:
                    sess.running_since = 0.0
                elif sess.status == "running" or sess.stop_reason in server._TIMEOUT_STOP_REASONS:
                    sess.running_since = now["t"] - age * 60
                server._check_stuck_sessions()
                counts.append((len(interrupts), len(sent), len(refreshes), len(store.redeliveries)))
        return counts

    def test_interrupts_and_notifies_once_per_stuck_turn(self):
        self.assertEqual(self._run_scans([40, 40]), [(1, 1, 1, 0), (1, 1, 1, 0)])

    def test_no_interrupt_below_threshold(self):
        self.assertEqual(self._run_scans([2]), [(0, 0, 0, 0)])

    def test_no_interrupt_when_not_running(self):
        self.assertEqual(self._run_scans([None]), [(0, 0, 0, 0)])

    def test_no_interrupt_when_stopped(self):
        self.assertEqual(
            self._run_scans([40], initial_status="stopped", stop_reason="completed"),
            [(0, 0, 0, 0)],
        )

    def test_no_repeat_interrupt_when_already_timeout_interrupted(self):
        self.assertEqual(
            self._run_scans(
                [40],
                initial_status="stopped",
                stop_reason="interrupted",
                interrupt_reason="timeout",
            ),
            [(0, 0, 0, 0)],
        )

    def test_interrupts_permission_waiting_too(self):
        self.assertEqual(
            self._run_scans([40], initial_status="stopped", stop_reason="permission_request"),
            [(1, 1, 1, 0)],
        )

    def test_interrupts_ask_user_question_waiting_too(self):
        self.assertEqual(
            self._run_scans([40], initial_status="stopped", stop_reason="ask_user_question"),
            [(1, 1, 1, 0)],
        )

    def test_permission_waiting_timeout_updates_root_card_state(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = "stopped"
        sess.stop_reason = "permission_request"
        sess.running_since = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        refreshes = []
        with patch.object(server, "session_store", store), \
             patch.object(server, "registry", _FakeRegistry(False)), \
             patch.object(server, "validate_target", lambda t: None), \
             patch.object(server, "is_agent_alive", lambda t: True), \
             patch.object(server, "capture_pane",
                          side_effect=AssertionError("waiting timeout must not inspect pane progress")), \
             patch.object(server, "_interrupt_agent_turn", lambda tmux: True), \
             patch.object(server, "_reply_status", lambda *a, **k: ("sent", "m1")), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or True), \
             patch.object(server.time, "time", lambda: now["t"]):
            with server._stuck_lock:
                server._stuck_alerted.clear()
            server._check_stuck_sessions()

        self.assertEqual(store.statuses, [("sid-1", "interrupted", "timeout")])
        self.assertEqual(sess.status, "stopped")
        self.assertEqual(sess.stop_reason, "interrupted")
        self.assertEqual(sess.interrupt_reason, "timeout")
        self.assertEqual(refreshes, [("sid-1", {})])

    def test_timeout_invalidates_open_hitl_cards(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = "stopped"
        sess.stop_reason = "permission_request"
        sess.running_since = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        reg = _FakeRegistry(snaps=[
            {"tool_name": "Bash", "card_msg_id": "card-perm"},
            {"tool_name": "AskUserQuestion", "card_msg_id": "card-ask"},
        ])
        edits = []
        with patch.object(server, "session_store", store), \
             patch.object(server, "registry", reg), \
             patch.object(server, "validate_target", lambda t: None), \
             patch.object(server, "is_agent_alive", lambda t: True), \
             patch.object(server, "_interrupt_agent_turn", lambda tmux: True), \
             patch.object(server, "_reply_status", lambda *a, **k: ("sent", "m1")), \
             patch.object(server, "_refresh_health_card_for_event", lambda *a, **k: False), \
             patch.object(server, "_edit_card", lambda mid, card: edits.append((mid, card)) or True), \
             patch.object(server.time, "time", lambda: now["t"]):
            with server._stuck_lock:
                server._stuck_alerted.clear()
            server._check_stuck_sessions()

        self.assertEqual(reg.invalidated, ["sid-1"])
        self.assertEqual([mid for mid, _ in edits], ["card-perm", "card-ask"])
        self.assertTrue(all(card["header"]["template"] == "grey" for _, card in edits))

    def test_legacy_running_without_timer_is_not_watchable(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = "running"
        sess.running_since = 0.0
        sess.created_at = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []
        last_ups = dict(server._session_last_ups)
        last_stop = dict(server._session_last_stop)
        try:
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            with patch.object(server, "session_store", store), \
                 patch.object(server, "registry", _FakeRegistry(False)), \
                 patch.object(server, "validate_target", lambda t: None), \
                 patch.object(server, "is_agent_alive", lambda t: True), \
                 patch.object(server, "_interrupt_agent_turn",
                              lambda tmux: interrupts.append(tmux) or True), \
                 patch.object(server, "_reply_status", lambda *a, **k: ("sent", "m1")), \
                 patch.object(server, "_refresh_health_card_for_event", lambda *a, **k: False), \
                 patch.object(server.time, "time", lambda: now["t"]):
                with server._stuck_lock:
                    server._stuck_alerted.clear()
                server._check_stuck_sessions()
        finally:
            server._session_last_ups.clear()
            server._session_last_ups.update(last_ups)
            server._session_last_stop.clear()
            server._session_last_stop.update(last_stop)

        self.assertEqual(interrupts, [])

    def test_no_interrupt_when_dead(self):
        self.assertEqual(self._run_scans([40], target="not found"), [(0, 0, 0, 0)])

    def test_failed_interrupt_is_retried_without_notifying(self):
        self.assertEqual(
            self._run_scans([40, 40], interrupt_ok=False),
            [(1, 0, 0, 0), (2, 0, 0, 0)],
        )

    def test_transient_notice_is_stashed_after_interrupt(self):
        self.assertEqual(
            self._run_scans([40, 40], reply_status="transient"),
            [(1, 1, 1, 1), (1, 1, 1, 1)],
        )

    def test_new_running_period_after_stop_interrupts_again(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.running_since = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []
        sent = []
        refreshes = []
        last_ups = dict(server._session_last_ups)
        last_stop = dict(server._session_last_stop)
        try:
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            with patch.object(server, "session_store", store), \
                 patch.object(server, "registry", _FakeRegistry(False)), \
                 patch.object(server, "validate_target", lambda t: None), \
                 patch.object(server, "is_agent_alive", lambda t: True), \
                 patch.object(server, "_interrupt_agent_turn",
                              lambda tmux: interrupts.append(tmux) or True), \
                 patch.object(server, "_reply_status",
                              lambda *a, **k: sent.append(a) or ("sent", f"m{len(sent)}")), \
                 patch.object(server, "_refresh_health_card_for_event",
                              lambda sid, **kw: refreshes.append((sid, kw)) or False), \
                 patch.object(server.time, "time", lambda: now["t"]):
                with server._stuck_lock:
                    server._stuck_alerted.clear()

                server._check_stuck_sessions()
                server._mark_session_idle("sid-1")
                now["t"] += 1
                server._mark_session_busy("sid-1")
                now["t"] += 40 * 60
                server._check_stuck_sessions()
        finally:
            server._session_last_ups.clear()
            server._session_last_ups.update(last_ups)
            server._session_last_stop.clear()
            server._session_last_stop.update(last_stop)

        self.assertEqual(len(interrupts), 2)
        self.assertEqual(len(sent), 2)
        self.assertEqual(len(refreshes), 2)

    def test_footer_text_is_not_used_for_timeout_detection(self):
        self.assertEqual(self._run_scans([40]), [(1, 1, 1, 0)])

    def test_pane_changes_do_not_reset_running_timeout(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.running_since = now["t"] - 29 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []
        last_ups = dict(server._session_last_ups)
        last_stop = dict(server._session_last_stop)
        try:
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            with patch.object(server, "session_store", store), \
                 patch.object(server, "registry", _FakeRegistry(False)), \
                 patch.object(server, "validate_target", lambda t: None), \
                 patch.object(server, "is_agent_alive", lambda t: True), \
                 patch.object(server, "capture_pane",
                              side_effect=AssertionError("running timeout must not inspect pane progress")), \
                 patch.object(server, "_interrupt_agent_turn",
                              lambda tmux: interrupts.append(tmux) or True), \
                 patch.object(server, "_reply_status", lambda *a, **k: ("sent", "m1")), \
                 patch.object(server, "_refresh_health_card_for_event", lambda *a, **k: False), \
                 patch.object(server.time, "time", lambda: now["t"]):
                with server._stuck_lock:
                    server._stuck_alerted.clear()
                server._check_stuck_sessions()
                now["t"] += 2 * 60
                server._check_stuck_sessions()
        finally:
            server._session_last_ups.clear()
            server._session_last_ups.update(last_ups)
            server._session_last_stop.clear()
            server._session_last_stop.update(last_stop)

        self.assertEqual(interrupts, ["walkcode-1"])

    def test_explicit_progress_event_resets_running_timeout(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.running_since = now["t"] - 29 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []
        last_ups = dict(server._session_last_ups)
        last_stop = dict(server._session_last_stop)
        try:
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            with patch.object(server, "session_store", store), \
                 patch.object(server, "registry", _FakeRegistry(False)), \
                 patch.object(server, "validate_target", lambda t: None), \
                 patch.object(server, "is_agent_alive", lambda t: True), \
                 patch.object(server, "capture_pane",
                              side_effect=AssertionError("running timeout must not inspect pane progress")), \
                 patch.object(server, "_interrupt_agent_turn",
                              lambda tmux: interrupts.append(tmux) or True), \
                 patch.object(server, "_reply_status", lambda *a, **k: ("sent", "m1")), \
                 patch.object(server, "_refresh_health_card_for_event", lambda *a, **k: False), \
                 patch.object(server.time, "time", lambda: now["t"]):
                with server._stuck_lock:
                    server._stuck_alerted.clear()
                server._check_stuck_sessions()
                now["t"] += 2 * 60
                server._mark_session_busy("sid-1")
                server._check_stuck_sessions()
                self.assertEqual(interrupts, [])
                self.assertEqual(sess.running_since, now["t"])
                now["t"] += 31 * 60
                server._check_stuck_sessions()
        finally:
            server._session_last_ups.clear()
            server._session_last_ups.update(last_ups)
            server._session_last_stop.clear()
            server._session_last_stop.update(last_stop)

        self.assertEqual(interrupts, ["walkcode-1"])


if __name__ == "__main__":
    unittest.main()
