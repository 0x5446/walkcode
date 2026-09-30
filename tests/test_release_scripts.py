"""End-to-end gate tests for release.sh / upgrade.sh (issue #23 K).

Spins up a throwaway git repo with a local bare 'origin' and fake
git-adjacent CLIs (gh/uv/walkcode/launchctl) on PATH, then drives the real
scripts to assert their gates:

* account check (must be 0x5446)
* prepare clean-slate gate: on main, HEAD==origin/main, no untracked files (A)
* publish HEAD==origin/main gate (B)
* re-entrant publish: pushes a local-only tag to origin, non-empty notes (C)
* publish aborts on a non-404 `gh release view` error (C)
* version checks
* upgrade is V3-only and does not require legacy codex env/hooks
* upgrade lock: active lock blocks, stale lock is reclaimed (H)

Everything uses the local bare remote, so nothing touches the network.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "release.sh"
INSTALL_SH = REPO_ROOT / "install.sh"
UPGRADE_SH = REPO_ROOT / "upgrade.sh"
UNINSTALL_SH = REPO_ROOT / "uninstall.sh"

_GIT = shutil.which("git")
# Prefer the system bash: on macOS that is 3.2, the interpreter these scripts
# actually run under (launchd, bare terminals) — newer homebrew bash would
# hide 3.2-only parsing bugs like unbraced vars before CJK text.
_BASH = "/bin/bash" if os.path.exists("/bin/bash") else shutil.which("bash")

FAKE_GH = """#!/usr/bin/env bash
cmd="${1:-}"; sub="${2:-}"
case "$cmd" in
  api)
    case "$sub" in
      */releases/latest)
        # FAKE_GH_LATEST_TAG=NONE emulates an unauthenticated / erroring gh.
        [ "${FAKE_GH_LATEST_TAG:-v9.9.9}" = "NONE" ] && exit 1
        echo "${FAKE_GH_LATEST_TAG:-v9.9.9}" ;;
      *) echo "${FAKE_GH_ACCOUNT:-0x5446}" ;;
    esac ;;
  pr)      case "$sub" in create) echo "https://fake/pr/1" ;; *) : ;; esac ;;
  release) case "$sub" in
             view)
               rc="${FAKE_RELEASE_VIEW_RC:-1}"
               [ "$rc" != "0" ] && echo "${FAKE_RELEASE_VIEW_MSG:-release not found}" >&2
               exit "$rc" ;;
             create)
               shift 2
               while [ $# -gt 0 ]; do
                 [ "$1" = "--notes" ] && printf '%s' "${2:-}" > "${FAKE_GH_NOTES_FILE:-/dev/null}"
                 shift
               done
               echo "release created" ;;
             *) : ;;
           esac ;;
  *) : ;;
esac
"""

FAKE_UV = """#!/usr/bin/env bash
if [ -n "${FAKE_UV_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_UV_LOG"
fi
exit 0
"""

FAKE_WALKCODE = """#!/usr/bin/env bash
case "${1:-}" in
  --version) echo "walkcode 0.10.0" ;;
  upgrade) echo "upgraded" ;;
  install-hooks) echo "hooks installed" ;;
  native) echo "native doctor ok" ;;
  *) : ;;
esac
"""

# upgrade.sh's last-resort tag source reads the Location header of
# github.com/<repo>/releases/latest via `curl -w '%{redirect_url}'`.
# FAKE_CURL_REDIRECT empty = no release (curl -f exits 22 on the 404).
FAKE_CURL = """#!/usr/bin/env bash
if [ -z "${FAKE_CURL_REDIRECT:-}" ]; then
  exit 22
fi
printf '%s' "$FAKE_CURL_REDIRECT"
"""

FAKE_LAUNCHCTL = """#!/usr/bin/env bash
if [ -n "${FAKE_LAUNCHCTL_LOG:-}" ]; then
  printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_LOG"
