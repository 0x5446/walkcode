"""Regression tests for tty.inject — the Feishu→terminal keystroke path.

Background: codex CLI >=0.136 added paste-burst detection. The old inject sent
the message text with `tmux send-keys -l` and then, microseconds later, a
separate `send-keys Enter`. codex coalesced the two into a single stdin read and
treated the trailing Enter as a newline INSIDE the paste rather than a submit —
so the message appeared in the input box but never sent (observed in Feishu: the
reply landed on the terminal and just sat there, unsubmitted).

The fix delivers a chat message via bracketed paste (`set-buffer` +
`paste-buffer -p`), which gives codex an unambiguous paste boundary, then a brief
delay, then Enter as its own keystroke → submit. A single-key reply (y/n/1-9) is
a MENU selection, not a message, and must stay a raw `send-keys -l` keystroke (a
permission menu reads "1" as "pick option 1", not text). These tests pin that.
"""

import unittest
from unittest.mock import MagicMock, patch

from walkcode import tty


def _ok(returncode=0, stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stderr = stderr
    return m


class InjectTests(unittest.TestCase):
    def setUp(self):
        p_valid = patch.object(tty, "validate_target", lambda s: None)
        p_sleep = patch.object(tty.time, "sleep", lambda s: None)
        p_valid.start(); p_sleep.start()
        self.addCleanup(p_valid.stop); self.addCleanup(p_sleep.stop)
        self.calls = []  # each entry is the argv list passed to subprocess.run

        def fake_run(argv, **kw):
            self.calls.append(argv)
            return _ok()

        p_run = patch.object(tty.subprocess, "run", fake_run)
        p_run.start(); self.addCleanup(p_run.stop)

    def _kinds(self):
        return [c[1] for c in self.calls]  # the tmux subcommand of each call

    # --- message path: bracketed paste, then a standalone Enter --------------

    def test_message_uses_bracketed_paste_then_enter(self):
        tty.inject("sess", "hello world")
        self.assertEqual(self._kinds(), ["set-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(self.calls[0][-1], "hello world")        # exact text buffered
        self.assertIn("-p", self.calls[1])                        # bracketed paste
        self.assertIn(tty._INJECT_BUFFER, self.calls[1])          # named buffer
        self.assertEqual(self.calls[2][-1], "Enter")              # standalone submit

    def test_message_text_never_sent_as_literal_keystroke(self):
        # The core regression: a message must NOT go through `send-keys -l` (that
        # is what let codex coalesce text+Enter into one paste burst).
        tty.inject("sess", "插件里干掉 openai codex")
        literal = [c for c in self.calls if c[1] == "send-keys" and "-l" in c]
        self.assertEqual(literal, [])

    def test_message_waits_before_enter(self):
        sleeps = []
        with patch.object(tty.time, "sleep", lambda s: sleeps.append(s)):
            tty.inject("sess", "hi there")
        self.assertEqual(sleeps, [tty._INJECT_ENTER_DELAY])

    def test_multiline_message_preserved_verbatim(self):
        msg = "第一段 请只回复收到\n第二段 不要执行任何操作"
        tty.inject("sess", msg)
        self.assertEqual(self.calls[0][1], "set-buffer")
        self.assertEqual(self.calls[0][-1], msg)

    def test_paste_buffer_failure_raises(self):
        def fake_run(argv, **kw):
            self.calls.append(argv)
            if argv[1] == "paste-buffer":
                return _ok(returncode=1, stderr="boom")
            return _ok()

        with patch.object(tty.subprocess, "run", fake_run):
            with self.assertRaises(RuntimeError):
                tty.inject("sess", "hello")

    # --- single-key (menu selection) path: raw keystroke, no paste -----------

    def test_single_key_is_raw_keystroke_not_paste(self):
        tty.inject("sess", "1", enter=True)
        self.assertEqual(self._kinds(), ["send-keys", "send-keys"])
        self.assertEqual(self.calls[0], ["tmux", "send-keys", "-t", "sess", "-l", "1"])
        self.assertEqual(self.calls[1][-1], "Enter")
        self.assertNotIn("paste-buffer", self._kinds())

    def test_single_key_no_enter_by_default(self):
        tty.inject("sess", "y")
        self.assertEqual(self._kinds(), ["send-keys"])  # no Enter appended
        self.assertEqual(self.calls[0][-1], "y")

    def test_single_key_does_not_sleep(self):
        sleeps = []
        with patch.object(tty.time, "sleep", lambda s: sleeps.append(s)):
            tty.inject("sess", "1", enter=True)
        self.assertEqual(sleeps, [])

    def test_invalid_target_raises(self):
        with patch.object(tty, "validate_target", lambda s: "no such session"):
            with self.assertRaises(RuntimeError):
                tty.inject("sess", "hello")


if __name__ == "__main__":
    unittest.main()
