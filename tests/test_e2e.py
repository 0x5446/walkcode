"""End-to-end tests: full request flows through HTTP + event handlers."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from walkcode import server
from walkcode.state import SessionStore


class _Base(unittest.TestCase):
    """Shared setup: TestClient + fake _send/_reply that track calls."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"
        self._next_send_id = 1
        self._next_reply_id = 1
        self.sent = []       # [(msg_id, text_or_card)]
        self.replied = []    # [(parent_msg_id, text_or_card)]
        self.reactions = []  # [(message_id, emoji_type)]
        self.edited = []     # [(message_id, text)]

        server.session_store = SessionStore(self.state_path)
        server.session_store.load()
        self.client = TestClient(server.app)

        self.started_claudes = []  # [(prompt, message_id)]

        self._patches = [
            mock.patch("walkcode.server._send", side_effect=self._fake_send),
            mock.patch("walkcode.server._reply", side_effect=self._fake_reply),
            mock.patch("walkcode.server._add_reaction", side_effect=self._fake_add_reaction),
            mock.patch("walkcode.server._edit_message", side_effect=self._fake_edit_message),
            mock.patch("walkcode.server._start_claude", side_effect=self._fake_start_claude),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def _fake_send(self, text=""):
        msg_id = f"root-{self._next_send_id}"
        self._next_send_id += 1
        self.sent.append((msg_id, text))
        return msg_id

    def _fake_reply(self, message_id, text="", reply_in_thread=False):
        reply_id = f"reply-{self._next_reply_id}"
        self._next_reply_id += 1
        self.replied.append((message_id, text))
        return reply_id

    def _fake_add_reaction(self, message_id, emoji_type):
        self.reactions.append((message_id, emoji_type))

    def _fake_edit_message(self, message_id, text):
        self.edited.append((message_id, text))

    def _fake_start_claude(self, prompt, message_id):
        self.started_claudes.append((prompt, message_id))

    # -- helpers --

    def _post_hook(self, session_id="session-1", tty="claude-project-123",
                   cwd="/tmp/project", hook_type="stop", matcher="",
                   message="done"):
        return self.client.post("/hook", json={
            "type": hook_type,
            "tty": tty,
            "cwd": cwd,
            "session_id": session_id,
            "message": message,
            "matcher": matcher,
        })

    def _msg_event(self, *, root_id="", parent_id="", message_id="msg-1",
                   text="hello", message_type="text"):
        content = json.dumps({"text": text}) if message_type == "text" else "{}"
        return SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_test"),
            ),
            message=SimpleNamespace(
                root_id=root_id,
                parent_id=parent_id,
                message_id=message_id,
                message_type=message_type,
                content=content,
            ),
        ))



# =========================================================================
# 1. Health endpoint
# =========================================================================

class HealthTests(_Base):
    def test_health_returns_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["sessions"], 0)

    def test_health_reports_session_count(self):
        self._post_hook(session_id="s1")
        self._post_hook(session_id="s2", tty="claude-other-456")
        resp = self.client.get("/health")
        self.assertEqual(resp.json()["sessions"], 2)


# =========================================================================
# 2. Hook endpoint — new session
# =========================================================================

class HookNewSessionTests(_Base):
    def test_new_session_sends_title_and_replies_content(self):
        resp = self._post_hook()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["msg_id"], "root-1")

        # Title as root, content as reply to create thread
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 1)
        _, title = self.sent[0]
        self.assertIsInstance(title, str)
        parent_id, content = self.replied[0]
        self.assertEqual(parent_id, "root-1")
        self.assertIn("done", content)

        session = server.session_store.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")
        self.assertEqual(session.root_msg_id, "root-1")

    def test_new_session_title_contains_project_session_and_snippet(self):
        long_msg = "a]" * 20  # 40 chars, exceeds 22-char snippet limit
        self._post_hook(hook_type="stop", matcher="", cwd="/tmp/myproject",
                        session_id="abcdef1234567890", message=long_msg)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 1)
        _, title = self.sent[0]
        self.assertIn("myproject", title)
        self.assertIn("abcdef12", title)  # session_id[:8]
        self.assertIn("...", title)

    def test_permission_prompt_sends_text_like_other_notifications(self):
        self._post_hook(matcher="permission_prompt", hook_type="notification",
                        message="Claude Code needs your approval")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 1)
        _, content = self.replied[0]
        self.assertIsInstance(content, str)
        self.assertIn("Claude Code needs your approval", content)

    def test_hook_missing_tty_returns_error(self):
        resp = self.client.post("/hook", json={
            "type": "stop", "tty": "", "cwd": "/tmp", "session_id": "s1",
            "message": "", "matcher": "",
        })
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["ok"])
        self.assertEqual(len(self.sent), 0)

    def test_hook_without_session_id_sends_but_no_session_stored(self):
        resp = self.client.post("/hook", json={
            "type": "stop", "tty": "claude-project-123", "cwd": "/tmp",
            "session_id": "", "message": "done", "matcher": "",
        })
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(server.session_store.count(), 0)


