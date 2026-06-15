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

    def test_can_evict_false_refuses_takeover(self):
        # A guard returning False protects the current owner: the incoming session
        # does NOT get the tty, and the owner keeps it. This is how a nested child
        # agent is stopped from displacing a live parent (see _can_evict_tty).
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        self.store.upsert(
            "sid-new", tty="tmux-a", cwd="/y", root_msg_id="root-new",
            can_evict=lambda oid, o: False,
        )
        self.assertEqual(self.store.get("sid-old").tty, "tmux-a", "owner keeps tty")
        self.assertEqual(self.store.get("sid-new").tty, "", "claimant gets no tty")

    def test_can_evict_true_allows_takeover(self):
        # A guard returning True keeps the legacy handoff behavior intact.
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        self.store.upsert(
            "sid-new", tty="tmux-a", cwd="/y", root_msg_id="root-new",
            can_evict=lambda oid, o: True,
        )
        self.assertEqual(self.store.get("sid-old").tty, "")
        self.assertEqual(self.store.get("sid-new").tty, "tmux-a")

    def test_can_evict_receives_the_current_owner(self):
        self.store.upsert("sid-old", tty="tmux-a", cwd="/x", root_msg_id="root-old")
        seen = {}

        def guard(oid, o):
            seen["id"], seen["root"] = oid, o.root_msg_id
            return True

        self.store.upsert("sid-new", tty="tmux-a", cwd="/y", can_evict=guard)
        self.assertEqual(seen["id"], "sid-old")
        self.assertEqual(seen["root"], "root-old")


class CwdDriftTests(unittest.TestCase):
    """Regression for resume cwd drift.

    A session's stored cwd is its *launch* dir — `--resume` cd's there to find
    the agent rollout file, which is keyed by launch cwd and never migrates when
    the user `cd`s mid-session. Only a trusted launch source (SessionStart sync /
    resume / Feishu pending) may establish it via cwd_is_launch=True. Runtime
    hooks (Stop/Notification/Permission) report the agent's *current* cwd and pass
    cwd_is_launch=False (the default); they must neither overwrite nor establish
    the launch cwd, or a mid-session `cd` (or a dropped SessionStart) would make
    resume cd into the wrong project dir and the agent would exit immediately.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.store = SessionStore(Path(self._tmp.name) / "state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_runtime_cwd_does_not_overwrite_launch_cwd(self):
        self.store.upsert("sid", tty="tmux-a", cwd="/launch", root_msg_id="root", cwd_is_launch=True)
        self.store.upsert("sid", tty="tmux-a", cwd="/launch/subdir")  # runtime hook
        self.assertEqual(self.store.get("sid").cwd, "/launch")

    def test_runtime_hook_first_create_does_not_lock_drifted_cwd(self):
        # SessionStart sync was dropped (best-effort upload). The first record is
        # created by a runtime hook AFTER an in-session cd — it must NOT lock the
        # subdir as launch cwd; it leaves cwd empty instead.
        self.store.upsert("sid", tty="tmux-a", cwd="/launch/sub", root_msg_id="root")  # runtime
        self.assertEqual(self.store.get("sid").cwd, "")
        # a later trusted sync still establishes the real launch cwd
        self.store.upsert("sid", tty="tmux-a", cwd="/launch", cwd_is_launch=True)
        self.assertEqual(self.store.get("sid").cwd, "/launch")

    def test_runtime_cwd_never_establishes_launch_cwd(self):
        # repeated runtime hooks never fill an empty launch cwd
        self.store.upsert("sid", tty="tmux-a", cwd="/a", root_msg_id="root")  # runtime
        self.store.upsert("sid", tty="tmux-a", cwd="/b")  # runtime
        self.assertEqual(self.store.get("sid").cwd, "")

    def test_established_launch_cwd_is_never_overwritten(self):
        # once set, neither a runtime hook nor a later (drifted) launch-source
        # write moves it — the field-is-empty guard makes establishment one-shot.
        self.store.upsert("sid", tty="tmux-a", cwd="/launch", cwd_is_launch=True)
        self.store.upsert("sid", tty="tmux-a", cwd="/launch/sub")  # runtime
        self.store.upsert("sid", tty="tmux-a", cwd="/elsewhere", cwd_is_launch=True)
        self.assertEqual(self.store.get("sid").cwd, "/launch")

    def test_resume_rebinds_tty_but_keeps_launch_cwd(self):
        # SessionStart records launch cwd; an in-session cd tries (and fails) to
        # drift it; resume then reads the *stored* cwd — exactly as _resume_agent
        # does (server.py: `cwd = old_session.cwd`) — and rebinds to a fresh tmux.
        # The stored cwd handed to resume must still be the launch dir; if the
        # drift guard regressed, get().cwd would be the subdir and the first
        # assert below would fail. Routing through get() (not a hardcoded
        # "/launch") is what makes this test actually catch the regression.
        self.store.upsert("sid", tty="old-tmux", cwd="/launch", root_msg_id="root", cwd_is_launch=True)
        self.store.upsert("sid", tty="old-tmux", cwd="/launch/sub")  # runtime drift attempt
        resume_cwd = self.store.get("sid").cwd  # what _resume_agent passes through
        self.assertEqual(resume_cwd, "/launch", "drifted cwd must not reach resume")
        self.store.upsert("sid", tty="new-tmux", cwd=resume_cwd, root_msg_id="root", cwd_is_launch=True)
        s = self.store.get("sid")
        self.assertEqual(s.tty, "new-tmux")
        self.assertEqual(s.cwd, "/launch")

    def test_cwd_protection_persists_after_reload(self):
        self.store.upsert("sid", tty="tmux-a", cwd="/launch", root_msg_id="root", cwd_is_launch=True)
        self.store.upsert("sid", tty="tmux-a", cwd="/launch/sub")  # runtime

        reloaded = SessionStore(self.store.path)
        reloaded.load()
        self.assertEqual(reloaded.get("sid").cwd, "/launch")

    def test_new_session_records_launch_cwd(self):
        self.store.upsert("sid", tty="tmux-a", cwd="/launch", cwd_is_launch=True)
        self.assertEqual(self.store.get("sid").cwd, "/launch")

    def test_tty_eviction_preserves_evicted_launch_cwd(self):
        # A tty takeover clears the old session's tty so its thread falls through
        # to resume-on-dead-tty; it must NOT disturb the old session's launch cwd.
        self.store.upsert("sid-old", tty="tmux-a", cwd="/old", root_msg_id="r-old", cwd_is_launch=True)
        self.store.upsert("sid-new", tty="tmux-a", cwd="/new", root_msg_id="r-new", cwd_is_launch=True)
        old = self.store.get("sid-old")
        self.assertEqual(old.tty, "")        # lost the tty
        self.assertEqual(old.cwd, "/old")    # launch cwd intact
        self.assertEqual(self.store.get("sid-new").cwd, "/new")

    def test_pending_launch_cwd_persists_after_reload(self):
        # Feishu-initiated start stashes the launch cwd in pending so a dropped
        # SessionStart can't lose it — and it must survive a server restart, so we
        # reload from disk before popping.
        self.store.add_pending("tmux-a", "root-1", cwd="/launch")
        reloaded = SessionStore(self.store.path)
        reloaded.load()
        root, _reply, cwd = reloaded.pop_pending("tmux-a")
        self.assertEqual(root, "root-1")
        self.assertEqual(cwd, "/launch")


if __name__ == "__main__":
    unittest.main()
