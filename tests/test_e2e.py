"""End-to-end tests: full request flows through HTTP + event handlers."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

from agent_hotline import server
from agent_hotline.state import SessionStore


class _Base(unittest.TestCase):
    """Shared setup: TestClient + fake _send/_reply that track calls."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"
        self._next_send_id = 1
        self._next_reply_id = 1
        self.sent = []       # [(msg_id, text_or_card)]
        self.replied = []    # [(parent_msg_id, text_or_card)]

        server.session_store = SessionStore(self.state_path)
        server.session_store.load()
        self.client = TestClient(server.app)

        self._patches = [
            mock.patch("agent_hotline.server._send", side_effect=self._fake_send),
            mock.patch("agent_hotline.server._reply", side_effect=self._fake_reply),
            mock.patch("agent_hotline.server._tag_terminal"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def _fake_send(self, text="", card=None):
        msg_id = f"root-{self._next_send_id}"
        self._next_send_id += 1
        self.sent.append((msg_id, card or text))
        return msg_id

    def _fake_reply(self, message_id, text="", card=None):
        reply_id = f"reply-{self._next_reply_id}"
        self._next_reply_id += 1
        self.replied.append((message_id, card or text))
        return reply_id

    # -- helpers --

    def _post_hook(self, session_id="session-1", tty="/dev/ttys001",
                   cwd="/tmp/project", hook_type="stop", matcher="",
                   message="done", tty_pid=1234,
                   tty_pid_started_at="Fri Mar  6 13:00:10 2026"):
        return self.client.post("/hook", json={
            "type": hook_type,
            "tty": tty,
            "cwd": cwd,
            "session_id": session_id,
            "message": message,
            "matcher": matcher,
            "tty_pid": tty_pid,
            "tty_pid_started_at": tty_pid_started_at,
        })

    def _msg_event(self, *, root_id="", parent_id="", message_id="msg-1",
                   text="hello", message_type="text"):
        content = json.dumps({"text": text}) if message_type == "text" else "{}"
        return SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(
            root_id=root_id,
            parent_id=parent_id,
            message_id=message_id,
            message_type=message_type,
            content=content,
            sender=SimpleNamespace(
                sender_id=SimpleNamespace(open_id="ou_test"),
            ),
        )))

    def _card_event(self, cmd="y", sid="session-1"):
        return SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value={"cmd": cmd, "sid": sid}),
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
        self._post_hook(session_id="s2", tty="/dev/ttys002")
        resp = self.client.get("/health")
        self.assertEqual(resp.json()["sessions"], 2)


# =========================================================================
# 2. Hook endpoint — new session
# =========================================================================

class HookNewSessionTests(_Base):
    def test_new_session_sends_text_root_and_card_reply(self):
        resp = self._post_hook()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["msg_id"], "root-1")

        # Text root sent, card replied to root
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 1)
        _, text = self.sent[0]
        self.assertIsInstance(text, str)
        parent_id, card = self.replied[0]
        self.assertEqual(parent_id, "root-1")
        self.assertIsInstance(card, dict)

        session = server.session_store.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "/dev/ttys001")
        self.assertEqual(session.root_msg_id, "root-1")
        self.assertEqual(session.tty_pid, 1234)

    def test_new_session_card_has_correct_title_and_color(self):
        self._post_hook(hook_type="stop", matcher="", cwd="/tmp/myproject")
        # Card is in the reply (first reply to text root)
        _, card = self.replied[0]
        self.assertIn("myproject", card["header"]["title"]["content"])
        self.assertEqual(card["header"]["template"], "green")

    def test_permission_prompt_card_has_buttons(self):
        self._post_hook(matcher="permission_prompt", hook_type="notification")
        _, card = self.replied[0]
        self.assertEqual(card["header"]["template"], "red")
        action_elements = [e for e in card["elements"] if e["tag"] == "action"]
        self.assertEqual(len(action_elements), 1)
        buttons = action_elements[0]["actions"]
        cmds = [b["value"]["cmd"] for b in buttons]
        self.assertEqual(cmds, ["y", "n", "a"])

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
            "type": "stop", "tty": "/dev/ttys001", "cwd": "/tmp",
            "session_id": "", "message": "done", "matcher": "",
        })
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(server.session_store.count(), 0)


# =========================================================================
# 3. Hook endpoint — existing session (thread reply)
# =========================================================================

class HookExistingSessionTests(_Base):
    def test_second_hook_replies_to_thread_root(self):
        self._post_hook()
        self.assertEqual(len(self.sent), 1)
        # First hook: text root (sent) + card reply
        self.assertEqual(len(self.replied), 1)

        resp = self._post_hook(message="second event")
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("thread", body)
        self.assertEqual(body["thread"], "root-1")

        # sent once (first hook text root), replied twice (first hook card + second hook card)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.replied), 2)
        parent_msg_id, _ = self.replied[1]
        self.assertEqual(parent_msg_id, "root-1")

    def test_second_hook_updates_tty(self):
        self._post_hook(tty="/dev/ttys001")
        self._post_hook(tty="/dev/ttys005")
        session = server.session_store.get("session-1")
        self.assertEqual(session.tty, "/dev/ttys005")


# =========================================================================
# 4. Feishu message reply → inject text
# =========================================================================

