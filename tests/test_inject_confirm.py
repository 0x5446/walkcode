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
        self.assertEqual(self.replies, [])
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
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_swallowed"))])

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

    # --- backstop ---

    def test_absolute_backstop_fails_even_if_never_idle(self):
        """A session that never goes idle still fails after the absolute max wait."""
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "stuck", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_CONFIRM_MAX + 1)
        server._sweep_pending_injects()
        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(len(self.replies), 1)

    # --- busy/idle helper ---

    def test_busy_idle_tracking_from_hooks(self):
        server._mark_session_busy("s")
        self.assertTrue(server._is_session_busy("s"))
        server._mark_session_idle("s")
        self.assertFalse(server._is_session_busy("s"))


if __name__ == "__main__":
    unittest.main()
