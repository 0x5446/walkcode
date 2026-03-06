import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_hotline.state import SessionStore


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_reload_restores_session_and_root_mapping(self):
        store = SessionStore(self.state_path, ttl=86400)
        store.load()
        store.upsert(
            "session-1",
            tty="claude-project-123",
            cwd="/tmp/project",
            root_msg_id="root-1",
        )

        reloaded = SessionStore(self.state_path, ttl=86400)
        reloaded.load()

        self.assertEqual(reloaded.resolve(root_id="root-1"), "session-1")
        session = reloaded.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")
        self.assertEqual(session.cwd, "/tmp/project")
        self.assertEqual(session.root_msg_id, "root-1")

    def test_parent_id_fallback_uses_same_root_mapping(self):
        store = SessionStore(self.state_path, ttl=86400)
        store.load()
        store.upsert("session-1", tty="claude-project-123", cwd="/tmp/project", root_msg_id="root-1")

        self.assertEqual(store.resolve(parent_id="root-1"), "session-1")

    def test_expired_sessions_are_pruned_on_load(self):
        payload = {
            "sessions": {
                "session-1": {
                    "tty": "claude-project-123",
                    "cwd": "/tmp/project",
                    "root_msg_id": "root-1",
                    "created_at": time.time() - 120,
                }
            }
        }
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

        store = SessionStore(self.state_path, ttl=60)
        store.load()

        self.assertEqual(store.count(), 0)
        self.assertIsNone(store.resolve(root_id="root-1"))
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"sessions": {}})

    def test_touch_persists_updated_timestamp(self):
        store = SessionStore(self.state_path, ttl=86400)
        store.load()
        first = store.upsert("session-1", tty="claude-project-123", cwd="/tmp/project", root_msg_id="root-1")

        time.sleep(0.01)
        touched = store.touch("session-1")

        self.assertIsNotNone(touched)
        self.assertGreater(touched.created_at, first.created_at)

        reloaded = SessionStore(self.state_path, ttl=86400)
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

        store = SessionStore(self.state_path, ttl=86400)
        store.load()

        session = store.get("session-1")
        self.assertIsNotNone(session)
        self.assertEqual(session.tty, "claude-project-123")


if __name__ == "__main__":
    unittest.main()
