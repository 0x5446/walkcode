import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from walkcode import server
from walkcode.state import SessionStore


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

        with mock.patch("walkcode.server.validate_target", return_value=None):
            session, error = server._load_reply_session("session-1")

        self.assertIsNone(error)
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")

    def test_load_reply_session_returns_session_data_when_tmux_dead(self):
        """When tmux is dead, session data should still be returned for resume."""
        self.store.upsert(
            "session-1",
            tty="claude-project-123",
            cwd="/tmp/project",
            root_msg_id="root-1",
        )

        with mock.patch("walkcode.server.validate_target", return_value="session not found"):
            session, error = server._load_reply_session("session-1")

        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")
        self.assertEqual(session.cwd, "/tmp/project")
        self.assertIsNotNone(error)
        self.assertIn("not found", error)

    def test_make_title_project_with_snippet(self):
        title = server._make_title("/tmp/myproject", message="hello world")
        self.assertEqual(title, "myproject | hello worl...")

    def test_make_title_with_session_id(self):
        title = server._make_title("/tmp/myproject", session_id="abcdef1234567890", message="fix bug")
        self.assertEqual(title, "myproject | abcdef12 | fix bug")

    def test_make_title_short_message_no_ellipsis(self):
        title = server._make_title("/tmp/myproject", message="done")
        self.assertEqual(title, "myproject | done")

    def test_make_title_no_message(self):
        title = server._make_title("/tmp/myproject")
        self.assertEqual(title, "myproject")

    def test_make_title_session_id_only(self):
        title = server._make_title("/tmp/myproject", session_id="abcdef1234567890")
        self.assertEqual(title, "myproject | abcdef12")

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
            mock.patch("walkcode.server._send", return_value="msg-1"),
            mock.patch("walkcode.server._reply"),
        ):
            response = asyncio.run(server.receive_hook(FakeRequest()))

        self.assertEqual(response, {"ok": True, "msg_id": "msg-1"})


if __name__ == "__main__":
    unittest.main()
