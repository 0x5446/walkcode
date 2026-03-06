import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_hotline import server
from agent_hotline.state import SessionStore


class ServerReplySessionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmpdir.name) / "state.json")
        self.store.load()
        server.session_store = self.store

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_reply_session_returns_session_when_tmux_alive(self):
        self.store.upsert(
            "session-1",
            tty="claude-project-123",
            cwd="/tmp/project",
            root_msg_id="root-1",
        )

        with mock.patch("agent_hotline.server.validate_target", return_value=None):
            session, error = server._load_reply_session("session-1")

        self.assertIsNone(error)
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")

    def test_load_reply_session_rejects_dead_tmux_session(self):
        self.store.upsert(
            "session-1",
            tty="claude-project-123",
            cwd="/tmp/project",
            root_msg_id="root-1",
        )

        with mock.patch("agent_hotline.server.validate_target", return_value="session not found"):
            session, error = server._load_reply_session("session-1")

        self.assertIsNone(session)
        self.assertEqual(error, server._STALE_SESSION_MESSAGE)

    def test_make_title_project_with_snippet(self):
        title = server._make_title("/tmp/myproject", "hello world")
        self.assertEqual(title, "myproject | hello...")

    def test_make_title_short_message_no_ellipsis(self):
        title = server._make_title("/tmp/myproject", "done")
        self.assertEqual(title, "myproject | done")

    def test_make_title_no_message(self):
        title = server._make_title("/tmp/myproject")
        self.assertEqual(title, "myproject")

    def test_mention_regex_strips_at_user(self):
        text = "@_user_1 continue working"
        result = server._MENTION_RE.sub("", text).strip()
        self.assertEqual(result, "continue working")

    def test_mention_regex_strips_multiple(self):
        text = "@_user_1 @_user_2 hello"
        result = server._MENTION_RE.sub("", text).strip()
        self.assertEqual(result, "hello")

    def test_receive_hook_sends_to_feishu(self):
        class FakeRequest:
            async def json(self):
                return {
                    "type": "stop",
                    "tty": "claude-project-123",
                    "cwd": "/tmp/plaudclaw",
                    "matcher": "",
                    "session_id": "9079ba57-f55e-4431-9486-62631f6f0979",
                    "message": "done",
                }

        with (
            mock.patch("agent_hotline.server._send", return_value="msg-1"),
            mock.patch("agent_hotline.server._reply"),
        ):
            response = asyncio.run(server.receive_hook(FakeRequest()))

        self.assertEqual(response, {"ok": True, "msg_id": "msg-1"})


if __name__ == "__main__":
    unittest.main()