class MessageReplyTests(_Base):
    def _setup_session(self):
        self._post_hook()
        # Clear tracking from hook
        self.replied.clear()

    def test_reply_injects_text_and_confirms(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", return_value=True) as inj:
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="continue",
            ))
        inj.assert_called_once_with("/dev/ttys001", "continue")
        self.assertEqual(self.replied[0], ("user-1", "✅ 已送达 /dev/ttys001"))

    def test_reply_inject_exception_returns_error(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", side_effect=RuntimeError("boom")):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="hello",
            ))
        self.assertIn("❌", self.replied[0][1])

    def test_reply_to_unknown_thread_returns_warning(self):
        server._on_message(self._msg_event(
            root_id="unknown-root", parent_id="unknown-parent",
            message_id="user-1", text="hello",
        ))
        self.assertEqual(len(self.replied), 1)
        self.assertIn("找不到对应会话", self.replied[0][1])

    def test_non_reply_message_is_ignored(self):
        """Top-level messages (no parent/root) should not inject or error."""
        server._on_message(self._msg_event(
            root_id="", parent_id="", message_id="user-1", text="hello",
        ))
        self.assertEqual(len(self.replied), 0)

    def test_non_text_message_rejected(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="", message_type="image",
            ))
        self.assertEqual(len(self.replied), 1)
        self.assertIn("只支持文本", self.replied[0][1])

    def test_empty_text_reply_is_ignored(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="   ",
            ))
        self.assertEqual(len(self.replied), 0)

    def test_reply_with_invalid_tty_returns_error(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value="TTY does not exist"):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="hello",
            ))
        self.assertIn("❌", self.replied[0][1])

    def test_reply_with_stale_session_returns_warning(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("process_missing", None)):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="hello",
            ))
        self.assertIn("终端映射已失效", self.replied[0][1])

    def test_reply_refreshes_tty_from_live_pid(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys009")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", return_value=True) as inj:
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="user-1", text="go",
            ))
        inj.assert_called_once_with("/dev/ttys009", "go")
        self.assertEqual(server.session_store.get("session-1").tty, "/dev/ttys009")


# =========================================================================
# 5. Card action (button click) → inject command
# =========================================================================

class CardActionTests(_Base):
    def _setup_session(self):
        self._post_hook()

    def test_card_action_injects_and_returns_success(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", return_value=True) as inj:
            resp = server._on_card_action(self._card_event(cmd="y", sid="session-1"))
        inj.assert_called_once_with("/dev/ttys001", "y")
        self.assertEqual(resp.toast.type, "success")
        self.assertIn("已送达", resp.toast.content)
        self.assertEqual(resp.card.data["header"]["template"], "green")
        self.assertEqual(resp.card.data["header"]["title"]["content"], "🔐 权限确认 → y")

    def test_card_action_inject_failure_returns_error(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", side_effect=RuntimeError("no tab")):
            resp = server._on_card_action(self._card_event(cmd="y", sid="session-1"))
        self.assertEqual(resp.toast.type, "error")
        self.assertIn("注入失败", resp.toast.content)

    def test_card_action_missing_cmd_returns_error(self):
        event = SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value={"cmd": "", "sid": "session-1"}),
        ))
        resp = server._on_card_action(event)
        self.assertEqual(resp.toast.type, "error")

    def test_card_action_missing_sid_returns_error(self):
        event = SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value={"cmd": "y", "sid": ""}),
        ))
        resp = server._on_card_action(event)
        self.assertEqual(resp.toast.type, "error")

    def test_card_action_expired_session_returns_error(self):
        resp = server._on_card_action(self._card_event(cmd="y", sid="nonexistent"))
        self.assertEqual(resp.toast.type, "error")
        self.assertIn("会话已过期", resp.toast.content)

    def test_card_action_stale_pid_returns_error(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("process_missing", None)):
            resp = server._on_card_action(self._card_event(cmd="y", sid="session-1"))
        self.assertEqual(resp.toast.type, "error")

    def test_card_action_invalid_tty_returns_error(self):
        self._setup_session()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value="TTY gone"):
            resp = server._on_card_action(self._card_event(cmd="y", sid="session-1"))
        self.assertEqual(resp.toast.type, "error")
        self.assertIn("终端不可用", resp.toast.content)


# =========================================================================
# 6. Multi-session routing
# =========================================================================

class MultiSessionTests(_Base):
    def test_two_sessions_route_replies_to_correct_terminals(self):
        self._post_hook(session_id="s1", tty="/dev/ttys001", cwd="/tmp/proj-a")
        self._post_hook(session_id="s2", tty="/dev/ttys002", cwd="/tmp/proj-b")
        self.replied.clear()

        injected_targets = []

        def fake_inject(tty, text):
            injected_targets.append((tty, text))
            return True

        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys001")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", side_effect=fake_inject):
            server._on_message(self._msg_event(
                root_id="root-1", parent_id="root-1",
                message_id="u1", text="for proj-a",
            ))

        self.assertEqual(injected_targets, [("/dev/ttys001", "for proj-a")])

        injected_targets.clear()
        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys002")), \
             mock.patch("agent_hotline.server.validate_tty", return_value=None), \
             mock.patch("agent_hotline.server.inject", side_effect=fake_inject):
            server._on_message(self._msg_event(
                root_id="root-2", parent_id="root-2",
                message_id="u2", text="for proj-b",
            ))

        self.assertEqual(injected_targets, [("/dev/ttys002", "for proj-b")])

    def test_reply_to_wrong_thread_does_not_cross_inject(self):
        self._post_hook(session_id="s1", tty="/dev/ttys001")
        self.replied.clear()

        server._on_message(self._msg_event(
            root_id="root-999", parent_id="root-999",
            message_id="u1", text="stray",
        ))
        self.assertIn("找不到对应会话", self.replied[0][1])


# =========================================================================
# 7. Session persistence across restart
# =========================================================================

class PersistenceTests(_Base):
    def test_session_survives_store_reload(self):
        self._post_hook(session_id="s1", tty="/dev/ttys001")

        # Simulate restart
        server.session_store = SessionStore(self.state_path)
        server.session_store.load()

        session = server.session_store.get("s1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "/dev/ttys001")
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


if __name__ == "__main__":
    unittest.main()
