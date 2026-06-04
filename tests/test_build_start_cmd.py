"""Regression tests for AgentAdapter.build_start_cmd — the Feishu→agent launch command.

Background (incident 2026-06-04): forwarding an image to the codex instance
spawned a tmux session that died within seconds, so every thread reply got
"⚠️ tmux session expired". Root cause was in build_start_cmd:

An image-only Feishu message carries an empty prompt (server sets text="" when
it extracts the image), so the launch command ended with a trailing empty
positional:

    codex --ask-for-approval untrusted --no-alt-screen --image '<img>' ''

codex's `-i, --image <FILE>...` is *variadic*. clap reads the trailing '' as a
second image value and aborts at startup:

    error: a value is required for '--image <FILE>...' but none was supplied

codex exits non-zero → the single-command tmux session is destroyed by tmux →
the next reply can only report a stale session. A *non-empty* prompt after the
variadic --image was just as wrong: it got swallowed as another image path
instead of being the prompt.

The fix: never append an empty positional, and separate a real prompt from the
variadic --image with `--`. These tests pin both.
"""

import unittest

from walkcode.agent import CLAUDE, CODEX

CWD = "/Users/alpha/.walkcode/workspace"
IMG = "/Users/alpha/.walkcode/images/x.jpg"


class TestCodexBuildStartCmd(unittest.TestCase):
    def test_image_only_no_trailing_empty_positional(self):
        # The exact incident: image, no prompt. Must NOT emit a trailing '' that
        # codex's variadic --image would choke on.
        cmd = CODEX.build_start_cmd("", CWD, IMG)
        self.assertIn(f"--image '{IMG}'", cmd)
        self.assertNotIn("''", cmd)  # no empty-string token anywhere
        self.assertFalse(cmd.rstrip().endswith("''"))
        # the precise byte sequence that crashed codex 0.136 must be gone
        self.assertNotIn(f"--image '{IMG}' ''", cmd)

    def test_image_with_prompt_separated_by_dashdash(self):
        cmd = CODEX.build_start_cmd("hello", CWD, IMG)
        self.assertIn(f"--image '{IMG}'", cmd)
        # prompt is a positional after `--`, so the variadic --image can't eat it
        self.assertIn(" -- 'hello'", cmd)
        self.assertLess(cmd.index("--image"), cmd.index(" -- 'hello'"))
        self.assertTrue(cmd.rstrip().endswith("'hello'"))

    def test_prompt_only_no_image_no_separator(self):
        cmd = CODEX.build_start_cmd("hello", CWD, None)
        self.assertNotIn("--image", cmd)
        self.assertNotIn(" -- ", cmd)  # no separator needed without a variadic opt
        self.assertTrue(cmd.rstrip().endswith("'hello'"))

    def test_empty_prompt_no_image_omits_positional(self):
        cmd = CODEX.build_start_cmd("", CWD, None)
        self.assertNotIn("''", cmd)
        self.assertFalse(cmd.rstrip().endswith("''"))

    def test_single_quote_in_prompt_still_escaped_with_image(self):
        cmd = CODEX.build_start_cmd("it's", CWD, IMG)
        self.assertIn(" -- ", cmd)
        # POSIX single-quote escaping: it's -> 'it'\''s'
        self.assertIn("'it'\\''s'", cmd)

    def test_includes_permission_and_extra_flags(self):
        cmd = CODEX.build_start_cmd("hello", CWD, None)
        self.assertIn("--ask-for-approval untrusted", cmd)
        self.assertIn("--no-alt-screen", cmd)
        self.assertTrue(cmd.startswith(f"cd '{CWD}' && codex"))


class TestClaudeBuildStartCmd(unittest.TestCase):
    def test_claude_has_no_image_flag_so_no_dashdash(self):
        # Claude delivers images via path injection, not a native flag
        # (image_flag is None), so even with an image_path there is no --image
        # and no `--` separator — the prompt is the only positional.
        cmd = CLAUDE.build_start_cmd("hi", CWD, IMG)
        self.assertNotIn("--image", cmd)
        self.assertNotIn(IMG, cmd)
        self.assertNotIn(" -- ", cmd)
        self.assertTrue(cmd.rstrip().endswith("'hi'"))


if __name__ == "__main__":
    unittest.main()
