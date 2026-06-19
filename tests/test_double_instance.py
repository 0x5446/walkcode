"""Tests for _alert_double_instance — same session id, two live tmux panes.

When a session's tty drifts to a new tmux (e.g. a manual `claude --resume` of an
id already running under a Feishu-launched pane) while the OLD pane is still a live
agent, two processes may be writing the same rollout. We warn once per distinct
drift on the old thread; we do NOT auto-kill (killing the wrong pane loses work).
The alert is marked sent only after the notice actually goes out, and dedupe is
keyed by the exact (session, old_tty, new_tty) drift so a later distinct drift
still alerts.
"""

import threading
import time
import unittest
from unittest.mock import patch

from walkcode import server
from walkcode.i18n import t


class DoubleInstanceTests(unittest.TestCase):
    def setUp(self):
        with server._double_instance_lock:
            server._double_instance_alerted.clear()
        self.replies = []
        self.reply_ok = True
        p = patch.object(server, "_reply", self._reply)
        p.start()
        self.addCleanup(p.stop)

    def _reply(self, mid, text, reply_in_thread=False):
        self.replies.append((mid, text))
        return "reply-id" if self.reply_ok else None

    def _run(self, *, alive, session_id="sid-1", old_tty="walkcode-old",
             old_root="root-1", new_tty="claude-workspace-new"):
        with patch.object(server, "validate_target", lambda t: None if alive else "gone"), \
             patch.object(server, "is_agent_alive", lambda t: alive):
            server._alert_double_instance(session_id, old_tty, old_root, new_tty)

    def test_old_alive_drift_alerts_on_old_thread(self):
        self._run(alive=True)
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(self.replies[0][0], "root-1")
        self.assertEqual(
            self.replies[0][1],
            t("feishu.double_instance", old_tmux="walkcode-old", new_tmux="claude-workspace-new"),
        )
        self.assertIn(("sid-1", "walkcode-old", "claude-workspace-new"),
                      server._double_instance_alerted)

    def test_old_dead_does_not_alert(self):
        self._run(alive=False)
        self.assertEqual(self.replies, [])
        self.assertEqual(server._double_instance_alerted, set())

    def test_alerts_only_once_per_drift(self):
        self._run(alive=True)
        self._run(alive=True)
        self.assertEqual(len(self.replies), 1)

    def test_distinct_drift_pair_realerts(self):
        self._run(alive=True, old_tty="A", new_tty="B")
        self._run(alive=True, old_tty="B", new_tty="C")
        self.assertEqual(len(self.replies), 2)

    def test_no_root_no_reply_and_not_marked(self):
        self._run(alive=True, old_root=None)
        self.assertEqual(self.replies, [])
        # Not marked, so a future event with a root can still deliver the alert.
        self.assertEqual(server._double_instance_alerted, set())

    def test_concurrent_same_drift_alerts_once(self):
        # Two daemon threads for the same drift must not both deliver: the key is
        # reserved under the lock before the (slow) reply.
        def slow_reply(mid, text, reply_in_thread=False):
            time.sleep(0.05)
            self.replies.append((mid, text))
            return "reply-id"

        with patch.object(server, "_reply", slow_reply), \
             patch.object(server, "validate_target", lambda t: None), \
             patch.object(server, "is_agent_alive", lambda t: True):
            threads = [
                threading.Thread(
                    target=server._alert_double_instance,
                    args=("sid-1", "old", "root-1", "new"),
                )
                for _ in range(5)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(len(self.replies), 1)

    def test_failed_reply_is_not_marked_and_retries(self):
        self.reply_ok = False
        self._run(alive=True)
        self.assertEqual(len(self.replies), 1)
        self.assertEqual(server._double_instance_alerted, set())
        # A later identical event re-attempts because the first never delivered.
        self.reply_ok = True
        self._run(alive=True)
        self.assertEqual(len(self.replies), 2)
        self.assertIn(("sid-1", "walkcode-old", "claude-workspace-new"),
                      server._double_instance_alerted)


if __name__ == "__main__":
    unittest.main()