# =========================================================================
# 3. Hook endpoint — existing session (thread reply)
# =========================================================================

class HookExistingSessionTests(_Base):
    def test_second_hook_replies_text_to_thread_root(self):
        self._post_hook()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 1)

        resp = self._post_hook(message="second event")
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("thread", body)
        self.assertEqual(body["thread"], "root-1")

        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 2)
        parent_msg_id, content = self.replied[1]
        self.assertEqual(parent_msg_id, "root-1")
        self.assertIsInstance(content, str)

    def test_second_hook_updates_tty(self):
        self._post_hook(tty="claude-project-123")
        self._post_hook(tty="claude-project-456")
        session = server.session_store.get("session-1")
        self.assertEqual(session.tty, "claude-project-456")


# =========================================================================
# 4. Feishu message reply → inject text
# =========================================================================

class MessageReplyTests(_Base):
    def _setup_session(self):
        self._post_hook()
        self.replied.clear()

    def test_reply_injects_text_and_reacts_success(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None), \
             mock.patch("walkcode.server.inject", return_value=True) as inj:
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="continue",
            ))
        inj.assert_called_once_with("claude-project-123", "continue")
        self.assertEqual(len(self.reactions), 1)
        self.assertEqual(self.reactions[0][0], "user-1")
        self.assertIn(self.reactions[0][1], server._SUCCESS_EMOJIS)

    def test_reply_inject_exception_reacts_failure(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None), \
             mock.patch("walkcode.server.inject", side_effect=RuntimeError("boom")):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="hello",
            ))
        self.assertEqual(len(self.reactions), 1)
        self.assertEqual(self.reactions[0][0], "user-1")
        self.assertIn(self.reactions[0][1], server._FAILURE_EMOJIS)

    def test_reply_to_unknown_thread_returns_warning(self):
        server._on_message(self._msg_event(
            root_id="unknown-root", parent_id="unknown-parent",
            message_id="user-1", text="hello",
        ))
        self.assertEqual(len(self.replied), 1)
        self.assertIn("找不到对应会话", self.replied[0][1])

    def test_non_reply_message_starts_claude(self):
        """Top-level messages (no parent/root) should trigger _start_claude."""
        server._on_message(self._msg_event(
            root_id="", parent_id="", message_id="user-1", text="hello",
        ))
        self.assertEqual(len(self.started_claudes), 1)
        self.assertEqual(self.started_claudes[0], ("hello", "user-1"))
        self.assertEqual(len(self.replied), 0)

    def test_non_reply_non_text_message_is_ignored(self):
        """Top-level non-text messages should be silently ignored."""
        server._on_message(self._msg_event(
            root_id="", parent_id="", message_id="user-1",
            text="", message_type="image",
        ))
        self.assertEqual(len(self.started_claudes), 0)

    def test_non_reply_empty_text_is_ignored(self):
        """Top-level messages with only whitespace/mentions should be ignored."""
        server._on_message(self._msg_event(
            root_id="", parent_id="", message_id="user-1", text="@_user_1 ",
        ))
        self.assertEqual(len(self.started_claudes), 0)

    def test_non_reply_strips_mention_before_start(self):
        """@mention should be stripped before passing to _start_claude."""
        server._on_message(self._msg_event(
            root_id="", parent_id="", message_id="user-1",
            text="@_user_1 build the app",
        ))
        self.assertEqual(len(self.started_claudes), 1)
        self.assertEqual(self.started_claudes[0][0], "build the app")

    def test_non_text_message_rejected(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="", message_type="image",
            ))
        self.assertEqual(len(self.replied), 1)
        self.assertIn("只支持文本", self.replied[0][1])

    def test_empty_text_reply_is_ignored(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="   ",
            ))
        self.assertEqual(len(self.replied), 0)

    def test_reply_with_dead_tmux_triggers_resume(self):
        """When tmux is dead but session exists, _resume_claude should be called."""
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value="session not found"), \
             mock.patch("walkcode.server._resume_claude") as mock_resume:
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="continue please",
            ))
        mock_resume.assert_called_once()
        args = mock_resume.call_args[0]
        self.assertEqual(args[0], "session-1")  # session_id
        self.assertEqual(args[1].tty, "claude-project-123")  # old session data
        self.assertEqual(args[2], "continue please")  # reply text
        self.assertEqual(args[3], "user-1")  # message_id

    def test_reply_strips_mention_before_inject(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None), \
             mock.patch("walkcode.server.inject", return_value=True) as inj:
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="@_user_1 continue working",
            ))
        inj.assert_called_once_with("claude-project-123", "continue working")

    def test_reply_mention_only_is_ignored(self):
        self._setup_session()
        with mock.patch("walkcode.server.validate_target", return_value=None):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="@_user_1 ",
            ))
        self.assertEqual(len(self.reactions), 0)


