"""Regression tests for the V3 `walkcode upgrade` command."""

import argparse
import unittest
import urllib.error
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
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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
                 patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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
             patch.object(m, "_get_latest_tag", lambda: "v9.9.9"), \
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


class LatestTagResolutionTests(unittest.TestCase):
    """The anonymous releases API is rate-limited per IP and returned 403 on
    2026-08-07; the old single-source lookup then silently installed from the
    default branch instead of the release."""

    def test_falls_back_to_release_redirect_when_gh_and_api_fail(self):
        with patch.object(m, "_latest_tag_via_gh", lambda: None), \
             patch.object(m, "_latest_tag_via_api", lambda: None), \
             patch.object(m, "_latest_tag_via_release_redirect", lambda: "v0.14.19"):
            self.assertEqual(m._get_latest_tag(), "v0.14.19")

    def test_gh_wins_over_the_other_sources(self):
        with patch.object(m, "_latest_tag_via_gh", lambda: "v1.2.3"), \
             patch.object(m, "_latest_tag_via_api", lambda: "v0.0.1"), \
             patch.object(m, "_latest_tag_via_release_redirect", lambda: "v0.0.2"):
            self.assertEqual(m._get_latest_tag(), "v1.2.3")

    def _redirect_opener(self, location):
        """Stand in for the 302 that /releases/latest answers with."""
        class _Opener:
            def open(self, url, timeout=None):
                raise urllib.error.HTTPError(
                    url, 302, "Found", {"Location": location}, None
                )

        return _Opener()

    def test_release_redirect_reads_the_tag_from_the_location_header(self):
        opener = self._redirect_opener("https://github.com/o/r/releases/tag/v0.14.19")
        with patch.object(m.urllib.request, "build_opener", lambda *a: opener):
            self.assertEqual(m._latest_tag_via_release_redirect(), "v0.14.19")

    def test_release_redirect_rejects_a_non_semver_target(self):
        # A moved/renamed page must not become an installable "tag".
        opener = self._redirect_opener("https://github.com/o/r/releases")
        with patch.object(m.urllib.request, "build_opener", lambda *a: opener):
            self.assertIsNone(m._latest_tag_via_release_redirect())

    def test_tag_sources_reject_shapes_that_are_not_semver(self):
        # The tag is interpolated into a shell `uv tool install` command.
        for raw in ("main", "v1.2", "v1.2.3; rm -rf /", "", None, "  v1.2.3  "):
            with self.subTest(raw=raw):
                got = m._validated_tag(raw)
                self.assertEqual(got, "v1.2.3" if raw == "  v1.2.3  " else None)

    def test_ls_remote_is_no_longer_a_tag_source(self):
        # release.sh pushes the tag BEFORE creating the Release, so a bare tag
        # list can name a version that was never released (AGENTS.md: upgrade
        # installs Releases).
        self.assertFalse(hasattr(m, "_latest_tag_via_ls_remote"))

    def test_upgrade_refuses_to_install_from_main_when_no_tag_resolves(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit):
                m.cmd_upgrade(argparse.Namespace())

        self.assertEqual(calls, [], "nothing may be installed without a resolved tag")

    def test_allow_main_override_still_installs_from_the_default_branch(self):
        calls = []
        with patch.object(m, "_run", lambda cmd, **kw: calls.append(cmd)), \
             patch.object(m, "_get_latest_tag", lambda: None), \
             patch.object(m, "_current_version", lambda: "0.0.0"), \
             patch.object(m, "_discover_v3_launchd_labels", lambda: []), \
             patch.dict("os.environ", {"WALKCODE_ALLOW_MAIN": "1"}, clear=True):
            m.cmd_upgrade(argparse.Namespace())

        install = next(c for c in calls if "uv tool install" in c)
        self.assertIn("walkcode @ git+", install)
        self.assertNotIn("@v", install.split("git+")[1])


if __name__ == "__main__":
    unittest.main()
