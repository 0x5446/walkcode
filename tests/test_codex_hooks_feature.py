"""Regression tests for codex config.toml feature-flag handling.

Background: codex 0.135+ gates hooks behind `[features] hooks = true`; older
builds used `codex_hooks`. The installer used to write `codex_hooks = true`,
which the running codex silently ignores — hooks never fire after a reinstall.

A second trap: codex also writes a `[hooks.state]` table to config.toml. A naive
`"hooks" in content` check would read that table as "already enabled" and skip
adding the feature flag. These tests pin that the installer writes the current
flag and is not fooled by `[hooks.state]`.
"""

import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
