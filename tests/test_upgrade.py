"""Regression tests for the V3 `walkcode upgrade` command."""

import argparse
import unittest
from pathlib import Path
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

    def test_upgrade_defers_self_driver_label(self):
        # ADR 0058: `walkcode upgrade` must never kickstart the runtime that
        # drives the session running it (review R1: the CLI entry point was
        # missing the guard upgrade.sh got).
        calls = []
        scheduled = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_schedule_deferred_self_restart", lambda label: scheduled.append(label)), \
             patch.dict("os.environ", {
                 "WALKCODE_V3_LAUNCHD_LABELS": "com.walkcode.a-claude,com.walkcode.b-codex",
                 "WALKCODE_DRIVER_LABEL": "com.walkcode.a-claude",
             }, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        self.assertFalse(
            any("kickstart" in c and "a-claude" in c for c in calls),
            f"self driver was kickstarted immediately: {calls}",
        )
        self.assertTrue(any("kickstart" in c and "b-codex" in c for c in calls), calls)
        self.assertEqual(scheduled, ["com.walkcode.a-claude"])

    def test_schedule_deferred_self_restart_validates_delay_and_uses_and(self):
        popens = []
        with patch.object(m.subprocess, "Popen", lambda *a, **kw: popens.append(a[0])), \
             patch.dict("os.environ", {"WALKCODE_SELF_RESTART_DELAY": "abc"}, clear=True):
            m._schedule_deferred_self_restart("com.walkcode.a-claude")

        argv = popens[0]
        # argv: [/bin/sh, -c, script, sh, delay, uid, label]
        self.assertEqual(argv[4], "120", argv)
        # `&&`, never `;`: a failed sleep must not fall through to kickstart.
        self.assertIn("&&", argv[2])
        self.assertNotIn(";", argv[2])

    def test_schedule_deferred_self_restart_rejects_fullwidth_digits(self):
        # str.isdigit() accepts full-width digits the system sleep rejects —
        # the detached restarter would die silently (review R2 shell#2).
        popens = []
        with patch.object(m.subprocess, "Popen", lambda *a, **kw: popens.append(a[0])), \
             patch.dict("os.environ", {"WALKCODE_SELF_RESTART_DELAY": "１２"}, clear=True):
            m._schedule_deferred_self_restart("com.walkcode.a-claude")
        self.assertEqual(popens[0][4], "120", popens[0])

    def test_upgrade_schedules_self_restart_after_all_output(self):
        # Ordering pin (review R2 tests#5): a zero/short delay must not kill
        # the driver before doctor and the completion message land.
        events = []
        with patch.object(m, "_run", lambda cmd, **kw: events.append(("run", cmd))), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_schedule_deferred_self_restart",
                          lambda label: events.append(("schedule", label))), \
             patch.dict("os.environ", {
                 "WALKCODE_V3_LAUNCHD_LABELS": "com.walkcode.a-claude,com.walkcode.b-codex",
                 "WALKCODE_DRIVER_LABEL": "com.walkcode.a-claude",
             }, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        kinds = [kind for kind, _ in events]
        self.assertIn("schedule", kinds, events)
        self.assertEqual(kinds[-1], "schedule", f"schedule must be the last action: {events}")

    def test_self_driver_label_process_tree_fallback(self):
        # No env marker (first upgrade from an old runtime): the ps climb
        # must find the `walkcode native serve` ancestor and map its PID to
        # a label via launchctl list (review R2 tests#4).
        listing = "77777\t0\tcom.walkcode.a-claude\n2\t0\tcom.walkcode.b-codex\n"

        def fake_run(argv, **kwargs):
            class R:
                stdout = ""
            r = R()
            if argv[0] == "launchctl":
                r.stdout = listing
            elif argv[0] == "ps":
                pid = argv[-1]
                if pid == "77777":
                    r.stdout = "    1 /usr/bin/python3 walkcode native serve"
                else:
                    r.stdout = "77777 python3 -m walkcode upgrade"
            return r

        with patch.object(m.subprocess, "run", fake_run), \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(m._self_driver_label(), "com.walkcode.a-claude")

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

    def test_explicit_tap_labels_are_never_kickstarted(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.dict(
                 "os.environ",
                 {
                     "HOME": "/nonexistent-walkcode-test",
                     "WALKCODE_V3_LAUNCHD_LABELS": "com.walkcode.tap-work,com.walkcode.a-claude",
                 },
                 clear=True,
             ):
            m.cmd_upgrade(argparse.Namespace())

        kicks = [c for c in calls if "launchctl kickstart" in c]
        self.assertTrue(any("com.walkcode.a-claude" in c for c in kicks), kicks)
        self.assertFalse(any("tap-work" in c for c in kicks), kicks)

    def test_doctor_binds_each_restarted_instance_env(self):
        import tempfile
        calls = []
        with tempfile.TemporaryDirectory() as home:
            walkdir = Path(home) / ".walkcode"
            walkdir.mkdir()
            (walkdir / "a-claude.env").write_text("WALKCODE_CHANNEL=lark\n")
            with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
                 patch.object(m, "_get_latest_tag", lambda: None), \
                 patch.object(m, "_current_version", lambda: "0.0.0"), \
                 patch.object(
                     m, "_discover_v3_launchd_labels",
                     lambda: ["com.walkcode.a-claude", "com.walkcode.b-codex"],
                 ), \
                 patch.dict("os.environ", {"HOME": home}, clear=True):
                m.cmd_upgrade(argparse.Namespace())

        doctors = [c for c in calls if "native doctor" in c]
        self.assertTrue(
            any("WALKCODE_ENV_FILE=" in c and "a-claude.env" in c for c in doctors), doctors
        )
        # b-codex has no env file → skipped, and no bare doctor is run since
        # at least one bound doctor already ran
        self.assertFalse(any(c.strip() == "walkcode native doctor" for c in doctors), doctors)

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
