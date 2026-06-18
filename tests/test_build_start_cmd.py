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

import os
import unittest

from walkcode.agent import CLAUDE, CODEX

CWD = "/Users/alpha/.walkcode/workspace"
IMG = "/Users/alpha/.walkcode/images/x.jpg"
_ROUTING_ENV = ("WALKCODE_EXTRA_ARGS", "WALKCODE_PERMISSION_FLAG")


class _CmdTestBase(unittest.TestCase):
    """Isolate every command-building test from ambient routing env vars.

    Without this, a shell that exports WALKCODE_PERMISSION_FLAG / WALKCODE_EXTRA_ARGS
    (e.g. a dev box or CI runner) would make the default-behavior assertions fail.
    All test classes that call build_start_cmd/build_resume_cmd inherit this.
    """

    def setUp(self):
        self._saved_routing = {k: os.environ.pop(k, None) for k in _ROUTING_ENV}

    def tearDown(self):
        for k in _ROUTING_ENV:
            os.environ.pop(k, None)
            if self._saved_routing.get(k) is not None:
                os.environ[k] = self._saved_routing[k]


class TestCodexBuildStartCmd(_CmdTestBase):
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
        self.assertTrue(cmd.startswith(f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex"))

    def test_codex_opts_out_of_owner_gate_via_inline_env(self):
        # codex (>=0.140) detaches its hooks, which walkcode's pane-ownership gate
        # would drop as a nested sub-agent. The launch command must set
        # WALKCODE_OWNER_CHECK=0 immediately before `codex` so codex and every hook
        # it spawns inherit it. Both start and resume paths must carry it.
        start = CODEX.build_start_cmd("hi", CWD, None)
        self.assertIn("&& WALKCODE_OWNER_CHECK=0 codex", start)
        resume = CODEX.build_resume_cmd("SID", CWD)
        self.assertIn("&& WALKCODE_OWNER_CHECK=0 codex", resume)


class TestClaudeBuildStartCmd(_CmdTestBase):
    def test_claude_keeps_owner_gate_no_inline_env(self):
        # claude runs hooks synchronously (intact ancestry), so it keeps the gate:
        # no WALKCODE_OWNER_CHECK override in its launch/resume commands.
        self.assertNotIn("WALKCODE_OWNER_CHECK", CLAUDE.build_start_cmd("hi", CWD, None))
        self.assertNotIn("WALKCODE_OWNER_CHECK", CLAUDE.build_resume_cmd("SID", CWD))

    def test_claude_has_no_image_flag_so_no_dashdash(self):
        # Claude delivers images via path injection, not a native flag
        # (image_flag is None), so even with an image_path there is no --image
        # and no `--` separator — the prompt is the only positional.
        cmd = CLAUDE.build_start_cmd("hi", CWD, IMG)
        self.assertNotIn("--image", cmd)
        self.assertNotIn(IMG, cmd)
        self.assertNotIn(" -- ", cmd)
        self.assertTrue(cmd.rstrip().endswith("'hi'"))


class TestPerInstanceRoutingOverrides(_CmdTestBase):
    """Per-instance env overrides for the Feishu→agent launch command.

    Each walkcode instance is single-agent, so routing is configured with two
    unprefixed env vars read at command-build time (loaded from the instance
    .env by Config.load):
      WALKCODE_EXTRA_ARGS      flags inserted right after the agent command,
                               e.g. `--settings .../vertex.json` to route Claude
                               through Vertex (the `ccv` route).
      WALKCODE_PERMISSION_FLAG replaces the default permission/approval flag,
                               e.g. `--yolo` for fully autonomous codex.

    Env isolation is provided by _CmdTestBase.setUp/tearDown.
    """

    # --- defaults unchanged when env is unset (backward compat) ---
    def test_claude_default_unchanged(self):
        cmd = CLAUDE.build_start_cmd("hi", CWD, None)
        self.assertEqual(cmd, f"cd '{CWD}' && claude --permission-mode default 'hi'")

    def test_codex_default_unchanged(self):
        cmd = CODEX.build_start_cmd("hi", CWD, None)
        self.assertEqual(
            cmd,
            f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex --ask-for-approval untrusted --no-alt-screen 'hi'",
        )

    # --- claude ccv: --settings inserted after `claude`, before permission flag ---
    def test_claude_extra_args_routes_to_vertex(self):
        os.environ["WALKCODE_EXTRA_ARGS"] = "--settings /p/vertex.json"
        cmd = CLAUDE.build_start_cmd("hi", CWD, None)
        self.assertEqual(
            cmd,
            f"cd '{CWD}' && claude --settings /p/vertex.json --permission-mode default 'hi'",
        )
        # order: settings must come before --permission-mode
        self.assertLess(cmd.index("--settings"), cmd.index("--permission-mode"))

    def test_claude_extra_args_applied_on_resume(self):
        os.environ["WALKCODE_EXTRA_ARGS"] = "--settings /p/vertex.json"
        cmd = CLAUDE.build_resume_cmd("SID", CWD)
        self.assertEqual(
            cmd,
            f"cd '{CWD}' && claude --settings /p/vertex.json --resume 'SID' --permission-mode default",
        )

    # --- codex yolo: permission flag replaces --ask-for-approval untrusted ---
    def test_codex_permission_flag_replaces_approval(self):
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--yolo"
        cmd = CODEX.build_start_cmd("hi", CWD, None)
        self.assertEqual(cmd, f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex --yolo --no-alt-screen 'hi'")
        self.assertNotIn("--ask-for-approval", cmd)

    def test_codex_yolo_keeps_image_dashdash_invariant(self):
        # The variadic --image guard must survive the permission override.
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--yolo"
        cmd = CODEX.build_start_cmd("", CWD, IMG)
        self.assertIn(f"--image '{IMG}'", cmd)
        self.assertNotIn("''", cmd)

    def test_codex_yolo_resume_puts_global_flag_before_subcommand(self):
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--yolo"
        cmd = CODEX.build_resume_cmd("SID", CWD)
        self.assertEqual(cmd, f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex --yolo resume 'SID'")
        # --yolo is a global flag, must precede `resume`
        self.assertLess(cmd.index("--yolo"), cmd.index("resume"))

    def test_codex_resume_default_has_no_permission_flag(self):
        # Without an override, codex resume stays bare (pre-existing behavior).
        cmd = CODEX.build_resume_cmd("SID", CWD)
        self.assertEqual(cmd, f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex resume 'SID'")

    def test_claude_permission_flag_override(self):
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--permission-mode plan"
        cmd = CLAUDE.build_start_cmd("hi", CWD, None)
        self.assertIn("--permission-mode plan", cmd)
        self.assertEqual(cmd.count("--permission-mode"), 1)

    def test_claude_permission_flag_override_on_resume(self):
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--permission-mode plan"
        cmd = CLAUDE.build_resume_cmd("SID", CWD)
        self.assertIn("--permission-mode plan", cmd)
        # the default flag must be fully replaced, not appended alongside
        self.assertNotIn("--permission-mode default", cmd)
        self.assertEqual(cmd.count("--permission-mode"), 1)

    # --- shell-injection neutralization (values come from a .env fragment) ---
    def test_extra_args_injection_is_neutralized(self):
        os.environ["WALKCODE_EXTRA_ARGS"] = "; touch /tmp/pwned"
        cmd = CLAUDE.build_start_cmd("hi", CWD, None)
        # the semicolon must be a quoted argument, never a bare command separator
        self.assertNotIn("&& claude ; touch", cmd)
        self.assertIn("';'", cmd)

    def test_permission_flag_injection_is_neutralized(self):
        os.environ["WALKCODE_PERMISSION_FLAG"] = "$(id)"
        cmd = CODEX.build_start_cmd("hi", CWD, None)
        # command substitution must be single-quoted, so the shell can't run it
        self.assertIn("'$(id)'", cmd)
        self.assertEqual(cmd.count("$(id)"), 1)  # only the quoted occurrence

    def test_valid_flags_are_quote_invariant(self):
        # normal config must pass through _safe_flags unchanged
        os.environ["WALKCODE_EXTRA_ARGS"] = "--settings /p/vertex.json"
        os.environ["WALKCODE_PERMISSION_FLAG"] = "--yolo"
        cmd = CODEX.build_start_cmd("hi", CWD, None)
        self.assertEqual(
            cmd,
            f"cd '{CWD}' && WALKCODE_OWNER_CHECK=0 codex --settings /p/vertex.json --yolo --no-alt-screen 'hi'",
        )


if __name__ == "__main__":
    unittest.main()
