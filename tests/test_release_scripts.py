"""End-to-end gate tests for release.sh / upgrade.sh (issue #23 K).

Spins up a throwaway git repo with a local bare 'origin' and fake
git-adjacent CLIs (gh/uv/walkcode/launchctl) on PATH, then drives the real
scripts to assert their gates:

* account check (must be 0x5446)
* prepare clean-slate gate: on main, HEAD==origin/main, no untracked files (A)
* publish HEAD==origin/main gate (B)
* re-entrant publish on an existing tag at HEAD (C)
* version checks
* upgrade requires the codex env file (F)

Everything uses the local bare remote, so nothing touches the network.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "release.sh"
UPGRADE_SH = REPO_ROOT / "upgrade.sh"

_GIT = shutil.which("git")
_BASH = shutil.which("bash")

FAKE_GH = """#!/usr/bin/env bash
cmd="${1:-}"; sub="${2:-}"
case "$cmd" in
  api)     echo "${FAKE_GH_ACCOUNT:-0x5446}" ;;
  pr)      case "$sub" in create) echo "https://fake/pr/1" ;; *) : ;; esac ;;
  release) case "$sub" in
             view)   exit "${FAKE_RELEASE_VIEW_RC:-1}" ;;
             create) echo "release created" ;;
             *) : ;;
           esac ;;
  *) : ;;
esac
"""

FAKE_UV = """#!/usr/bin/env bash
exit 0
"""

FAKE_WALKCODE = """#!/usr/bin/env bash
case "${1:-}" in
  --version) echo "walkcode 0.10.0" ;;
  upgrade) echo "upgraded" ;;
  install-hooks) echo "hooks installed" ;;
  *) : ;;
esac
"""

FAKE_LAUNCHCTL = """#!/usr/bin/env bash
case "${1:-}" in
  list) echo '{ "PID" = 4242; };' ;;
  *) : ;;
esac
"""


def _write_exe(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@unittest.skipUnless(_GIT and _BASH, "git and bash required")
class _ScriptGateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-reltest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.fakebin = self.tmp / "bin"
        self.fakebin.mkdir()
        _write_exe(self.fakebin / "gh", FAKE_GH)
        _write_exe(self.fakebin / "uv", FAKE_UV)
        _write_exe(self.fakebin / "walkcode", FAKE_WALKCODE)
        _write_exe(self.fakebin / "launchctl", FAKE_LAUNCHCTL)

        self.env = os.environ.copy()
        self.env["PATH"] = f"{self.fakebin}{os.pathsep}{self.env['PATH']}"
        self.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        self.env["TMPDIR"] = str(self.tmp)
        self.env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })

        self._git("init", "--bare", str(self.origin), cwd=self.tmp)
        self._git("-c", "init.defaultBranch=main", "init", str(self.work), cwd=self.tmp)
        self._git("checkout", "-B", "main", cwd=self.work)
        (self.work / "pyproject.toml").write_text(
            '[project]\nname = "walkcode"\nversion = "0.10.0"\n')
        (self.work / "tests").mkdir()
        (self.work / "tests" / "keep.txt").write_text("")
        shutil.copy(RELEASE_SH, self.work / "release.sh")
        shutil.copy(UPGRADE_SH, self.work / "upgrade.sh")
        (self.work / "release.sh").chmod(0o755)
        (self.work / "upgrade.sh").chmod(0o755)
        self._git("add", "-A", cwd=self.work)
        self._git("commit", "-m", "init", cwd=self.work)
        self._git("remote", "add", "origin", str(self.origin), cwd=self.work)
        self._git("push", "-u", "origin", "main", cwd=self.work)

    def _git(self, *args, cwd):
        r = subprocess.run([_GIT, *args], cwd=str(cwd), env=self.env,
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            self.fail(f"git {' '.join(args)} failed: {r.stderr}")
        return r

    def _git_out(self, *args):
        return subprocess.run([_GIT, *args], cwd=str(self.work), env=self.env,
                              capture_output=True, text=True, errors="replace").stdout

    def _run(self, script, *args, extra_env=None):
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([_BASH, f"./{script}", *args], cwd=str(self.work),
                              env=env, capture_output=True, text=True, errors="replace")

    def _set_version(self, v):
        (self.work / "pyproject.toml").write_text(
            f'[project]\nname = "walkcode"\nversion = "{v}"\n')


class ReleaseGateTests(_ScriptGateBase):
    def test_wrong_account_rejected(self):
        r = self._run("release.sh", "prepare", "0.10.1",
                      extra_env={"FAKE_GH_ACCOUNT": "someone-else"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("0x5446", r.stdout + r.stderr)

    def test_prepare_rejects_untracked(self):
        (self.work / "stray.txt").write_text("debris")
        r = self._run("release.sh", "prepare", "0.10.1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("stray.txt", r.stdout + r.stderr)

    def test_prepare_rejects_non_main(self):
        self._git("checkout", "-b", "feature", cwd=self.work)
        r = self._run("release.sh", "prepare", "0.10.1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("main", r.stdout + r.stderr)

    def test_prepare_happy_path(self):
        r = self._run("release.sh", "prepare", "0.10.1", "-m", "release v0.10.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('version = "0.10.1"', (self.work / "pyproject.toml").read_text())
        self.assertIn("release/v0.10.1", self._git_out("branch"))

    def test_publish_rejects_version_mismatch(self):
        r = self._run("release.sh", "publish", "0.10.1")  # pyproject still 0.10.0
        self.assertNotEqual(r.returncode, 0)

    def test_publish_rejects_head_ahead_of_origin(self):
        self._set_version("0.10.1")
        self._git("commit", "-am", "bump", cwd=self.work)  # local-only, not pushed
        r = self._run("release.sh", "publish", "0.10.1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("origin/main", r.stdout + r.stderr)

    def test_publish_happy_path(self):
        self._set_version("0.10.1")
        self._git("commit", "-am", "bump 0.10.1", cwd=self.work)
        self._git("push", "origin", "main", cwd=self.work)
        r = self._run("release.sh", "publish", "0.10.1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("v0.10.1", self._git_out("tag"))

    def test_publish_reentrant_existing_tag_at_head(self):
        self._set_version("0.10.1")
        self._git("commit", "-am", "bump 0.10.1", cwd=self.work)
        self._git("push", "origin", "main", cwd=self.work)
        self._git("tag", "-a", "v0.10.1", "-m", "v0.10.1", cwd=self.work)
        r = self._run("release.sh", "publish", "0.10.1")  # Release "missing" (rc=1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, r"resuming|续跑")


class UpgradeGateTests(_ScriptGateBase):
    def test_missing_codex_env_fails(self):
        r = self._run("upgrade.sh", extra_env={
            "WALKCODE_CODEX_ENV": str(self.tmp / "nope.env"),
            "WALKCODE_LAUNCHD_LABEL": "fake",
            "WALKCODE_LAUNCHD_LABEL_CODEX": "fake-codex",
            "LOG_CLAUDE": str(self.tmp / "c.log"),
            "LOG_CODEX": str(self.tmp / "x.log"),
        })
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("codex env", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main()
