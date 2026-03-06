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

    def test_load_reply_session_refreshes_tty_from_live_pid(self):
        self.store.upsert(
            "session-1",
            tty="/dev/ttys001",
            cwd="/tmp/project",
            root_msg_id="root-1",
            tty_pid=1234,
            tty_pid_started_at="Fri Mar  6 13:00:10 2026",
        )

        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("ok", "/dev/ttys009")):
            session, error = server._load_reply_session("session-1")

        self.assertIsNone(error)
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "/dev/ttys009")
        self.assertEqual(self.store.get("session-1").tty, "/dev/ttys009")

    def test_load_reply_session_rejects_stale_binding(self):
        self.store.upsert(
            "session-1",
            tty="/dev/ttys001",
            cwd="/tmp/project",
            root_msg_id="root-1",
            tty_pid=1234,
            tty_pid_started_at="Fri Mar  6 13:00:10 2026",
        )

        with mock.patch("agent_hotline.server.inspect_tty_owner", return_value=("process_missing", None)):
            session, error = server._load_reply_session("session-1")

        self.assertIsNone(session)
        self.assertEqual(error, server._STALE_SESSION_MESSAGE)

    def test_make_message_includes_tty_and_session_hint(self):
        text = server._make_message(
            "stop",
            "",
            "/tmp/plaudclaw",
            "done",
            tty="/dev/ttys001",
            session_id="9079ba57-f55e-4431-9486-62631f6f0979",
        )

        self.assertIn("[plaudclaw ttys001 9079ba57] ✅ 任务完成", text)
        self.assertIn("> done", text)

    def test_terminal_label_matches_notification_hint(self):
        label = server._terminal_label(
            "/tmp/plaudclaw",
            "/dev/ttys001",
            "9079ba57-f55e-4431-9486-62631f6f0979",
        )

        self.assertEqual(label, "plaudclaw ttys001 9079ba57")

    def test_receive_hook_tags_terminal_before_sending(self):
        class FakeRequest:
            async def json(self):
                return {
                    "type": "stop",
                    "tty": "/dev/ttys001",
                    "cwd": "/tmp/plaudclaw",
                    "matcher": "",
                    "session_id": "9079ba57-f55e-4431-9486-62631f6f0979",
                    "message": "done",
                }

        with (
            mock.patch("agent_hotline.server._tag_terminal") as tag_terminal,
            mock.patch("agent_hotline.server._send", return_value="msg-1"),
            mock.patch("agent_hotline.server._reply", return_value="reply-1"),
        ):
            response = asyncio.run(server.receive_hook(FakeRequest()))

        tag_terminal.assert_called_once_with(
            "/dev/ttys001",
            "/tmp/plaudclaw",
            "9079ba57-f55e-4431-9486-62631f6f0979",
        )
        self.assertEqual(response, {"ok": True, "msg_id": "msg-1"})


if __name__ == "__main__":
    unittest.main()
