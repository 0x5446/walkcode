"""Regression test for SessionStore tty eviction on upsert.

Background: a single tmux session is often reused by several claude
session_ids (e.g. claude /clear or restart in the same tmux). Before this
fix, every session_id kept pointing at the shared tmux name, so a Feishu
reply on an older thread would inject into whichever claude was currently
running in that tmux — silently routing messages to the wrong session.
upsert now clears the tty field of any other session_id that previously
held the same tmux, so stale threads naturally fall through to the
resume-on-dead-tty path.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from walkcode.state import SessionStore


class TtyEvictionTests(unittest.TestCase):

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self._tmp.name) / "state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_new_session_evicts_prior_holder_of_same_tty(self):
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        self.store.upsert("sid-new", tty="tmux-a", cwd="/x", root_msg_id="root-new")

        old = self.store.get("sid-old")
        new = self.store.get("sid-new")
        self.assertEqual(old.tty, "", "old session should lose its tty mapping")
        self.assertEqual(old.root_msg_id, "root-old", "thread mapping is preserved")
        self.assertEqual(new.tty, "tmux-a")

    def test_resolve_by_root_still_returns_evicted_session(self):
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        self.store.upsert("sid-new", tty="tmux-a", cwd="/x", root_msg_id="root-new")

        self.assertEqual(self.store.resolve(root_id="root-old"), "sid-old")
        evicted = self.store.get("sid-old")
        self.assertEqual(evicted.tty, "", "caller will see empty tty and trigger resume")

    def test_same_session_reupsert_does_not_self_evict(self):
        self.store.upsert("sid-a", tty="tmux-a", cwd="/x", root_msg_id="root-a")
        self.store.upsert("sid-a", tty="tmux-a", cwd="/x")
        self.assertEqual(self.store.get("sid-a").tty, "tmux-a")

    def test_empty_tty_upsert_does_not_evict(self):
        self.store.upsert("sid-a", tty="tmux-a", cwd="/x", root_msg_id="root-a")
        self.store.upsert("sid-b", tty="", cwd="/x", root_msg_id="root-b")
        self.assertEqual(self.store.get("sid-a").tty, "tmux-a")

    def test_eviction_persists_after_reload(self):
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        self.store.upsert("sid-new", tty="tmux-a", cwd="/x", root_msg_id="root-new")

        reloaded = SessionStore(self.store.path)
        reloaded.load()
        self.assertEqual(reloaded.get("sid-old").tty, "")
        self.assertEqual(reloaded.get("sid-new").tty, "tmux-a")


if __name__ == "__main__":
    unittest.main()
