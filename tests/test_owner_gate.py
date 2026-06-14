"""Regression tests for the hook-ownership gate (tty.is_tmux_pane_owner).

Background: a nested child agent (e.g. a deep-review sub-agent in the parent's
background terminal) inherits the parent's $TMUX and would fire SessionStart/
Stop/permission hooks reporting the PARENT's tmux, hijacking its Feishu thread.
v0.10.31 gated hooks by comparing the hook's OWN controlling terminal to the
pane's pty. That broke when claude (>=2.1.x) began spawning hook processes
*detached*: the hook then has no controlling terminal (ctty ``??``), so every
legitimate main-session hook was misclassified as a nested child and dropped —
nothing reached Feishu even though the TUI showed the result.

The fix anchors ownership on tmux's ``pane_pid`` (the agent process itself),
reached by walking up from the hook past its shell to the firing agent. These
tests drive that walk with a fake process table so the scenarios are explicit.
"""

import os
import unittest
from unittest import mock

from walkcode import tty


def _fake_proc_info(table):
    """Return a stand-in for tty._proc_info backed by ``table``: pid -> (ppid,
    ctty, comm). Unknown pids return None (models an unreadable/gone process)."""
    def _impl(pid):
        return table.get(pid)
    return _impl


# pane the hooks claim: foreground agent pid 200 on /dev/ttys053
PANE_TTY = "/dev/ttys053"
PANE_PID = "200"


class CttyOwnsPaneTests(unittest.TestCase):
    """The pure predicate underneath the gate."""

    def test_matching_ctty(self):
        self.assertTrue(tty._ctty_owns_pane("ttys053", "/dev/ttys053"))

    def test_mismatched_ctty(self):
        self.assertFalse(tty._ctty_owns_pane("ttys099", "/dev/ttys053"))

    def test_no_controlling_terminal(self):
        self.assertFalse(tty._ctty_owns_pane("??", "/dev/ttys053"))
        self.assertFalse(tty._ctty_owns_pane("?", "/dev/ttys053"))

    def test_empty_inputs(self):
        self.assertFalse(tty._ctty_owns_pane("", "/dev/ttys053"))
        self.assertFalse(tty._ctty_owns_pane("ttys053", ""))

    def test_no_partial_suffix_match(self):
        # "ttys5" must not match "/dev/ttys053" via a bare endswith.
        self.assertFalse(tty._ctty_owns_pane("ttys5", "/dev/ttys053"))


class ProcInfoParseTests(unittest.TestCase):
    """_proc_info parses `ps -o ppid=,tty=,comm=` and normalises comm."""

    def _with_ps(self, returncode, stdout):
        fake = mock.Mock(returncode=returncode, stdout=stdout)
        return mock.patch.object(tty.subprocess, "run", return_value=fake)

    def test_full_path_comm_basenamed_and_lowercased(self):
        with self._with_ps(0, "2072 ttys053 /bin/ZSH\n"):
            self.assertEqual(tty._proc_info(900), (2072, "ttys053", "zsh"))

    def test_bare_comm_and_no_ctty(self):
        with self._with_ps(0, "10 ?? claude\n"):
            self.assertEqual(tty._proc_info(200), (10, "??", "claude"))

    def test_nonzero_returncode_is_none(self):
        with self._with_ps(1, ""):
            self.assertIsNone(tty._proc_info(123))

    def test_empty_output_is_none(self):
        with self._with_ps(0, "  \n"):
            self.assertIsNone(tty._proc_info(123))