fi
case "${1:-}" in
  list)
    if [ -n "${FAKE_LAUNCHCTL_LIST:-}" ]; then
      printf '%s\n' "$FAKE_LAUNCHCTL_LIST"
    else
      echo '{ "PID" = 4242; };'
    fi
    ;;
  *) : ;;
esac
"""


def _write_exe(path: Path, body: str):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sanitized_host_env(environ) -> dict:
    """Copy ``environ`` minus host-session state that breaks gate hermeticity.

    FEISHU_* are legacy-remnant blockers upgrade.sh refuses to run with, and
    WALKCODE_* leak in when this suite itself runs inside a walkcode-driven
    session — e.g. the runtime's WALKCODE_DRIVER_LABEL marker would
    short-circuit upgrade.sh's ps-climb self-detection and flip the
    self-restart tests onto the wrong branch. Tests that need WALKCODE_*
    values pass them explicitly via extra_env.
    """
    return {
        key: value
        for key, value in dict(environ).items()
        if not key.startswith(("FEISHU_", "WALKCODE_"))
    }


class RuntimeDependencyTests(unittest.TestCase):
    def test_runtime_dependencies_are_declared_not_extras(self):
        # A bare install used to build a venv with neither package: Lark
        # ingress and the Claude transport both failed at import while the
        # process stayed up (the 2026-07-24/25 outage).
        import tomllib

        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
        names = {re.split(r"[<>=!~ ]", dep, maxsplit=1)[0] for dep in project["dependencies"]}
        self.assertLessEqual({"claude-agent-sdk", "lark-oapi"}, names)
        self.assertIn("claude-agent-sdk>=0.2.124", project["dependencies"])


class HostEnvSanitizationTests(unittest.TestCase):
    def test_strips_walkcode_and_feishu_vars_and_keeps_the_rest(self):
        env = _sanitized_host_env(
            {
                "WALKCODE_DRIVER_LABEL": "com.walkcode.personal-claude",
                "WALKCODE_V3_LAUNCHD_LABELS": "com.walkcode.a-claude",
                "WALKCODE_SELF_RESTART_DELAY": "0",
                "FEISHU_APP_ID": "cli_x",
                "PATH": "/usr/bin",
                "HOME": "/tmp/h",
            }
        )
        self.assertNotIn("WALKCODE_DRIVER_LABEL", env)
        self.assertNotIn("WALKCODE_V3_LAUNCHD_LABELS", env)
        self.assertNotIn("WALKCODE_SELF_RESTART_DELAY", env)
        self.assertNotIn("FEISHU_APP_ID", env)
        self.assertEqual(env, {"PATH": "/usr/bin", "HOME": "/tmp/h"})


@unittest.skipUnless(_GIT and _BASH, "git and bash required")
class _ScriptGateBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-reltest-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self.fakebin = self.tmp / "bin"
        self.home = self.tmp / "home"
        self.fakebin.mkdir()
        self.home.mkdir()
        _write_exe(self.fakebin / "gh", FAKE_GH)
        _write_exe(self.fakebin / "uv", FAKE_UV)
        _write_exe(self.fakebin / "walkcode", FAKE_WALKCODE)
        _write_exe(self.fakebin / "launchctl", FAKE_LAUNCHCTL)
        # Not on PATH by default: only the tag-resolution tests shadow curl.
        self.fakecurl = self.fakebin / "curl-shim"
        self.fakecurl.mkdir()
        _write_exe(self.fakecurl / "curl", FAKE_CURL)

        self.env = _sanitized_host_env(os.environ)
        self.env["PATH"] = f"{self.fakebin}{os.pathsep}{self.env['PATH']}"
        self.env["HOME"] = str(self.home)
        self.env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        self.env["TMPDIR"] = str(self.tmp)
        self.env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })

        self._git("init", "--bare", str(self.origin), cwd=self.tmp)
        self._git("-c", "init.defaultBranch=main", "init", str(self.work), cwd=self.tmp)
        self._git("checkout", "-B", "main", cwd=self.work)
        self._set_version("0.10.0")
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

    def _upgrade_env(self, **extra):
        env = {
            "LOG_CLAUDE": str(self.tmp / "c.log"),
            "LOG_CODEX": str(self.tmp / "x.log"),
            "TMPDIR": str(self.tmp),
        }
        env.update(extra)
        return env


class ReleaseGateTests(_ScriptGateBase):
    def test_install_script_installs_claude_sdk_in_tool_env(self):
        text = INSTALL_SH.read_text()
        self.assertIn("--with claude-agent-sdk", text)
        self.assertNotIn("walkcode[summary]", text)

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

    def test_prepare_rejects_stale_main(self):
        other = self.tmp / "other"
        self._git("clone", str(self.origin), str(other), cwd=self.tmp)
        (other / "x.txt").write_text("new")
        self._git("add", "-A", cwd=other)
        self._git("commit", "-m", "advance", cwd=other)
        self._git("push", "origin", "HEAD:main", cwd=other)
        r = self._run("release.sh", "prepare", "0.10.1")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("origin/main", r.stdout + r.stderr)

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
        self.assertIn("v0.10.1", self._git_out("ls-remote", "--tags", "origin"))

    def test_publish_reentrant_pushes_remote_tag_and_notes(self):
        # prior release tag so notes have a base
        self._git("tag", "-a", "v0.10.0", "-m", "v0.10.0", cwd=self.work)
        self._git("push", "origin", "v0.10.0", cwd=self.work)
        self._set_version("0.10.1")
        self._git("commit", "-am", "bump 0.10.1", cwd=self.work)
        self._git("push", "origin", "main", cwd=self.work)
        # half-done publish: local tag at HEAD but NOT pushed to origin
        self._git("tag", "-a", "v0.10.1", "-m", "v0.10.1", cwd=self.work)
        notes_file = self.tmp / "notes.txt"
        r = self._run("release.sh", "publish", "0.10.1",
                      extra_env={"FAKE_GH_NOTES_FILE": str(notes_file)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # ISSUE_1: remote tag must now exist
        self.assertIn("v0.10.1", self._git_out("ls-remote", "--tags", "origin"))
        # ISSUE_2: notes must not be empty (current tag excluded from base)
        self.assertIn("bump 0.10.1", notes_file.read_text())

    def test_publish_aborts_on_release_view_error(self):
        self._set_version("0.10.1")
        self._git("commit", "-am", "bump 0.10.1", cwd=self.work)
        self._git("push", "origin", "main", cwd=self.work)
        r = self._run("release.sh", "publish", "0.10.1",
                      extra_env={"FAKE_RELEASE_VIEW_RC": "1",
                                 "FAKE_RELEASE_VIEW_MSG": "HTTP 500 internal error"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("release view", (r.stdout + r.stderr).lower())


class UpgradeGateTests(_ScriptGateBase):
    def test_upgrade_does_not_require_legacy_codex_env_or_install_hooks(self):
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            WALKCODE_CODEX_ENV=str(self.tmp / "nope.env"),
            FAKE_UV_LOG=str(uv_log),
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("hooks installed", r.stdout + r.stderr)
        self.assertIn("native doctor", r.stdout + r.stderr)
        uv_invocation = uv_log.read_text()
        self.assertIn("--with claude-agent-sdk", uv_invocation)
        self.assertIn("--reinstall", uv_invocation)
        self.assertIn("--refresh-package walkcode", uv_invocation)

    def _offline_tag_env(self, **extra):
        """No gh release lookup, no reachable releases API — so only the
        release-page redirect can answer. The dead proxy keeps the anonymous
        API attempt hermetic (it used to reach github.com from the suite)."""
        env = self._upgrade_env(
            FAKE_GH_LATEST_TAG="NONE",
            https_proxy="http://127.0.0.1:1",
            HTTPS_PROXY="http://127.0.0.1:1",
            PATH=f"{self.fakecurl}{os.pathsep}{self.env['PATH']}",
        )
        env.update(extra)
        return env

    def test_upgrade_pins_the_release_tag_gh_resolves(self):
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            FAKE_GH_LATEST_TAG="v1.2.3", FAKE_UV_LOG=str(uv_log)))

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("walkcode.git@v1.2.3", uv_log.read_text())

    def test_upgrade_rejects_a_tag_shape_that_is_not_semver(self):
        # The tag lands in a shell `uv tool install` command; a compromised or
        # merely renamed release must not smuggle anything into it.
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._offline_tag_env(
            FAKE_GH_LATEST_TAG="v1.2.3; touch /tmp/pwned", FAKE_UV_LOG=str(uv_log)))

        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(uv_log.exists(), "a malformed tag must install nothing")

    def test_upgrade_falls_back_to_the_release_page_redirect(self):
        # 2026-08-07: the anonymous releases API answered 403 (rate limit) and
        # upgrade.sh silently installed from main. The redirect is the no-API
        # backstop, and unlike a bare tag list it can only name a tag that
        # actually has a Release.
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._offline_tag_env(
            FAKE_CURL_REDIRECT="https://github.com/0x5446/walkcode/releases/tag/v0.14.19",
            FAKE_UV_LOG=str(uv_log)))

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("walkcode.git@v0.14.19", uv_log.read_text())

    def test_upgrade_refuses_to_install_from_main_when_no_tag_resolves(self):
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._offline_tag_env(
            FAKE_CURL_REDIRECT="", FAKE_UV_LOG=str(uv_log)))

        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(uv_log.exists(), "nothing may be installed without a tag")

    def test_allow_main_override_installs_from_the_default_branch(self):
        uv_log = self.tmp / "uv.log"
        r = self._run("upgrade.sh", extra_env=self._offline_tag_env(
            FAKE_CURL_REDIRECT="", FAKE_UV_LOG=str(uv_log), WALKCODE_ALLOW_MAIN="1"))

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        invocation = uv_log.read_text()
        self.assertIn("walkcode.git", invocation)
        self.assertNotIn("walkcode.git@", invocation)

    def test_upgrade_allows_v3_pass_through_agent_wrappers(self):
        wrappers = self.home / ".agent-control-plane"
        wrappers.mkdir(parents=True)
        (wrappers / "agent-wrappers.sh").write_text(
            'claude() { command claude "$@"; }\n'
            'codex() { command codex "$@"; }\n'
        )
        (self.home / ".zshrc").write_text("source ~/.agent-control-plane/agent-wrappers.sh")

        r = self._run("upgrade.sh", extra_env=self._upgrade_env())

        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("legacy agent-wrappers", r.stdout + r.stderr)

    def test_v3_launchd_labels_are_restarted_when_configured(self):
        log = self.tmp / "launchctl.log"
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            WALKCODE_V3_LAUNCHD_LABELS="com.walkcode.telegram-claude,com.walkcode.telegram-codex",
            FAKE_LAUNCHCTL_LOG=str(log),
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        content = log.read_text()
        self.assertIn("gui/", content)
        self.assertIn("com.walkcode.telegram-claude", content)
        self.assertIn("com.walkcode.telegram-codex", content)

    def test_empty_labels_auto_discovers_loaded_runtimes_excluding_taps(self):
        # 2026-07-14 v0.13.1 upgrade: empty WALKCODE_V3_LAUNCHD_LABELS left
        # all five instances running the old version. Empty env must fall
        # back to loaded com.walkcode.* labels, never touching tap-* proxies
        # (they carry live Claude API traffic).
        log = self.tmp / "launchctl.log"
        walkdir = self.home / ".walkcode"
        walkdir.mkdir(parents=True, exist_ok=True)
        (walkdir / "a-claude.env").write_text("WALKCODE_CHANNEL=lark\n")
        listing = "\n".join(
            [
                "1\t0\tcom.walkcode.a-claude",
                "2\t0\tcom.walkcode.tap-a",
                "3\t0\tcom.apple.foo",
            ]
        )
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            FAKE_LAUNCHCTL_LOG=str(log),
            FAKE_LAUNCHCTL_LIST=listing,
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
        self.assertTrue(any("com.walkcode.a-claude" in l for l in kicks), kicks)
        self.assertFalse(any("tap-a" in l for l in kicks), kicks)
        self.assertFalse(any("com.apple.foo" in l for l in kicks), kicks)
        # doctor runs bound to the discovered instance's env file
        self.assertIn("native doctor ok", r.stdout + r.stderr)

    def test_explicit_labels_never_restart_tap_proxies(self):
        # Taps carry live Claude API traffic — even an explicit label list
        # must not kickstart them.
        log = self.tmp / "launchctl.log"
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            WALKCODE_V3_LAUNCHD_LABELS="com.walkcode.tap-work,com.walkcode.a-claude",
            FAKE_LAUNCHCTL_LOG=str(log),
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
        self.assertTrue(any("com.walkcode.a-claude" in l for l in kicks), kicks)
        self.assertFalse(any("tap-work" in l for l in kicks), kicks)
        self.assertRegex(r.stdout + r.stderr, r"tap proxy|tap 代理")

    def test_self_driver_label_restart_is_deferred(self):
        # ADR 0058 suicide trap: an upgrade run from inside a session that a
        # com.walkcode.* runtime drives must not kickstart that runtime under
        # its own feet (2026-07-20 15:13: the restart severed the session's
        # driver mid-turn; Feishu went silent for half an hour). The self
        # label is deferred to a detached restart; other labels restart now.
        log = self.tmp / "launchctl.log"
        listing = "\n".join(
            [
                "1\t0\tcom.walkcode.a-claude",
                "2\t0\tcom.walkcode.b-codex",
            ]
        )
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            FAKE_LAUNCHCTL_LOG=str(log),
            FAKE_LAUNCHCTL_LIST=listing,
            WALKCODE_DRIVER_LABEL="com.walkcode.a-claude",
            WALKCODE_SELF_RESTART_DELAY="0",
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, r"deferred|脱管重启")
        kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
        self.assertTrue(any("com.walkcode.b-codex" in l for l in kicks), kicks)
        # The self label is restarted by the detached scheduler (delay 0),
        # not by the script's own restart loop — poll briefly for it.
        deadline = time.time() + 10.0
        while time.time() < deadline:
            kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
            if any("com.walkcode.a-claude" in l for l in kicks):
                break
            time.sleep(0.1)
        self.assertTrue(any("com.walkcode.a-claude" in l for l in kicks), kicks)

    def test_self_driver_process_tree_fallback_defers(self):
        # Pre-marker runtimes (first upgrade to v0.14.10) rely on the ps
        # climb: ancestor chain ends at a fake `walkcode native serve` whose
        # PID maps to a label via launchctl list (review R1 tests#1).
        fake_ps = """#!/usr/bin/env bash