# =========================================================================
# 5. Multi-session routing
# =========================================================================

class MultiSessionTests(_Base):
    def test_two_sessions_route_replies_to_correct_terminals(self):
        self._post_hook(session_id="s1", tty="claude-proj-a-111", cwd="/tmp/proj-a")
        self._post_hook(session_id="s2", tty="claude-proj-b-222", cwd="/tmp/proj-b")
        self.replied.clear()

        injected_targets = []

        def fake_inject(session_name, text):
            injected_targets.append((session_name, text))
            return True

        with mock.patch("walkcode.server.validate_target", return_value=None), \
             mock.patch("walkcode.server.inject", side_effect=fake_inject):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="u1", text="for proj-a",
            ))

        self.assertEqual(injected_targets, [("claude-proj-a-111", "for proj-a")])

        injected_targets.clear()
        with mock.patch("walkcode.server.validate_target", return_value=None), \
             mock.patch("walkcode.server.inject", side_effect=fake_inject):
            server._on_message(self._msg_event(
                root_id="root-2", parent_id="root-2",
                message_id="u2", text="for proj-b",
            ))

        self.assertEqual(injected_targets, [("claude-proj-b-222", "for proj-b")])

    def test_reply_to_wrong_thread_does_not_cross_inject(self):
        self._post_hook(session_id="s1", tty="claude-proj-123")
        self.replied.clear()

        server._on_message(self._msg_event(
            root_id="root-999", parent_id="root-999",
            message_id="u1", text="stray",
        ))
        self.assertIn("找不到对应会话", self.replied[0][1])


# =========================================================================
# 6. Session persistence across restart
# =========================================================================

class PersistenceTests(_Base):
    def test_session_survives_store_reload(self):
        self._post_hook(session_id="s1", tty="claude-proj-123")

        # Simulate restart
        server.session_store = SessionStore(self.state_path)
        server.session_store.load()

        session = server.session_store.get("s1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-proj-123")
        self.assertEqual(session.root_msg_id, "root-1")

        # Can still resolve thread mapping
        resolved = server.session_store.resolve(root_id="root-1")
        self.assertEqual(resolved, "s1")

    def test_hook_after_restart_replies_to_existing_thread(self):
        self._post_hook(session_id="s1")

        server.session_store = SessionStore(self.state_path)
        server.session_store.load()
        self.replied.clear()

        resp = self._post_hook(session_id="s1", message="after restart")
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(resp.json()["thread"], "root-1")


# =========================================================================
# 7. Feishu-initiated Claude Code (pending_roots)
# =========================================================================

