import json
import tempfile
import time
import unittest
from pathlib import Path

from walkcode.state import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reload_restores_session_and_root_mapping(self):
        store = SessionStore(self.state_path)
        store.load()
        store.upsert(
            "session-1",
            tty="claude-project-123",
            cwd="/tmp/project",
            root_msg_id="root-1",
        )

        reloaded = SessionStore(self.state_path)
        reloaded.load()

        self.assertEqual(reloaded.resolve(root_id="root-1"), "session-1")
        session = reloaded.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")
        self.assertEqual(session.cwd, "/tmp/project")
        self.assertEqual(session.root_msg_id, "root-1")

    def test_parent_id_fallback_uses_same_root_mapping(self):
        store = SessionStore(self.state_path)
        store.load()
        store.upsert("session-1", tty="claude-project-123", cwd="/tmp/project", root_msg_id="root-1")

        self.assertEqual(store.resolve(parent_id="root-1"), "session-1")

    def test_sessions_are_never_expired(self):
        """Sessions should persist indefinitely (no TTL)."""
        payload = {
            "sessions": {
                "session-1": {
                    "tty": "claude-project-123",
                    "cwd": "/tmp/project",
                    "root_msg_id": "root-1",
                    "created_at": time.time() - 86400 * 30,  # 30 days old
                }
            }
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

        store = SessionStore(self.state_path)
        store.load()

        self.assertEqual(store.count(), 1)
        self.assertEqual(store.resolve(root_id="root-1"), "session-1")

    def test_touch_persists_updated_timestamp(self):
        store = SessionStore(self.state_path)
        store.load()
        first = store.upsert("session-1", tty="claude-project-123", cwd="/tmp/project", root_msg_id="root-1")

        time.sleep(0.01)
        touched = store.touch("session-1")

        self.assertIsNotNone(touched)
        self.assertGreater(touched.created_at, first.created_at)

        reloaded = SessionStore(self.state_path)
        reloaded.load()
        session = reloaded.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.created_at, touched.created_at)

    def test_loads_legacy_state_with_extra_fields(self):
        """Old state.json with tty_pid fields should still load fine."""
        payload = {
            "sessions": {
                "session-1": {
                    "tty": "claude-project-123",
                    "cwd": "/tmp/project",
                    "root_msg_id": "root-1",
                    "tty_pid": 1234,
                    "tty_pid_started_at": "2026-03-06 13:00:10",
                    "created_at": time.time(),
                }
            }
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

        store = SessionStore(self.state_path)
        store.load()

        session = store.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")

    def test_items_returns_all_sessions(self):
        store = SessionStore(self.state_path)
        store.load()
        store.upsert("s1", tty="tty-1", cwd="/a")
        store.upsert("s2", tty="tty-2", cwd="/b")

        items = store.items()
        self.assertEqual(len(items), 2)
        ids = {sid for sid, _ in items}
        self.assertEqual(ids, {"s1", "s2"})


if __name__ == "__main__":
    unittest.main()