mode=""; pid=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) mode="$2"; shift 2 ;;
    -p) pid="$2"; shift 2 ;;
    *) shift ;;
  esac
done
case "$mode" in
  command=) if [ "$pid" = "77777" ]; then echo "/usr/bin/python3 walkcode native serve"; else echo "bash ./upgrade.sh"; fi ;;
  ppid=)    if [ "$pid" = "77777" ]; then echo "    1"; else echo "77777"; fi ;;
esac
"""
        _write_exe(self.fakebin / "ps", fake_ps)
        self.addCleanup(lambda: (self.fakebin / "ps").unlink())
        log = self.tmp / "launchctl.log"
        listing = "\n".join(
            [
                "77777\t0\tcom.walkcode.a-claude",
                "2\t0\tcom.walkcode.b-codex",
            ]
        )
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            FAKE_LAUNCHCTL_LOG=str(log),
            FAKE_LAUNCHCTL_LIST=listing,
            WALKCODE_SELF_RESTART_DELAY="0",
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, r"deferred|脱管重启")
        kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
        self.assertTrue(any("com.walkcode.b-codex" in l for l in kicks), kicks)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
            if any("com.walkcode.a-claude" in l for l in kicks):
                break
            time.sleep(0.1)
        self.assertTrue(any("com.walkcode.a-claude" in l for l in kicks), kicks)

    def test_invalid_self_restart_delay_falls_back_and_never_kicks_now(self):
        # `sleep garbage; kickstart` would fire the kickstart immediately —
        # the exact suicide the guard prevents. Invalid delay must warn, fall
        # back to 120s, and the self label must NOT restart during the test.
        log = self.tmp / "launchctl.log"
        listing = "\n".join(
            [
                "1\t0\tcom.walkcode.a-claude",
                "2\t0\tcom.walkcode.b-codex",
            ]
        )
        # A failing fake `sleep` kills the detached timer instantly: no
        # leaked 120s process outliving the test, and the `&&` chain must
        # prevent the kickstart from firing (review R2 tests#2).
        _write_exe(self.fakebin / "sleep", "#!/usr/bin/env bash\nexit 1\n")
        self.addCleanup(lambda: (self.fakebin / "sleep").unlink())
        r = self._run("upgrade.sh", extra_env=self._upgrade_env(
            FAKE_LAUNCHCTL_LOG=str(log),
            FAKE_LAUNCHCTL_LIST=listing,
            WALKCODE_DRIVER_LABEL="com.walkcode.a-claude",
            WALKCODE_SELF_RESTART_DELAY="abc",
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertRegex(r.stdout + r.stderr, r"invalid|非法")
        time.sleep(1.0)
        kicks = [l for l in log.read_text().splitlines() if "kickstart" in l]
        self.assertTrue(any("com.walkcode.b-codex" in l for l in kicks), kicks)
        self.assertFalse(any("com.walkcode.a-claude" in l for l in kicks), kicks)

    def test_self_driver_dry_run_prints_plan_without_scheduling(self):
        log = self.tmp / "launchctl.log"
        listing = "1\t0\tcom.walkcode.a-claude"
        r = self._run("upgrade.sh", "--dry-run", extra_env=self._upgrade_env(
            FAKE_LAUNCHCTL_LOG=str(log),
            FAKE_LAUNCHCTL_LIST=listing,
            WALKCODE_DRIVER_LABEL="com.walkcode.a-claude",
            WALKCODE_SELF_RESTART_DELAY="0",
        ))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("[dry-run] deferred self restart", r.stdout + r.stderr)
        time.sleep(0.5)
        kicks = [l for l in (log.read_text().splitlines() if log.exists() else []) if "kickstart" in l]
        self.assertFalse(any("com.walkcode.a-claude" in l for l in kicks), kicks)

    def test_shell_scripts_brace_vars_before_cjk(self):
        # macOS bash 3.2 misparses `$VAR` glued to a CJK character as part of
        # the variable name ("ENV_FILE?: unbound variable" under set -u) —
        # every expansion followed by CJK text must use ${VAR}.
        pat = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*[　-〿一-鿿＀-￯]")
        for script in (UPGRADE_SH, RELEASE_SH, INSTALL_SH, UNINSTALL_SH):
            for lineno, line in enumerate(script.read_text().splitlines(), 1):
                self.assertIsNone(
                    pat.search(line),
                    f"{script.name}:{lineno} unbraced var before CJK: {line.strip()}",
                )

    def test_upgrade_blocks_legacy_remnants(self):
        hooks = self.home / ".codex" / "hooks.json"
        hooks.parent.mkdir(parents=True)
        hooks.write_text('{"hooks": {"Stop": [{"hooks": [{"command": "walkcode hook stop"}]}]}}')

        r = self._run("upgrade.sh", extra_env=self._upgrade_env())

        self.assertNotEqual(r.returncode, 0)
        combined = r.stdout + r.stderr
        self.assertIn("Legacy hook", combined)
        self.assertIn("legacy remnants", combined)

    def test_active_lock_blocks(self):
        lock = self.tmp / "walkcode-upgrade.lock"
        lock.mkdir()
        (lock / "pid").write_text(str(os.getpid()))  # alive
        r = self._run("upgrade.sh", extra_env=self._upgrade_env())
        self.assertNotEqual(r.returncode, 0)
        self.assertRegex(r.stdout + r.stderr, r"another upgrade is running|已有升级在运行")

    def test_stale_lock_reclaimed(self):
        lock = self.tmp / "walkcode-upgrade.lock"
        lock.mkdir()
        (lock / "pid").write_text("999999")  # dead pid
        r = self._run("upgrade.sh", extra_env=self._upgrade_env())
        self.assertNotRegex(r.stdout + r.stderr, r"another upgrade is running|已有升级在运行")
        self.assertRegex(r.stdout + r.stderr, r"stale|残留")


_WC_HOOK = "WALKCODE_ENV_FILE=/x/work-claude.env walkcode native hook {} --agent claude --defer"
_OTHER_HOOK = "node /x/other-hook.mjs"


@unittest.skipUnless(_BASH and shutil.which("python3"), "bash and python3 required")
class UninstallScriptTests(unittest.TestCase):
    """uninstall.sh against a throwaway HOME with fake launchctl/uv on PATH."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wc-uninstall-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        fakebin = self.tmp / "bin"
        fakebin.mkdir()
        _write_exe(fakebin / "launchctl", FAKE_LAUNCHCTL)
        _write_exe(fakebin / "uv", FAKE_UV)
        self.launchctl_log = self.tmp / "launchctl.log"
        self.uv_log = self.tmp / "uv.log"

        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        for label in ("com.walkcode.a-claude", "com.walkcode.b-codex", "com.walkcode.tap-work"):
            (agents / f"{label}.plist").write_text("<plist/>")
        self.agents = agents

        # Mixed PreToolUse entry: the other hook must survive.
        self.claude_settings = self.home / ".claude-profiles" / "work" / "settings.json"
        self.claude_settings.parent.mkdir(parents=True)
        self.claude_settings.write_text(json.dumps({
            "model": "opus",
            "hooks": {
                "PreToolUse": [{"matcher": "", "hooks": [
                    {"type": "command", "command": _OTHER_HOOK},
                    {"type": "command", "command": _WC_HOOK.format("PreToolUse"), "timeout": 1830},
                ]}],
                "Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": _WC_HOOK.format("Stop")},
                ]}],
                "SessionStart": [{"matcher": "", "hooks": [
                    {"type": "command", "command": _OTHER_HOOK},
                ]}],
            },
        }, indent=2))
        self.codex_hooks = self.home / ".codex" / "hooks.json"
        self.codex_hooks.parent.mkdir(parents=True)
        self.codex_hooks.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "walkcode hook stop"},
        ]}]}}))
        self.clean_codex = self.home / ".codex-profiles" / "personal" / "hooks.json"
        self.clean_codex.parent.mkdir(parents=True)
        self.clean_codex.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": _OTHER_HOOK},
        ]}]}}))

        walk = self.home / ".walkcode"
        (walk / "workspace").mkdir(parents=True)
        (walk / "logs").mkdir()
        self.kept = [
            walk / "workspace" / "user_code.py",
            walk / "a-claude.env",
            walk / "a-claude-state.json",
            walk / "logs" / "a-claude.log",
        ]
        for path in self.kept:
            path.write_text("keep me")

        self.env = _sanitized_host_env(os.environ)
        self.env.update({
            "PATH": f"{fakebin}{os.pathsep}{self.env['PATH']}",
            "HOME": str(self.home),
            "LANG": "en_US.UTF-8",
            "FAKE_LAUNCHCTL_LOG": str(self.launchctl_log),
            "FAKE_UV_LOG": str(self.uv_log),
            "FAKE_LAUNCHCTL_LIST": "\n".join([
                "PID\tStatus\tLabel",
                "123\t0\tcom.walkcode.a-claude",
                "-\t0\tcom.walkcode.tap-work",
                "77\t0\tcom.apple.foo",
            ]),
        })

    def _run(self, *args, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        # New session = no controlling terminal: every prompt must take the
        # non-interactive default instead of blocking on /dev/tty.
        return subprocess.run(
            [_BASH, str(UNINSTALL_SH), *args], env=env, capture_output=True,
            text=True, errors="replace", start_new_session=True, timeout=60,
        )

    def _snapshot(self):
        return {
            str(p.relative_to(self.home)): p.read_bytes()
            for p in self.home.rglob("*") if p.is_file()
        }

    def _log(self, path):
        return path.read_text().splitlines() if path.exists() else []

    def _backups(self):
        return sorted(self.home.rglob("*.walkcode-uninstall-*.bak"))

    def test_dry_run_has_no_side_effects(self):
        before = self._snapshot()
        r = self._run("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._backups(), [])
        self.assertFalse(any("bootout" in l for l in self._log(self.launchctl_log)))
        self.assertEqual(self._log(self.uv_log), [])
        self.assertIn("[dry-run] launchctl bootout", r.stdout)
        self.assertIn("[dry-run] uv tool uninstall walkcode", r.stdout)
        self.assertIn("[dry-run] remove 2 WalkCode hook(s)", r.stdout)

    def test_non_interactive_without_yes_aborts_untouched(self):
        before = self._snapshot()
        r = self._run()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._log(self.uv_log), [])

    def test_removes_only_walkcode_hooks_and_non_tap_agents(self):
        original_settings = self.claude_settings.read_text()
        original_clean = self.clean_codex.read_text()
        r = self._run("--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        bootouts = [l for l in self._log(self.launchctl_log) if l.startswith("bootout")]
        uid = os.getuid()
        self.assertIn(f"bootout gui/{uid}/com.walkcode.a-claude", bootouts)
        self.assertIn(f"bootout gui/{uid}/com.walkcode.b-codex", bootouts)
        self.assertFalse(any("tap-" in l for l in bootouts), bootouts)
        self.assertFalse((self.agents / "com.walkcode.a-claude.plist").exists())
        self.assertFalse((self.agents / "com.walkcode.b-codex.plist").exists())
        self.assertTrue((self.agents / "com.walkcode.tap-work.plist").exists())
        self.assertEqual(self._log(self.uv_log), ["tool uninstall walkcode"])

        settings = json.loads(self.claude_settings.read_text())
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(
            settings["hooks"]["PreToolUse"],
            [{"matcher": "", "hooks": [{"type": "command", "command": _OTHER_HOOK}]}],
        )
        self.assertNotIn("Stop", settings["hooks"])
        self.assertIn("SessionStart", settings["hooks"])
        self.assertNotIn("hooks", json.loads(self.codex_hooks.read_text()))
        self.assertEqual(self.clean_codex.read_text(), original_clean)

        backups = self._backups()
        self.assertEqual(
            sorted(b.parent for b in backups),
            sorted([self.claude_settings.parent, self.codex_hooks.parent]),
        )
        claude_backup = next(b for b in backups if b.parent == self.claude_settings.parent)
        self.assertEqual(claude_backup.read_text(), original_settings)

        # Non-interactive: state/logs kept too; workspace and env never touched.
        for path in self.kept:
            self.assertEqual(path.read_text(), "keep me", path)

    def test_self_driver_label_is_booted_out_last(self):
        r = self._run("--yes", WALKCODE_DRIVER_LABEL="com.walkcode.a-claude")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        bootouts = [l for l in self._log(self.launchctl_log) if l.startswith("bootout")]
        self.assertTrue(bootouts[-1].endswith("/com.walkcode.a-claude"), bootouts)
        self.assertEqual(len(bootouts), 2, bootouts)


if __name__ == "__main__":
    unittest.main()