class AncestorOwnsPaneTests(unittest.TestCase):
    """The walk-up that finds the firing agent and decides ownership."""

    def _run(self, table, start=1000):
        with mock.patch.object(tty, "_proc_info", _fake_proc_info(table)):
            return tty._ancestor_owns_pane(start, PANE_TTY, PANE_PID)

    def test_main_agent_detached_hook_is_owner(self):
        # claude 2.1.x: hook + its shell have no ctty; agent IS pane_pid.
        table = {
            1000: (900, "??", "python"),   # the walkcode hook (self)
            900:  (200, "??", "sh"),        # sh -c "... walkcode hook ..."
            200:  (10, "??", "claude"),     # the pane's agent == pane_pid
        }
        self.assertIs(self._run(table), True)

    def test_main_agent_with_ctty_is_owner(self):
        # agent differs from pane_pid (e.g. launched inside the pane's shell) but
        # holds the pane's controlling terminal — the ctty fallback covers it.
        table = {
            1000: (900, "??", "python"),
            900:  (201, "??", "sh"),
            201:  (200, "ttys053", "node"),  # agent, ctty == pane_tty
        }
        self.assertIs(self._run(table), True)

    def test_multiple_shells_are_transparent(self):
        # comm here is already normalised (basename, lower-cased) — that is what
        # _proc_info yields and what the walk matches against _SHELLS.
        table = {
            1000: (950, "??", "python"),
            950:  (940, "??", "zsh"),
            940:  (200, "??", "bash"),
            200:  (10, "??", "claude"),      # pane_pid
        }
        self.assertIs(self._run(table), True)

    def test_nested_subagent_without_ctty_is_foreign(self):
        # The hijack case: a sub-agent in the parent's tree, no ctty of its own.
        # It is a descendant of pane_pid, so it is never pane_pid itself.
        table = {
            1000: (900, "??", "python"),
            900:  (300, "??", "sh"),
            300:  (200, "??", "claude"),     # sub-agent (child of pane agent 200)
            200:  (10, "ttys053", "claude"),
        }
        self.assertIs(self._run(table), False)

    def test_nested_subagent_with_foreign_ctty_is_foreign(self):
        # Sub-agent running in its own background pty (different ctty).
        table = {
            1000: (900, "??", "python"),
            900:  (300, "??", "sh"),
            300:  (200, "ttys099", "claude"),  # foreign terminal
            200:  (10, "ttys053", "claude"),
        }
        self.assertIs(self._run(table), False)

    def test_self_unreadable_is_indeterminate(self):
        # Cannot read even our own process → fail open (None).
        self.assertIsNone(self._run({}, start=1000))

    def test_broken_ancestry_is_indeterminate(self):
        # Parent vanished mid-walk (reparented) → fail open.
        table = {1000: (900, "??", "python")}  # 900 missing
        self.assertIsNone(self._run(table))

    def test_reaching_init_without_agent_is_indeterminate(self):
        table = {
            1000: (900, "??", "python"),
            900:  (1, "??", "sh"),  # only shells up to init
        }
        self.assertIsNone(self._run(table))

    def test_ancestry_cycle_is_indeterminate(self):
        table = {
            1000: (900, "??", "python"),
            900:  (901, "??", "sh"),
            901:  (900, "??", "sh"),  # cycle
        }
        self.assertIsNone(self._run(table))


class IsTmuxPaneOwnerTests(unittest.TestCase):
    """End-to-end gate behaviour incl. env short-circuits and fail-open."""

    def _owner(self, table, env=None, pane=( PANE_TTY, PANE_PID)):
        base = {"TMUX": "/tmp/tmux-501/default,2072,127"}
        if env:
            base.update(env)
        with mock.patch.dict(os.environ, base, clear=True), \
             mock.patch.object(tty.os, "getpid", return_value=1000), \
             mock.patch.object(tty, "_pane_identity", return_value=pane), \
             mock.patch.object(tty, "_proc_info", _fake_proc_info(table)):
            return tty.is_tmux_pane_owner()

    def test_disabled_by_env(self):
        # WALKCODE_OWNER_CHECK=0 short-circuits before any probe.
        self.assertTrue(self._owner({}, env={"WALKCODE_OWNER_CHECK": "0"}))

    def test_not_under_tmux_is_owner(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(tty.is_tmux_pane_owner())

    def test_pane_probe_failure_fails_open(self):
        self.assertTrue(self._owner({}, pane=None))

    def test_main_detached_hook_delivered(self):
        table = {
            1000: (900, "??", "python"),
            900:  (200, "??", "sh"),
            200:  (10, "??", "claude"),
        }
        self.assertTrue(self._owner(table))

    def test_nested_subagent_dropped(self):
        table = {
            1000: (900, "??", "python"),
            900:  (300, "??", "sh"),
            300:  (200, "??", "claude"),
            200:  (10, "ttys053", "claude"),
        }
        self.assertFalse(self._owner(table))


if __name__ == "__main__":
    unittest.main()
