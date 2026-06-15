"""Unit tests for tty.probe_agent_liveness — the three-state liveness mapping."""

import subprocess
import unittest
from unittest.mock import patch

from walkcode import tty


class _FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class ProbeLivenessTests(unittest.TestCase):
    def _run(self, *, ret=None, exc=None):
        def fake_run(*a, **k):
            if exc is not None:
                raise exc
            return ret
        return patch.object(tty.subprocess, "run", fake_run)

    def test_agent_command_is_alive(self):
        with self._run(ret=_FakeProc(0, stdout="node\n")):
            self.assertEqual(tty.probe_agent_liveness("s"), "alive")

    def test_shell_command_is_dead(self):
        with self._run(ret=_FakeProc(0, stdout="bash\n")):
            self.assertEqual(tty.probe_agent_liveness("s"), "dead")

    def test_missing_session_is_dead(self):
        with self._run(ret=_FakeProc(1, stderr="can't find session: s")):
            self.assertEqual(tty.probe_agent_liveness("s"), "dead")

    def test_no_server_is_dead(self):
        with self._run(ret=_FakeProc(1, stderr="no server running on /tmp/tmux-501/default")):
            self.assertEqual(tty.probe_agent_liveness("s"), "dead")

    def test_transient_nonzero_is_unknown(self):
        # permission / server hiccup with an unrecognized stderr → not death
        with self._run(ret=_FakeProc(1, stderr="permission denied")):
            self.assertEqual(tty.probe_agent_liveness("s"), "unknown")

    def test_timeout_is_unknown(self):
        with self._run(exc=subprocess.TimeoutExpired(cmd="tmux", timeout=2)):
            self.assertEqual(tty.probe_agent_liveness("s"), "unknown")

    def test_oserror_is_unknown(self):
        with self._run(exc=OSError("boom")):
            self.assertEqual(tty.probe_agent_liveness("s"), "unknown")

    def test_empty_output_is_unknown(self):
        with self._run(ret=_FakeProc(0, stdout="\n")):
            self.assertEqual(tty.probe_agent_liveness("s"), "unknown")


if __name__ == "__main__":
    unittest.main()
