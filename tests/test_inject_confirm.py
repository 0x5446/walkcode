"""Regression tests for Feishu reply injection semantics.

WalkCode treats a Feishu reply as delivered once tmux accepts paste+Enter.
Claude Code may run it immediately or queue it behind the current turn; that is
Claude Code's decision. UserPromptSubmit is kept only as best-effort
observation, so missing or late hooks must not create user-visible queued or
swallowed notices.
"""

import asyncio
import json
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from walkcode import server
from walkcode import tty as tty_mod
from walkcode.config import Config
from walkcode.i18n import t
from walkcode.state import SessionStore


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

    def test_ack_inject_accepted_adds_success_reaction(self):
        server._ack_inject_accepted("msg1")

        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)
        self.assertEqual(self.replies, [])

    def test_register_pending_inject_is_observation_only_even_when_busy(self):
        server._mark_session_busy("sess1")
        server._register_pending_inject("sess1", "tty1", "queued by claude", "msg1")

        self.assertEqual(len(server._pending_injects), 1)
        self.assertEqual(self.replies, [])
        self.assertEqual(self.reactions, [])

    def test_userpromptsubmit_observation_removes_pending_without_extra_reaction(self):
        server._register_pending_inject("sess1", "tty1", "hello   world", "msg1")
        server._confirm_pending_inject("sess1", "tty1", "please: hello world\n")

        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies, [])
        self.assertEqual(self.reactions, [])

    def test_observation_expiry_is_silent(self):
        server._register_pending_inject("sess1", "tty1", "no hook", "msg1")
        with server._pending_lock:
            server._pending_injects[0]["injected_at"] = \
                time.time() - (server._INJECT_OBSERVATION_TTL + 1)

        server._sweep_pending_injects()

        self.assertEqual(len(server._pending_injects), 0)
        self.assertEqual(self.replies, [])
        self.assertEqual(self.reactions, [])

    def test_busy_idle_tracking_from_hooks(self):
        server._mark_session_busy("s")
        self.assertTrue(server._is_session_busy("s"))
        server._mark_session_idle("s")
        self.assertFalse(server._is_session_busy("s"))

    def test_progress_hook_marks_session_busy(self):
        class _Req:
            async def json(self):
                return {"type": "subagent-stop", "session_id": "s", "tty": "tmux1"}

        busy = []
        refreshes = []
        with patch.object(server, "_mark_session_progress", lambda sid: busy.append(sid) or True), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or False):
            res = asyncio.run(server.receive_progress_hook(_Req()))

        self.assertEqual(res, {"ok": True, "updated": True})
        self.assertEqual(busy, ["s"])
        self.assertEqual([sid for sid, _ in refreshes], ["s"])

    def test_progress_hook_does_not_reset_human_waiting_timeout(self):
        for reason in ("permission_request", "ask_user_question"):
            with self.subTest(reason=reason), TemporaryDirectory() as d:
                store = SessionStore(Path(d) / "state.json")
                store.upsert("s", tty="tmux1", cwd="/tmp/proj", root_msg_id="root1")
                store.mark_waiting("s", reason, 111.0)

                class _Req:
                    async def json(self):
                        return {"type": "subagent-stop", "session_id": "s", "tty": "tmux1"}

                refreshes = []
                with patch.object(server, "session_store", store), \
                     patch.object(server, "_refresh_health_card_for_event",
                                  lambda sid, **kw: refreshes.append((sid, kw)) or False), \
                     patch.object(server.time, "time", lambda: 999.0):
                    res = asyncio.run(server.receive_progress_hook(_Req()))

                sess = store.get("s")
                self.assertEqual(res, {"ok": True, "updated": False})
                self.assertEqual(sess.status, "stopped")
                self.assertEqual(sess.stop_reason, reason)
                self.assertEqual(sess.running_since, 111.0)
                self.assertEqual([sid for sid, _ in refreshes], ["s"])


class FeishuReplyInjectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._orig_config = server.config
        self._orig_store = server.session_store
        server.config = Config(
            feishu_app_id="x", feishu_app_secret="y",
            feishu_receive_id="user-open-id", feishu_receive_id_type="open_id",
        )
        server.session_store = SessionStore(Path(self._tmp.name) / "state.json")
        server.session_store.upsert("sess1", tty="tmux1", cwd="/tmp/proj", root_msg_id="root1")

        def _restore():
            server.config = self._orig_config
            server.session_store = self._orig_store
        self.addCleanup(_restore)

        with server._pending_lock:
            server._pending_injects.clear()
            server._session_last_ups.clear()
            server._session_last_stop.clear()

        self.injected = []
        self.reactions = []
        self.replies = []
        # Default: the close-the-loop verify reports a clean submit (box cleared).
        # Individual tests override self.verify_result to exercise STUCK / menu paths.
        self.verify_result = server.INPUT_EMPTY
        patches = [
            patch.object(server, "validate_target", lambda tty: None),
            patch.object(server, "is_agent_alive", lambda tty: True),
            patch.object(server, "inject", lambda tty, text: self.injected.append((tty, text))),
            patch.object(server, "verify_submitted",
                         lambda tty, text, **kw: self.verify_result),
            patch.object(server, "capture_pane", lambda tty, lines=30: ""),
            patch.object(server, "_add_reaction",
                         lambda mid, emoji: self.reactions.append((mid, emoji))),
            patch.object(server, "_reply",
                         lambda mid, text, reply_in_thread=False: self.replies.append((mid, text))),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _data(self, text="hello", message_id="msg1"):
        msg = types.SimpleNamespace(
            parent_id="root1",
            root_id="root1",
            message_id=message_id,
            message_type="text",
            content=json.dumps({"text": text}),
        )
        sender = types.SimpleNamespace(
            sender_id=types.SimpleNamespace(open_id="user-open-id")
        )
        return types.SimpleNamespace(event=types.SimpleNamespace(sender=sender, message=msg))

    def test_inject_success_is_acknowledged_at_tmux_boundary(self):
        server._handle_message(self._data("？"))

        self.assertEqual(self.injected, [("tmux1", "？")])
        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)
        self.assertEqual(len(server._pending_injects), 1)

    def test_inject_success_marks_busy_and_unfreezes_health_card(self):
        server.session_store.set_stopped("sess1", "completed")
        server._mark_session_idle("sess1")

        server._handle_message(self._data("继续"))

        self.assertTrue(server._is_session_busy("sess1"))
        self.assertEqual(server.session_store.get("sess1").status, "running")

    def test_busy_session_does_not_send_queued_receipt(self):
        server._mark_session_busy("sess1")

        server._handle_message(self._data("next prompt"))

        self.assertEqual(self.injected, [("tmux1", "next prompt")])
        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.reactions), 1)

    def test_tmux_inject_failure_still_reports_failure(self):
        def fail_inject(_tty, _text):
            raise RuntimeError("tmux failed")

        with patch.object(server, "inject", fail_inject):
            server._handle_message(self._data("hello"))

        self.assertEqual(self.injected, [])
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_timeout"))])
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._FAILURE_EMOJIS)
        self.assertEqual(len(server._pending_injects), 0)

    def test_stuck_on_idle_session_reports_not_submitted(self):
        # The 13:50 bug: Enter dropped on an idle pane, text stuck in the box.
        # tmux accepted the keys (inject succeeds) but the prompt never submitted.
        self.verify_result = server.STUCK  # idle: no busy mark, capture_pane -> ""

        server._handle_message(self._data("仔细全面的更新adr了吗？"))

        self.assertEqual(self.injected, [("tmux1", "仔细全面的更新adr了吗？")])
        self.assertEqual(self.replies, [("msg1", t("feishu.inject_not_submitted"))])
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._FAILURE_EMOJIS)
        self.assertEqual(len(server._pending_injects), 0)

    def test_stuck_but_busy_is_treated_as_queued_success(self):
        # A turn is running: the message is queued, not lost. Don't cry failure.
        self.verify_result = server.STUCK
        server._mark_session_busy("sess1")

        server._handle_message(self._data("next prompt"))

        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)
        self.assertEqual(len(server._pending_injects), 1)

    def test_menu_unconfirmed_falls_back_to_accept_boundary(self):
        # Bottom is a menu/dialog — can't confirm and must not press Enter blindly.
        # Fall back to the legacy tmux-accept boundary (no worse than before).
        self.verify_result = tty_mod.INPUT_MENU

        server._handle_message(self._data("hello"))

        self.assertEqual(self.replies, [])
        self.assertEqual(len(self.reactions), 1)
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)
        self.assertEqual(len(server._pending_injects), 1)


if __name__ == "__main__":
    unittest.main()