class FeishuInitiatedTests(_Base):
    def test_pending_root_links_to_feishu_thread(self):
        """When pending entry exists, hook should reuse that root."""
        server.session_store.add_pending("walkcode-12345", "feishu-msg-100")

        resp = self._post_hook(
            session_id="s1", tty="walkcode-12345",
            cwd="/tmp/project", message="started",
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["msg_id"], "feishu-msg-100")

        # Should NOT create a new root message
        self.assertEqual(len(self.sent), 0)
        # Should reply to existing Feishu message
        self.assertEqual(len(self.replied), 1)
        parent_id, content = self.replied[0]
        self.assertEqual(parent_id, "feishu-msg-100")
        self.assertIn("started", content)

        # Session should be stored with the Feishu root
        session = server.session_store.get("s1")
        self.assertIsNotNone(session)
        self.assertEqual(session.root_msg_id, "feishu-msg-100")

        # pending should be consumed
        root, _ = server.session_store.pop_pending("walkcode-12345")
        self.assertIsNone(root)

    def test_pending_root_edits_launch_reply_with_session_id(self):
        """When pending root matches, the launch reply should be updated with session_id."""
        server.session_store.add_pending("walkcode-12345", "feishu-msg-100", reply_id="reply-launch-1")

        self._post_hook(
            session_id="s1", tty="walkcode-12345",
            cwd="/tmp/project", message="started",
        )

        self.assertEqual(len(self.edited), 1)
        msg_id, text = self.edited[0]
        self.assertEqual(msg_id, "reply-launch-1")
        self.assertIn("s1"[:8], text)
        self.assertIn("walkcode-12345", text)

    def test_pending_root_not_consumed_by_wrong_tty(self):
        """pending entry should only match by tmux session name."""
        server.session_store.add_pending("walkcode-12345", "feishu-msg-100")

        resp = self._post_hook(
            session_id="s1", tty="walkcode-other",
            cwd="/tmp/project", message="started",
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        # Should create a new root (not reuse pending)
        self.assertEqual(len(self.sent), 1)
        root, _ = server.session_store.pop_pending("walkcode-12345")
        self.assertEqual(root, "feishu-msg-100")

    def test_subsequent_hook_after_pending_replies_to_thread(self):
        """After pending linking, subsequent hooks reply to same thread."""
        server.session_store.add_pending("walkcode-12345", "feishu-msg-100")

        # First hook: consumes pending root
        self._post_hook(
            session_id="s1", tty="walkcode-12345",
            cwd="/tmp/project", message="first",
        )
        self.replied.clear()

        # Second hook: existing session, should reply to feishu-msg-100
        resp = self._post_hook(
            session_id="s1", tty="walkcode-12345",
            cwd="/tmp/project", message="second",
        )
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["thread"], "feishu-msg-100")
        self.assertEqual(len(self.replied), 1)
        self.assertEqual(self.replied[0][0], "feishu-msg-100")


# =========================================================================
# 8. Idle reaper
# =========================================================================

class IdleReaperTests(_Base):
    def test_reap_kills_idle_session_and_notifies(self):
        self._post_hook(session_id="s1", tty="walkcode-old")
        import time
        idle_time = time.time() - server._IDLE_TIMEOUT - 100  # well past timeout

        with mock.patch("walkcode.server.get_session_activity", return_value=idle_time), \
             mock.patch("walkcode.server.kill_session") as mock_kill:
            # Unpatch _reply to use our fake
            server._reap_idle_sessions()

        mock_kill.assert_called_once_with("walkcode-old")
        # Should have notified in the thread
        notify_replies = [r for r in self.replied if "无活动已关闭" in str(r[1])]
        self.assertEqual(len(notify_replies), 1)

    def test_reap_skips_active_session(self):
        self._post_hook(session_id="s1", tty="walkcode-active")
        import time

        with mock.patch("walkcode.server.get_session_activity", return_value=time.time()), \
             mock.patch("walkcode.server.kill_session") as mock_kill:
            server._reap_idle_sessions()

        mock_kill.assert_not_called()

    def test_reap_skips_dead_session(self):
        self._post_hook(session_id="s1", tty="walkcode-dead")

        with mock.patch("walkcode.server.get_session_activity", return_value=None), \
             mock.patch("walkcode.server.kill_session") as mock_kill:
            server._reap_idle_sessions()

        mock_kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
