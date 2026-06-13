"""Regression tests for tty.inject — the Feishu→terminal keystroke path.

Background: codex CLI >=0.136 added paste-burst detection. The old inject sent
the message text with `tmux send-keys -l` and then, microseconds later, a
separate `send-keys Enter`. codex coalesced the two into a single stdin read and
treated the trailing Enter as a newline INSIDE the paste rather than a submit —
so the message appeared in the input box but never sent (observed in Feishu: the
reply landed on the terminal and just sat there, unsubmitted).

The fix delivers a chat message via bracketed paste (`set-buffer` +
`paste-buffer -p`), which gives codex an unambiguous paste boundary, then a brief
delay, then Enter as its own keystroke → submit.

Intent is now declared by the caller via `menu_key`, not guessed from content:
a chat message is ALWAYS pasted and submitted, even a single char like "2" (the
old content sniffing treated "2" as a menu key and left it unsubmitted in the
box). `menu_key=True` is the raw `send-keys -l` keystroke path used only by the
permission hook-timeout fallback (a menu reads "1" as "pick option 1", not text).
These tests pin that.
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
        # set-buffer and paste-buffer must target the SAME per-call buffer, whose
        # name is derived from _INJECT_BUFFER (now unique per call, not fixed).
        set_buf = self.calls[0][self.calls[0].index("-b") + 1]
        paste_buf = self.calls[1][self.calls[1].index("-b") + 1]
        self.assertTrue(set_buf.startswith(tty._INJECT_BUFFER))
        self.assertEqual(set_buf, paste_buf)
        self.assertEqual(self.calls[2][-1], "Enter")              # standalone submit

    def test_concurrent_injects_use_distinct_buffers(self):
        # Two injects must not share a global buffer name — a fixed name lets the
        # claude/codex bot processes (or worker threads) race on the tmux server
        # buffer and paste one session's text into another. See _INJECT_BUFFER.
        tty.inject("sessA", "first")
        tty.inject("sessB", "second")
        bufs = [c[c.index("-b") + 1] for c in self.calls if c[1] == "set-buffer"]
        self.assertEqual(len(bufs), 2)
        self.assertNotEqual(bufs[0], bufs[1])

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

    # --- regression: a single-character CHAT message must be submitted -------

    def test_single_char_message_is_pasted_and_submitted(self):
        # The reported bug: a Feishu reply of "2" was sniffed as a menu key, so
        # it was typed into the box but never submitted. A chat message — even a
        # single digit — must go through bracketed paste AND get an Enter.
        tty.inject("sess", "2")
        self.assertEqual(self._kinds(), ["set-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(self.calls[0][-1], "2")          # exact text buffered
        self.assertIn("-p", self.calls[1])                # bracketed paste
        self.assertEqual(self.calls[2][-1], "Enter")      # standalone submit

    def test_single_char_letter_message_is_submitted(self):
        # "y"/"n"/"a" were also in the old SINGLE_KEYS set — same swallow bug.
        tty.inject("sess", "y")
        self.assertEqual(self._kinds(), ["set-buffer", "paste-buffer", "send-keys"])
        self.assertEqual(self.calls[2][-1], "Enter")

    # --- menu-key path (explicit): raw keystroke, no paste -------------------

    def test_menu_key_is_raw_keystroke_not_paste(self):
        tty.inject("sess", "1", enter=True, menu_key=True)
        self.assertEqual(self._kinds(), ["send-keys", "send-keys"])
        self.assertEqual(self.calls[0], ["tmux", "send-keys", "-t", "sess", "-l", "1"])
        self.assertEqual(self.calls[1][-1], "Enter")
        self.assertNotIn("paste-buffer", self._kinds())

    def test_menu_key_no_enter_by_default(self):
        tty.inject("sess", "y", menu_key=True)
        self.assertEqual(self._kinds(), ["send-keys"])  # no Enter appended
        self.assertEqual(self.calls[0][-1], "y")

    def test_menu_key_does_not_sleep(self):
        sleeps = []
        with patch.object(tty.time, "sleep", lambda s: sleeps.append(s)):
            tty.inject("sess", "1", enter=True, menu_key=True)
        self.assertEqual(sleeps, [])

    def test_invalid_target_raises(self):
        with patch.object(tty, "validate_target", lambda s: "no such session"):
            with self.assertRaises(RuntimeError):
                tty.inject("sess", "hello")


if __name__ == "__main__":
    unittest.main()
