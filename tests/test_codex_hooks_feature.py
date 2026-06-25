"""Regression tests for codex config.toml feature-flag handling.

Background: codex 0.135+ gates hooks behind `[features] hooks = true`; older
builds used `codex_hooks`. The installer used to write `codex_hooks = true`,
which the running codex silently ignores — hooks never fire after a reinstall.

A second trap: codex also writes a `[hooks.state]` table to config.toml. A naive
`"hooks" in content` check would read that table as "already enabled" and skip
adding the feature flag. These tests pin that the installer writes the current
flag and is not fooled by `[hooks.state]`.
"""

import json
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from walkcode import __main__ as m


class EnsureCodexHooksFeatureTests(unittest.TestCase):
    def _run(self, initial=None):
        with TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            if initial is not None:
                p.write_text(initial)
            m._ensure_codex_hooks_feature(p)
            return p.read_text()

    def test_creates_file_with_current_flag(self):
        out = self._run(None)
        self.assertIn("[features]", out)
        self.assertIn("hooks = true", out)
        self.assertNotIn("codex_hooks", out)

    def test_adds_flag_under_existing_features(self):
        out = self._run("[features]\njs_repl = false\n")
        self.assertRegex(out, r"(?m)^hooks = true$")
        self.assertIn("js_repl = false", out)

    def test_appends_features_section_when_absent(self):
        out = self._run('model = "gpt-5.5"\n')
        self.assertIn('model = "gpt-5.5"', out)
        self.assertIn("[features]", out)
        self.assertRegex(out, r"(?m)^hooks = true$")

    def test_idempotent_when_already_enabled(self):
        initial = "[features]\nhooks = true\n"
        out = self._run(initial)
        self.assertEqual(out, initial)
        self.assertEqual(out.count("hooks = true"), 1)

    def test_hooks_state_table_is_not_mistaken_for_the_flag(self):
        # codex writes [hooks.state] entries; they must NOT suppress the flag.
        initial = (
            '[features]\njs_repl = false\n\n'
            '[hooks.state]\n\n'
            '[hooks.state."/Users/x/.codex/hooks.json:stop:0:0"]\n'
            'trusted_hash = "sha256:abc"\n'
        )
        out = self._run(initial)
        self.assertRegex(out, r"(?m)^hooks = true$")
        # original [hooks.state] content preserved
        self.assertIn("trusted_hash", out)

    def test_legacy_codex_hooks_does_not_suppress_current_flag(self):
        # Old installer wrote codex_hooks; reinstall must still add hooks = true.
        out = self._run("[features]\ncodex_hooks = true\n")
        self.assertIs(tomllib.loads(out)["features"]["hooks"], True)
        self.assertIn("codex_hooks = true", out)  # legacy flag left untouched

    def test_hooks_false_is_replaced_not_duplicated(self):
        # Must replace in place — a duplicate key would make tomllib.loads raise.
        out = self._run("[features]\nhooks = false\n")
        data = tomllib.loads(out)
        self.assertIs(data["features"]["hooks"], True)

    def test_hooks_true_with_trailing_comment_is_noop(self):
        # tomllib sees features.hooks = True despite the comment → leave untouched.
        initial = "[features]\nhooks = true # keep me\n"
        self.assertEqual(self._run(initial), initial)

    # --- C: header matching must tolerate whitespace / trailing comments ----
    # A literal "[features]" match misses these legal TOML spellings; the old
    # code would then write a duplicate [features] table or a duplicate key,
    # leaving codex with invalid config or hooks still disabled.

    def test_spaced_header_with_hooks_false_is_repaired(self):
        out = self._run("[ features ]\nhooks = false\n")
        self.assertIs(tomllib.loads(out)["features"]["hooks"], True)
        self.assertEqual(out.count("hooks = true"), 1)
        self.assertEqual(out.count("[ features ]") + out.count("[features]"), 1)

    def test_commented_header_with_hooks_false_is_repaired(self):
        out = self._run("[features] # codex flags\nhooks = false\n")
        self.assertIs(tomllib.loads(out)["features"]["hooks"], True)
        self.assertEqual(out.count("hooks = true"), 1)
        # the header comment is preserved, no duplicate table
        self.assertIn("# codex flags", out)

    def test_spaced_header_without_hooks_gets_flag(self):
        out = self._run("[ features ]\njs_repl = false\n")
        self.assertIs(tomllib.loads(out)["features"]["hooks"], True)
        self.assertIn("js_repl = false", out)
        self.assertEqual(out.count("[ features ]") + out.count("[features]"), 1)

    def test_commented_header_without_hooks_gets_flag(self):
        out = self._run("[features] # codex flags\n")
        self.assertIs(tomllib.loads(out)["features"]["hooks"], True)
        self.assertIn("# codex flags", out)

    def test_refuses_to_corrupt_a_previously_valid_config(self):
        # The tomllib re-validation guard: if the edit would somehow produce
        # invalid TOML for a config that originally parsed cleanly, leave the
        # file untouched rather than break codex's config.
        import io as _io

        initial = "[features]\nhooks = false\n"
        with TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(initial)
            with patch.object(m, "_set_features_hooks_true", lambda c: "x = = broken"), \
                 patch.object(m.sys, "stderr", _io.StringIO()) as err:
                m._ensure_codex_hooks_feature(p)
            self.assertEqual(p.read_text(), initial)  # unchanged
            self.assertIn("skipped enabling codex hooks", err.getvalue())


class InstallClaudeHooksTests(unittest.TestCase):
    def test_installs_subagent_progress_hooks(self):
        with TemporaryDirectory() as d:
            home = Path(d)
            settings_path = home / ".claude" / "settings.json"
            settings_path.parent.mkdir()
            settings_path.write_text("{}")

            with patch.object(m.Path, "home", return_value=home):
                m._install_claude_hooks(None)

            hooks = json.loads(settings_path.read_text())["hooks"]
            self.assertEqual(
                hooks["SubagentStart"][0]["hooks"][0]["command"],
                "walkcode hook subagent-start",
            )
            self.assertEqual(
                hooks["SubagentStop"][0]["hooks"][0]["command"],
                "walkcode hook subagent-stop",
            )
            self.assertEqual(
                hooks["TaskCreated"][0]["hooks"][0]["command"],
                "walkcode hook task-created",
            )
            self.assertEqual(
                hooks["TaskCompleted"][0]["hooks"][0]["command"],
                "walkcode hook task-completed",
            )


class InstallCodexHooksTests(unittest.TestCase):
    def test_installs_subagent_progress_hooks(self):
        with TemporaryDirectory() as d:
            home = Path(d)

            with patch.object(m.Path, "home", return_value=home), \
                 patch.dict("os.environ", {}, clear=True):
                m._install_codex_hooks(None)

            hooks = json.loads((home / ".codex" / "hooks.json").read_text())["hooks"]
            self.assertEqual(
                hooks["SubagentStart"][0]["hooks"][0]["command"],
                "WALKCODE_AGENT=codex WALKCODE_PORT=3001 walkcode hook subagent-start",
            )
            self.assertEqual(
                hooks["SubagentStop"][0]["hooks"][0]["command"],
                "WALKCODE_AGENT=codex WALKCODE_PORT=3001 walkcode hook subagent-stop",
            )


if __name__ == "__main__":
    unittest.main()
