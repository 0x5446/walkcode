"""`walkcode upgrade` delegates to upgrade.sh instead of re-implementing it.

The Python copy drifted from the script (no claude-agent-sdk floor, no
--reinstall, no legacy-remnant gate, no lock, aborted on the first failed
kickstart). upgrade.sh's own behaviour is covered in test_release_scripts.py.
"""

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from walkcode import __main__ as m


class _Execd(Exception):
    """os.execv never returns; the fake raises to model that."""


def _fake_execv(calls):
    def execv(path, argv):
        calls.append((path, argv))
        raise _Execd

    return execv


class UpgradeDelegationTests(unittest.TestCase):
    def test_checkout_execs_upgrade_sh_with_bash(self):
        calls = []
        script = Path("/repo/upgrade.sh")
        with patch.object(m, "_checkout_upgrade_script", lambda: script), \
             patch.object(m.os, "execv", _fake_execv(calls)), \
             self.assertRaises(_Execd):
            m.cmd_upgrade(argparse.Namespace(dry_run=False))
        self.assertEqual(calls, [("/bin/bash", ["/bin/bash", str(script)])])

    def test_dry_run_is_passed_through(self):
        calls = []
        script = Path("/repo/upgrade.sh")
        with patch.object(m, "_checkout_upgrade_script", lambda: script), \
             patch.object(m.os, "execv", _fake_execv(calls)), \
             self.assertRaises(_Execd):
            m.cmd_upgrade(argparse.Namespace(dry_run=True))
        self.assertEqual(calls, [("/bin/bash", ["/bin/bash", str(script), "--dry-run"])])

    def test_without_checkout_prints_instructions_and_fails(self):
        err = io.StringIO()
        with patch.object(m, "_checkout_upgrade_script", lambda: None), \
             patch.object(m.os, "execv") as execv, \
             redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                m.cmd_upgrade(argparse.Namespace(dry_run=False))
        self.assertNotEqual(ctx.exception.code, 0)
        execv.assert_not_called()
        self.assertIn("upgrade.sh", err.getvalue())

    def test_checkout_detection_requires_upgrade_sh_next_to_pyproject(self):
        with tempfile.TemporaryDirectory() as root:
            pkg = Path(root) / "src" / "walkcode"
            pkg.mkdir(parents=True)
            fake_main = pkg / "__main__.py"
            fake_main.write_text("")
            with patch.object(m, "__file__", str(fake_main)):
                self.assertIsNone(m._checkout_upgrade_script())
                (Path(root) / "upgrade.sh").write_text("")
                self.assertIsNone(m._checkout_upgrade_script())
                (Path(root) / "pyproject.toml").write_text("")
                self.assertEqual(
                    m._checkout_upgrade_script(), (Path(root) / "upgrade.sh").resolve()
                )

    def test_this_checkout_resolves_to_the_repo_upgrade_sh(self):
        repo_script = Path(__file__).resolve().parent.parent / "upgrade.sh"
        self.assertEqual(m._checkout_upgrade_script(), repo_script)

    def test_no_python_reimplementation_is_left_behind(self):
        for name in (
            "_get_latest_tag",
            "_latest_tag_via_gh",
            "_latest_tag_via_api",
            "_latest_tag_via_release_redirect",
            "_discover_v3_launchd_labels",
            "_self_driver_label",
            "_schedule_deferred_self_restart",
            "_run",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(m, name))


if __name__ == "__main__":
    unittest.main()
