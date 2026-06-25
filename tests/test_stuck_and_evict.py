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

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from walkcode import server, tty
from walkcode.config import parse_stuck_threshold
from walkcode.state import (
    INTERRUPT_REASON_TIMEOUT,
    STATUS_RUNNING,
    STATUS_STOPPED,
    STOP_REASON_INTERRUPTED,
    Session,
    SessionStore,
)


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

    def get(self, session_id):
        return self._sessions.get(session_id)

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

    def start_running_if_allowed(self, session_id, started_at, *, allow_stopped_reasons=frozenset()):
        sess = self._sessions.get(session_id)
        if sess is None:
            return True
        if sess.status == "stopped" and sess.stop_reason not in allow_stopped_reasons:
            return False
        self.start_running(session_id, started_at)
        return True

    def interrupt_timeout_if_unchanged(
        self, session_id, *, expected_tty, expected_status, expected_stop_reason,
        expected_running_since, interrupt,
    ):
        sess = self._sessions.get(session_id)
        if sess is None:
            return "stale"
        if (
            sess.tty != expected_tty
            or sess.status != expected_status
            or sess.stop_reason != expected_stop_reason
            or sess.running_since != expected_running_since
        ):
            return "stale"
        if not interrupt(sess.tty):
            return "failed"
        self.set_stopped(session_id, "interrupted", interrupt_reason="timeout")
        return "interrupted"

    def add_redelivery(self, session_id, text, key=None):
        self.redeliveries.append((session_id, text, key))


class _FakeRegistry:
    def __init__(self, hitl=False, snaps=None):
        self.hitl = hitl
        self.snaps = list(snaps or [])
        self.timed_out = []

    def has_open_request(self, session_id):
        return self.hitl

    def timeout_session(self, session_id):
        self.timed_out.append(session_id)
        return list(self.snaps)


class ParseStuckThresholdTests(unittest.TestCase):
    def test_invalid_values_fall_back_to_default(self):
        for raw in ("", "abc", "-5", "0"):
            with self.subTest(raw=raw):
                self.assertEqual(parse_stuck_threshold(raw, default=123), 123)

    def test_missing_env_falls_back_to_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(parse_stuck_threshold(None, default=123), 123)

    def test_positive_value_is_used(self):
        self.assertEqual(parse_stuck_threshold("45", default=123), 45)


class SessionStoreTimeoutInterruptTests(unittest.TestCase):
    def _store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(Path(tmp.name) / "state.json")
        store.upsert("sid-1", "walkcode-1", "/x", root_msg_id="root-1", cwd_is_launch=True)
        return store

    def test_interrupt_timeout_success_persists_terminal_timeout(self):
        store = self._store()
        store.start_running("sid-1", 123.0)
        calls = []

        result = store.interrupt_timeout_if_unchanged(
            "sid-1",
            expected_tty="walkcode-1",
            expected_status=STATUS_RUNNING,
            expected_stop_reason="",
            expected_running_since=123.0,
            interrupt=lambda tty_name: calls.append(tty_name) or True,
        )

        self.assertEqual(result, "interrupted")
        self.assertEqual(calls, ["walkcode-1"])
        session = store.get("sid-1")
        self.assertEqual(session.status, STATUS_STOPPED)
        self.assertEqual(session.stop_reason, STOP_REASON_INTERRUPTED)
        self.assertEqual(session.interrupt_reason, INTERRUPT_REASON_TIMEOUT)
        self.assertEqual(session.running_since, 0.0)

        reloaded = SessionStore(store.path)
        reloaded.load()
        session = reloaded.get("sid-1")
        self.assertEqual(session.status, STATUS_STOPPED)
        self.assertEqual(session.stop_reason, STOP_REASON_INTERRUPTED)
        self.assertEqual(session.interrupt_reason, INTERRUPT_REASON_TIMEOUT)

    def test_interrupt_timeout_stale_does_not_send_or_mark(self):
        store = self._store()
        store.start_running("sid-1", 123.0)

        result = store.interrupt_timeout_if_unchanged(
            "sid-1",
            expected_tty="walkcode-1",
            expected_status=STATUS_RUNNING,
            expected_stop_reason="",
            expected_running_since=122.0,
            interrupt=lambda _tty: self.fail("stale timeout must not send Esc"),
        )

        self.assertEqual(result, "stale")
        session = store.get("sid-1")
        self.assertEqual(session.status, STATUS_RUNNING)
        self.assertEqual(session.running_since, 123.0)

    def test_interrupt_timeout_failed_send_keeps_state(self):
        store = self._store()
        store.start_running("sid-1", 123.0)

        result = store.interrupt_timeout_if_unchanged(
            "sid-1",
            expected_tty="walkcode-1",
            expected_status=STATUS_RUNNING,
            expected_stop_reason="",
            expected_running_since=123.0,
            interrupt=lambda _tty: False,
        )

        self.assertEqual(result, "failed")
        session = store.get("sid-1")
        self.assertEqual(session.status, STATUS_RUNNING)
        self.assertEqual(session.running_since, 123.0)


