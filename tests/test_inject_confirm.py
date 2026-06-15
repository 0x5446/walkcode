"""Regression tests for inject delivery confirmation.

Background: a successful `tmux send-keys` only means bytes reached the pane —
NOT that Claude accepted them as a prompt. A modal (/status, /model, a
permission prompt), copy-mode, or a 100%-context state silently swallows the
keystrokes. walkcode used to add a success emoji based purely on the send-keys
exit code, so messages vanished while the user saw a "delivered" reaction
(observed: a Feishu reply that got a handshake emoji but never reached the
terminal — the injected Enter had merely dismissed an open /status dialog).

Delivery is now confirmed via the UserPromptSubmit hook: after injecting we wait
for that session to report a matching prompt. If it never arrives the message
was swallowed and we tell the user instead of faking success. Busy/idle is
derived from hooks only (UserPromptSubmit = busy, Stop = idle) so an inject
during generation is treated as queued (not lost). These tests pin that machine.
"""

import time
import unittest
from unittest.mock import patch

from walkcode import server
from walkcode.i18n import t


class InjectConfirmTests(unittest.TestCase):
    def setUp(self):
        with server._pending_lock:
            server._pending_injects.clear()
            server._session_last_ups.clear()
            server._session_last_stop.clear()
            server._ups_capable_sessions.clear()
        self.reactions = []  # (message_id, emoji)
        self.replies = []    # (message_id, text)
        p1 = patch.object(server, "_add_reaction",
                          lambda mid, emoji: self.reactions.append((mid, emoji)))
        p2 = patch.object(server, "_reply",
                          lambda mid, text, reply_in_thread=False: self.replies.append((mid, text)))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    # --- delivered ---

    def test_idle_inject_confirmed_by_userpromptsubmit(self):
        """Idle inject + matching UserPromptSubmit → success emoji, no failure."""
        server._register_pending_inject("sess1", "tty1", "hello world", "msg1")
        server._confirm_pending_inject("sess1", "tty1", "hello world")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)
        self.assertEqual(self.replies, [])

    def test_match_is_whitespace_tolerant_and_substring(self):
        """Prompt echoed back with different whitespace / surrounding text still matches."""
        server._register_pending_inject("sess1", "tty1", "hello   world", "msg1")
        server._confirm_pending_inject("sess1", "tty1", "please: hello world\n")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)

    def test_confirm_by_tty_when_session_id_absent(self):
        """Pending registered without a session_id confirms via tty fallback."""
        server._register_pending_inject(None, "tty1", "via tty", "msg1")
        server._confirm_pending_inject("", "tty1", "via tty")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertIn(self.reactions[-1][1], server._SUCCESS_EMOJIS)

    def test_does_not_confirm_other_session(self):
        """A UserPromptSubmit from a different session/tty must not confirm."""
        server._register_pending_inject("sessA", "ttyA", "shared text", "msgA")
        server._confirm_pending_inject("sessB", "ttyB", "shared text")
        self.assertEqual(len(server._pending_injects), 1)
        self.assertEqual(self.reactions, [])

    # --- swallowed (idle) ---

    def test_idle_inject_without_ups_reported_swallowed(self):
        """Idle inject, no UserPromptSubmit, grace elapsed → failure text + emoji."""
        server._ups_capable_sessions.add("sess1")  # session is known to emit UPS
        server._register_pending_inject("sess1", "tty1", "lost msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_CONFIRM_GRACE + 1)
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_swallowed"))])
        self.assertIn(self.reactions[-1][1], server._FAILURE_EMOJIS)

    def test_idle_inject_within_grace_not_yet_failed(self):
        """Before the grace window elapses, an unconfirmed inject stays pending."""
        server._register_pending_inject("sess1", "tty1", "still waiting", "msg1")
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1)
        self.assertEqual(self.replies, [])

    # --- queued (busy) ---

    def test_busy_inject_is_queued_not_failed_then_confirmed(self):
        """Inject during generation must not be failed; confirms after the turn ends."""
        server._mark_session_busy("sess1")          # a turn is in progress
        server._register_pending_inject("sess1", "tty1", "queued msg", "msg1")
        with server._pending_lock:                  # way past grace, but still busy
            server._pending_injects[0]["injected_at"] = time.time() - 60
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1,
                         "queued inject must not be failed while the session is busy")
        # The only feedback so far is the immediate "queued" receipt — no failure.
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_queued"))])
        # turn ends; the queued prompt fires its UserPromptSubmit
        server._mark_session_idle("sess1")
        server._confirm_pending_inject("sess1", "tty1", "queued msg")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertIn(self.reactions[-1][1], server._SUCCESS_EMOJIS)

    def test_busy_inject_failed_if_no_ups_after_turn_ends(self):
        """If, after the turn ends, no UserPromptSubmit arrives within grace → swallowed."""
        now = time.time()
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "queued lost", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = now - 30
            server._session_last_stop["sess1"] = now - (server._INJECT_CONFIRM_GRACE + 1)
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        # First the queued receipt (busy at inject), then the swallowed notice.
        self.assertEqual(self.replies, [
            ("msg1", t("feishu.inject_queued")),
            ("msg1", t("feishu.inject_swallowed")),
        ])

    def test_legacy_session_without_hook_assumed_delivered(self):
        """A session that never emits UserPromptSubmit (predates the hook) must not
        be cried-wolf about: window elapses → assume delivered, no failure text."""
        # note: NOT added to _ups_capable_sessions
        server._register_pending_inject("oldsess", "ttyX", "legacy msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_CONFIRM_GRACE + 1)
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies, [])  # no false "not delivered"
        self.assertIn(self.reactions[-1][1], server._SUCCESS_EMOJIS)

    # --- busy long turn: no false failure, late confirm, robust dead detection ---

    def test_busy_longturn_alive_not_failed(self):
        """A queued inject whose turn is still running (session alive) must NOT be
        failed even long past the old fixed backstop — it stays pending."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "long turn msg", "msg1")
        with server._pending_lock:  # well past the liveness-probe threshold, still busy
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        with patch.object(server, "probe_agent_liveness", lambda t: "alive"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1,
                         "a still-running turn on a live session must keep waiting")
        # Only the immediate queued receipt — no false "not delivered".
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_queued"))])

    def test_busy_longturn_late_confirm(self):
        """The queued prompt's UserPromptSubmit, arriving minutes later, still
        confirms — the pending entry was never prematurely removed (core regression)."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "late msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        with patch.object(server, "probe_agent_liveness", lambda t: "alive"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1)
        # turn ends much later; the queued prompt fires its UserPromptSubmit
        server._mark_session_idle("sess1")
        server._confirm_pending_inject("sess1", "tty1", "late msg")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertIn(self.reactions[-1][1], server._SUCCESS_EMOJIS)
        texts = [r[1] for r in self.replies]
        self.assertNotIn(t("feishu.inject_swallowed"), texts)
        self.assertNotIn(t("feishu.inject_timeout"), texts)

    def test_busy_probe_unknown_keeps_waiting(self):
        """An inconclusive probe (tmux timeout/error) must NOT be treated as death —
        the queued message keeps waiting, dead streak stays at 0."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "blip msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        with patch.object(server, "probe_agent_liveness", lambda t: "unknown"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1, "unknown probe must keep waiting")
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_queued"))])
        self.assertEqual(server._pending_injects[0]["dead_probes"], 0)

    def test_single_dead_probe_does_not_reclaim(self):
        """One 'dead' read is not enough — a single tmux blip can't steal a message."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "one blip", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        with patch.object(server, "probe_agent_liveness", lambda t: "dead"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1, "one dead read must not reclaim")
        self.assertEqual(server._pending_injects[0]["dead_probes"], 1)
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_queued"))])

    def test_busy_turn_never_ends_dead_session_times_out(self):
        """A confirmed-dead session (turn can never end) is reclaimed as a timeout
        once _INJECT_DEAD_PROBES consecutive 'dead' reads accumulate."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "orphan msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
            # one 'dead' read already banked; this sweep's read makes it consecutive
            server._pending_injects[0]["dead_probes"] = server._INJECT_DEAD_PROBES - 1
        with patch.object(server, "probe_agent_liveness", lambda t: "dead"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies[-1], ("msg1", t("feishu.inject_timeout")))
        self.assertIn(self.reactions[-1][1], server._FAILURE_EMOJIS)
        # exactly the queued receipt then the timeout — no swallowed text mixed in
        self.assertEqual(self.replies, [
            ("msg1", t("feishu.inject_queued")),
            ("msg1", t("feishu.inject_timeout")),
        ])

    def test_dead_probe_handed_back_when_turn_ends_during_probe(self):
        """If Stop arrives during the (lock-free) probe, the entry is handed back to
        the Stop-grace path, not failed as a dead-session timeout — and a late
        confirm still succeeds (core race fix from round-2 review)."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "race msg", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
            server._pending_injects[0]["dead_probes"] = server._INJECT_DEAD_PROBES - 1

        def probe_then_stop(_tty):
            # simulate Stop landing while the lock is released for the probe
            server._mark_session_idle("sess1")
            return "dead"

        with patch.object(server, "probe_agent_liveness", probe_then_stop):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1, "Stop during probe must hand back")
        self.assertNotIn(t("feishu.inject_timeout"), [r[1] for r in self.replies])
        self.assertEqual(server._pending_injects[0]["dead_probes"], 0)
        # the late UserPromptSubmit still confirms
        server._confirm_pending_inject("sess1", "tty1", "race msg")
        self.assertEqual(len(server._pending_injects), 0)
        self.assertIn(self.reactions[-1][1], server._SUCCESS_EMOJIS)

    def test_unknown_resets_dead_streak(self):
        """A 'dead' read then an inconclusive 'unknown' must reset the streak, so the
        unknown can't be miscounted toward the two-consecutive-deaths reclaim."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "flap", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        # first sweep: one 'dead' → streak 1, not reclaimed
        with patch.object(server, "probe_agent_liveness", lambda t: "dead"):
            server._sweep_pending_injects()
        self.assertEqual(server._pending_injects[0]["dead_probes"], 1)
        # clear the interval throttle so the next sweep probes again
        with server._pending_lock:
            server._pending_injects[0]["last_liveness_check"] = 0.0
        # second sweep: 'unknown' resets the streak — no reclaim, no timeout
        with patch.object(server, "probe_agent_liveness", lambda t: "unknown"):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 1)
        self.assertEqual(server._pending_injects[0]["dead_probes"], 0)
        self.assertNotIn(t("feishu.inject_timeout"), [r[1] for r in self.replies])

    def test_throttle_skips_probe_within_interval(self):
        """A pending probed recently is not re-probed until the interval elapses
        (prevents the 1s sweeper from forking tmux every tick)."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "throttled", "msg1")
        now = time.time()
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = now - (server._INJECT_LIVENESS_AFTER + 30)
            server._pending_injects[0]["last_liveness_check"] = now  # just probed
        calls = []
        with patch.object(server, "probe_agent_liveness",
                          lambda t: (calls.append(t), "dead")[1]):
            server._sweep_pending_injects()
        self.assertEqual(calls, [], "must not re-probe within the interval")
        self.assertEqual(len(server._pending_injects), 1)

    def test_probe_called_once_per_tty(self):
        """Multiple queued messages on the same tty trigger ONE liveness probe per
        sweep (no fork-per-message storm)."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "ttyShared", "m1", "msg1")
        server._register_pending_inject("sess1", "ttyShared", "m2", "msg2")
        old = time.time() - (server._INJECT_LIVENESS_AFTER + 30)
        with server._pending_lock:
            for pe in server._pending_injects:
                pe["injected_at"] = old
        calls = []
        with patch.object(server, "probe_agent_liveness",
                          lambda t: (calls.append(t), "alive")[1]):
            server._sweep_pending_injects()
        self.assertEqual(calls, ["ttyShared"], "same tty must be probed exactly once")
        self.assertEqual(len(server._pending_injects), 2)

    def test_busy_absolute_leakguard_times_out_without_probing(self):
        """An ever-busy, still-alive session (a stuck modal is indistinguishable
        from a very long turn) is reclaimed by the absolute leak-guard, no probe."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "stuck", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_CONFIRM_MAX + 1)
        probed = []
        with patch.object(server, "probe_agent_liveness",
                          lambda t: (probed.append(t), "alive")[1]):
            server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies[-1], ("msg1", t("feishu.inject_timeout")))
        self.assertEqual(probed, [], "leak-guard reclaim must not probe")

    # --- queued receipt (immediate busy ack) ---

    def test_busy_inject_sends_immediate_queued_receipt(self):
        """A mid-turn inject is acknowledged at once so the sender isn't left in
        silence; no reaction yet (delivery verdict still deferred)."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "while busy", "msg1")
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_queued"))])
        self.assertEqual(self.reactions, [])
        self.assertEqual(len(server._pending_injects), 1)

    def test_idle_inject_sends_no_queued_receipt(self):
        """An idle inject submits immediately — no need for a 'queued' receipt."""
        server._register_pending_inject("sess1", "tty1", "while idle", "msg1")
        self.assertEqual(self.replies, [])

    # --- busy/idle helper ---

    def test_busy_idle_tracking_from_hooks(self):
        server._mark_session_busy("s")
        self.assertTrue(server._is_session_busy("s"))
        server._mark_session_idle("s")
        self.assertFalse(server._is_session_busy("s"))


if __name__ == "__main__":
    unittest.main()
