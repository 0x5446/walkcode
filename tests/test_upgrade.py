"""Regression tests for the V3 `walkcode upgrade` command."""

import argparse
import unittest
from unittest.mock import patch

from walkcode import __main__ as m


class UpgradeV3Tests(unittest.TestCase):
    def test_upgrade_does_not_restore_legacy_hooks_or_daemon(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_discover_v3_launchd_labels", lambda: []), \
             patch.object(m, "cmd_install_hooks") as install_hooks, \
             patch.dict("os.environ", {}, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        self.assertTrue(any("uv tool install" in c for c in calls))
        self.assertTrue(any("walkcode @ git+" in c for c in calls), calls)
        self.assertFalse(any("walkcode[summary]" in c for c in calls), calls)
        self.assertTrue(any("--with claude-agent-sdk" in c for c in calls), calls)
        self.assertTrue(
            any("--python 3.13" in c for c in calls),
            f"expected upgrade to pin a compatible Python, got {calls}",
        )
        self.assertFalse(any("install-hooks" in c for c in calls), calls)
        self.assertFalse(any(" walkcode start" in c or "walkcode serve" in c for c in calls), calls)
        install_hooks.assert_not_called()
        self.assertTrue(any("walkcode native doctor" in c for c in calls), calls)

    def test_upgrade_restarts_only_explicit_v3_launchd_labels(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.dict("os.environ", {"WALKCODE_V3_LAUNCHD_LABELS": "com.walkcode.telegram-claude, com.walkcode.telegram-codex"}, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        self.assertTrue(any("launchctl kickstart -k" in c and "com.walkcode.telegram-claude" in c for c in calls), calls)
        self.assertTrue(any("launchctl kickstart -k" in c and "com.walkcode.telegram-codex" in c for c in calls), calls)

    def test_parse_launchd_labels_excludes_taps_and_foreign_services(self):
        listing = "\n".join(
            [
                "1\t0\tcom.walkcode.b-codex",
                "2\t0\tcom.walkcode.tap-b",
                "-\t0\tcom.walkcode.a-claude",
                "3\t0\tcom.apple.foo",
                "1\t0\tcom.walkcode.b-codex",  # duplicates collapse
                "not-a-launchctl-line",
                "",
            ]
        )
        self.assertEqual(
            m._parse_launchd_labels(listing),
            ["com.walkcode.a-claude", "com.walkcode.b-codex"],
        )

    def test_upgrade_discovers_labels_when_env_is_empty(self):
        # Empty WALKCODE_V3_LAUNCHD_LABELS used to skip the restart entirely,
        # leaving every instance on the old version after an upgrade.
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_discover_v3_launchd_labels", lambda: ["com.walkcode.a-claude"]), \
             patch.dict("os.environ", {"HOME": "/nonexistent-walkcode-test"}, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        self.assertTrue(
            any("launchctl kickstart -k" in c and "com.walkcode.a-claude" in c for c in calls),
            calls,
        )
        # no per-label env file exists → falls back to a bare doctor
        self.assertTrue(any("walkcode native doctor" in c for c in calls), calls)
        self.assertFalse(any("tap-" in c for c in calls), calls)


if __name__ == "__main__":
    unittest.main()