class BackgroundServicesTests(unittest.TestCase):
    def test_health_card_disabled_skips_stuck_watchdog(self):
        calls = []
        with patch.object(server, "_start_idle_reaper", lambda: calls.append("idle")), \
             patch.object(server, "_start_stuck_watchdog", lambda: calls.append("stuck")), \
             patch.object(server, "_start_inject_sweeper", lambda: calls.append("inject")):
            server._start_background_services(SimpleNamespace(health_card_enabled=False))
        self.assertEqual(calls, ["idle", "inject"])

    def test_health_card_enabled_starts_stuck_watchdog(self):
        calls = []
        with patch.object(server, "_start_idle_reaper", lambda: calls.append("idle")), \
             patch.object(server, "_start_stuck_watchdog", lambda: calls.append("stuck")), \
             patch.object(server, "_start_inject_sweeper", lambda: calls.append("inject")):
            server._start_background_services(SimpleNamespace(health_card_enabled=True))
        self.assertEqual(calls, ["idle", "stuck", "inject"])


class InterruptAgentTurnTests(unittest.TestCase):
    def test_interrupt_agent_turn_sends_escape_to_tmux(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stderr="")

        with patch.object(server.subprocess, "run", fake_run):
            self.assertTrue(server._interrupt_agent_turn("walkcode-1"))

        self.assertEqual(calls, [(
            ["tmux", "send-keys", "-t", "walkcode-1", "Escape"],
            {"capture_output": True, "text": True, "timeout": 5},
        )])

    def test_interrupt_agent_turn_returns_false_on_tmux_error(self):
        with patch.object(
            server.subprocess,
            "run",
            lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="no pane"),
        ):
            self.assertFalse(server._interrupt_agent_turn("walkcode-1"))

    def test_interrupt_agent_turn_returns_false_on_exception(self):
        def fail(*_args, **_kwargs):
            raise OSError("boom")

        with patch.object(server.subprocess, "run", fail):
            self.assertFalse(server._interrupt_agent_turn("walkcode-1"))


class StuckWatchdogTests(unittest.TestCase):
    def setUp(self):
        self._last_ups = dict(server._session_last_ups)
        self._last_stop = dict(server._session_last_stop)
        with server._stuck_lock:
            self._last_stuck = dict(server._stuck_alerted)
            server._stuck_alerted.clear()
        server._session_last_ups.clear()
        server._session_last_stop.clear()
        self.addCleanup(self._restore_globals)

    def _restore_globals(self):
        server._session_last_ups.clear()
        server._session_last_ups.update(self._last_ups)
        server._session_last_stop.clear()
        server._session_last_stop.update(self._last_stop)
        with server._stuck_lock:
            server._stuck_alerted.clear()
            server._stuck_alerted.update(self._last_stuck)

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
                elif sess.status == "running" or sess.stop_reason in server._WAITING_STOP_REASONS:
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

    def test_startup_grace_skips_persisted_old_timer_once(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.running_since = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []
        old_started = server._watchdog_started_at
        try:
            server._watchdog_started_at = now["t"] - 60
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
                self.assertEqual(interrupts, [])

                now["t"] = server._watchdog_started_at + server._STUCK_THRESHOLD + 1
                server._check_stuck_sessions()
        finally:
            server._watchdog_started_at = old_started

        self.assertEqual(interrupts, ["walkcode-1"])

    def test_stale_timeout_snapshot_is_not_interrupted_or_marked(self):
        now = {"t": 10_000.0}
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.running_since = now["t"] - 40 * 60
        store = _FakeStore({"sid-1": sess})
        interrupts = []

        def stale_timeout(*_args, **_kwargs):
            return "stale"

        store.interrupt_timeout_if_unchanged = stale_timeout
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

        self.assertEqual(interrupts, [])
        self.assertEqual(store.statuses, [])
        self.assertEqual(sess.status, "running")

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

    def test_timeout_denies_open_hitl_hooks_and_updates_cards(self):
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

        self.assertEqual(reg.timed_out, ["sid-1"])
        self.assertEqual([mid for mid, _ in edits], ["card-perm", "card-ask"])
        self.assertTrue(all(card["header"]["template"] == "grey" for _, card in edits))

    def test_progress_event_does_not_reopen_stopped_session(self):
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = "stopped"
        sess.stop_reason = "completed"
        sess.running_since = 0.0
        store = _FakeStore({"sid-1": sess})
        with patch.object(server, "session_store", store):
            updated = server._mark_session_progress("sid-1")
        self.assertFalse(updated)
        self.assertEqual(sess.status, "stopped")
        self.assertEqual(sess.stop_reason, "completed")
        self.assertEqual(sess.running_since, 0.0)

    def test_progress_event_does_not_reopen_timeout_session(self):
        sess = Session(tty="walkcode-1", cwd="/x", root_msg_id="root-1")
        sess.status = "stopped"
        sess.stop_reason = "interrupted"
        sess.interrupt_reason = "timeout"
        sess.running_since = 0.0
        store = _FakeStore({"sid-1": sess})
        with patch.object(server, "session_store", store):
            updated = server._mark_session_progress("sid-1")
        self.assertFalse(updated)
        self.assertEqual(sess.status, "stopped")
        self.assertEqual(sess.stop_reason, "interrupted")
        self.assertEqual(sess.interrupt_reason, "timeout")
        self.assertEqual(sess.running_since, 0.0)

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
