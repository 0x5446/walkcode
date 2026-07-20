"""WalkCode V3 CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path


_GITHUB_REPO = "0x5446/walkcode"
_GITHUB_URL = f"https://github.com/{_GITHUB_REPO}.git"
_HOOKS_ASSIGN = re.compile(r"^\s*hooks\s*=")


def _run(cmd: str, **kwargs) -> None:
    print(f"  -> {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        print(f"command failed with exit code {result.returncode}")
        raise SystemExit(1)


def _get_latest_tag() -> str | None:
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("tag_name")
    except Exception:
        return None


def _current_version() -> str:
    try:
        from importlib.metadata import version

        return version("walkcode")
    except Exception:
        return "unknown"


def _ensure_codex_hooks_feature(config_toml: Path) -> None:
    """Ensure `[features] hooks = true` for Codex native hook observation."""

    if not config_toml.exists():
        config_toml.parent.mkdir(parents=True, exist_ok=True)
        config_toml.write_text("[features]\nhooks = true\n", encoding="utf-8")
        return

    content = config_toml.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        data = {}
    features = data.get("features", {}) if isinstance(data, dict) else {}
    if isinstance(features, dict) and features.get("hooks") is True:
        return

    new_content = _set_features_hooks_true(content)
    try:
        check = tomllib.loads(new_content)
    except tomllib.TOMLDecodeError:
        if data:
            print(
                f"[walkcode] skipped enabling codex hooks flag: editing {config_toml} "
                "would produce invalid TOML; please set [features] hooks = true manually",
                file=sys.stderr,
            )
            return
    else:
        if not isinstance(check, dict) or check.get("features", {}).get("hooks") is not True:
            if data:
                print(
                    f"[walkcode] skipped enabling codex hooks flag: editing {config_toml} "
                    "would not yield a valid [features] hooks = true; please set it manually",
                    file=sys.stderr,
                )
                return

    config_toml.write_text(new_content, encoding="utf-8")


def _set_features_hooks_true(content: str) -> str:
    """Return TOML content with `[features] hooks = true` set."""

    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_features = False
    saw_features = False
    saw_hooks = False
    header_pos = -1

    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and "]" in stripped
        if is_header:
            if in_features and not saw_hooks and header_pos >= 0:
                out.insert(header_pos + 1, "hooks = true\n")
                saw_hooks = True
            compact = stripped.split("#", 1)[0].replace(" ", "")
            in_features = compact == "[features]"
            if in_features:
                saw_features = True
                saw_hooks = False
                header_pos = len(out)
            out.append(line)
            continue

        if in_features and _HOOKS_ASSIGN.match(line):
            out.append("hooks = true\n" if line.endswith("\n") else "hooks = true")
            saw_hooks = True
            continue

        out.append(line)

    if in_features and not saw_hooks and header_pos >= 0:
        out.insert(header_pos + 1, "hooks = true\n")
    elif not saw_features:
        sep = "" if not out or out[-1].endswith("\n") else "\n"
        out.append(sep + "[features]\nhooks = true\n")

    return "".join(out)


def cmd_install_hooks(_args) -> None:
    print(
        "walkcode install-hooks is not part of the V3 runtime. "
        "Use walkcode native hook from a TUI hook config only when read-only observation and takeover are needed.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _install_claude_hooks(_args) -> None:
    cmd_install_hooks(_args)


def _install_codex_hooks(_args) -> None:
    cmd_install_hooks(_args)


def _parse_launchd_labels(listing: str) -> list[str]:
    """Pick V3 runtime labels out of `launchctl list` output.

    com.walkcode.tap-* is excluded on purpose: the debug proxies carry live
    Claude API traffic, kickstarting them would sever every local session's
    in-flight request.
    """
    labels = []
    for line in listing.splitlines():
        parts = line.split()
        name = parts[-1] if parts else ""
        if name.startswith("com.walkcode.") and not name.startswith("com.walkcode.tap-"):
            labels.append(name)
    return sorted(set(labels))


def _discover_v3_launchd_labels() -> list[str]:
    try:
        result = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return _parse_launchd_labels(result.stdout or "")


def _self_driver_label() -> str:
    """ADR 0058: the launchd label of the runtime driving THIS process, or "".

    Priority 1 is the WALKCODE_DRIVER_LABEL marker exported by `walkcode
    native serve` (v0.14.10+) and inherited by every worker subprocess.
    Fallback climbs the process tree for a `walkcode native serve` ancestor
    and maps its PID to a label via `launchctl list` (LC_ALL=C: day-first
    locales broke ps parsing before, v0.14.4).
    """
    marker = os.environ.get("WALKCODE_DRIVER_LABEL", "")
    if marker:
        return marker
    env = {**os.environ, "LC_ALL": "C"}
    try:
        listing = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, env=env
        ).stdout
    except Exception:
        listing = ""
    pid_to_label: dict[str, str] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1].startswith("com.walkcode."):
            pid_to_label[parts[0]] = parts[-1]
    pid = str(os.getpid())
    for _ in range(25):
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=,command=", "-p", pid],
                capture_output=True,
                text=True,
                env=env,
            ).stdout.strip()
        except Exception:
            return ""
        if not out:
            return ""
        ppid, _, command = out.partition(" ")
        ppid = ppid.strip()
        if "walkcode native serve" in command and pid in pid_to_label:
            return pid_to_label[pid]
        if not ppid or ppid == pid or ppid in {"0", "1"}:
            return ""
        pid = ppid
    return ""


def _schedule_deferred_self_restart(label: str) -> None:
    """Restart our own driver runtime later, from a detached process.

    start_new_session=True: the restarter must survive the SIGTERM it will
    deliver to our own ancestry. `&&` (not `;`): a failed sleep must never
    fall through to an immediate kickstart.
    """
    delay_raw = os.environ.get("WALKCODE_SELF_RESTART_DELAY", "120")
    # isascii too: str.isdigit() accepts full-width digits, which the system
    # `sleep` rejects — the detached restarter would die silently (review R2).
    delay = delay_raw if (delay_raw.isascii() and delay_raw.isdigit()) else "120"
    if delay != delay_raw:
        print(f"invalid WALKCODE_SELF_RESTART_DELAY {delay_raw!r}; using 120s.")
    # Say everything and FLUSH before starting the timer (R3): with a
    # zero/short delay the detached kickstart could otherwise kill the driver
    # before buffered output lands.
    print(
        f"this upgrade runs inside a session driven by {label}; its restart is "
        f"deferred by {delay}s (detached). Wrap up the final reply now — the "
        "session revives on the next message.",
        flush=True,
    )
    sys.stderr.flush()
    uid = str(os.getuid())
    subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            'sleep "$1" && exec launchctl kickstart -k "gui/$2/$3"',
            "sh",
            delay,
            uid,
            label,
        ],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def cmd_upgrade(_args) -> None:
    current = _current_version()
    print(f"Current version: {current}")

    tag = _get_latest_tag()
    if tag:
        print(f"Latest release: {tag}")
        source = f"walkcode @ git+{_GITHUB_URL}@{tag}"
    else:
        print("Could not resolve latest release; installing from default branch.")
        source = f"walkcode @ git+{_GITHUB_URL}"

    python_spec = os.environ.get("WALKCODE_PYTHON", "3.13")
    _run(
        "uv tool install "
        f"--python {shlex.quote(python_spec)} "
        "--with claude-agent-sdk "
        "--with lark-oapi "
        f"{shlex.quote(source)} "
        "--force"
    )

    labels = [
        item.strip()
        for item in os.environ.get("WALKCODE_V3_LAUNCHD_LABELS", "").split(",")
        if item.strip()
    ]
    if not labels:
        labels = _discover_v3_launchd_labels()
        if labels:
            print(
                "WALKCODE_V3_LAUNCHD_LABELS is empty; restarting discovered labels: "
                + ", ".join(labels)
            )
    # Hard guard even against explicit configuration: taps proxy live Claude
    # API traffic; kickstarting one severs every local session's in-flight
    # request.
    for label in labels:
        if label.startswith("com.walkcode.tap-"):
            print(f"refusing to restart tap proxy {label} (carries live Claude API traffic).")
    labels = [label for label in labels if not label.startswith("com.walkcode.tap-")]
    # ADR 0058 suicide-trap guard, same semantics as upgrade.sh: never
    # kickstart the runtime that drives the session running this command.
    self_label = _self_driver_label()
    deferred_self = ""
    if self_label and self_label in labels:
        deferred_self = self_label
        labels = [label for label in labels if label != self_label]
    if labels:
        uid = os.getuid()
        for label in labels:
            _run(f"launchctl kickstart -k gui/{uid}/{shlex.quote(label)}")
    else:
        print(
            "WALKCODE_V3_LAUNCHD_LABELS is empty and no loaded com.walkcode.* "
            "service was found; no V3 runtime was restarted."
        )

    env_file = os.environ.get("WALKCODE_ENV_FILE")
    if env_file:
        _run(f"WALKCODE_ENV_FILE={shlex.quote(env_file)} walkcode native doctor")
    else:
        # A bare doctor without an env file only reports a config error; bind
        # each restarted instance to its own env file instead.
        ran_doctor = False
        for label in [*labels, *([deferred_self] if deferred_self else [])]:
            label_env = Path.home() / ".walkcode" / (label.removeprefix("com.walkcode.") + ".env")
            if label_env.is_file():
                _run(f"WALKCODE_ENV_FILE={shlex.quote(str(label_env))} walkcode native doctor")
                ran_doctor = True
            else:
                print(f"no env file for {label} (expected {label_env}); doctor skipped.")
        if not ran_doctor:
            _run("walkcode native doctor")
    print("Upgrade complete.")
    if deferred_self:
        # Scheduled dead last, after every print: even a zero/short delay
        # must not kill the driver before this command's output lands.
        _schedule_deferred_self_restart(deferred_self)


def cmd_uninstall(_args) -> None:
    print("Removing walkcode uv tool.")
    subprocess.run(["uv", "tool", "uninstall", "walkcode"], capture_output=True)
    print("Uninstall complete. Remove any V3 LaunchAgents and env files you no longer need.")


def cmd_removed_legacy(args) -> None:
    command = getattr(args, "command", "") or "legacy command"
    print(
        f"walkcode {command} belongs to the pre-V3 runtime and is no longer a product CLI path. "
        "Use walkcode native doctor, walkcode native serve, or walkcode native hook.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def cmd_native(args) -> None:
    from .channel_native_runtime import run_native_cli

    run_native_cli(args)


def main() -> None:
    parser = argparse.ArgumentParser(prog="walkcode", description="Channel-native runtime for coding agents")
    parser.add_argument("-v", "--version", action="version", version=f"walkcode {_current_version()}")
    sub = parser.add_subparsers(dest="command")

    for legacy_name in (
        "serve",
        "start",
        "stop",
        "restart",
        "status",
        "hook",
        "install-hooks",
        "clean-images",
        "test-inject",
    ):
        legacy_parser = sub.add_parser(legacy_name, help=argparse.SUPPRESS)
        legacy_parser.add_argument("legacy_args", nargs=argparse.REMAINDER)

    sub.add_parser("upgrade", help="Upgrade to latest V3 release")
    sub.add_parser("uninstall", help="Uninstall WalkCode CLI")

    np = sub.add_parser("native", help="Channel-native V3 runtime")
    nsub = np.add_subparsers(dest="native_command", required=True)

    nd = nsub.add_parser("doctor", help="Show channel-native V3 runtime status")
    nd.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    dbg = nsub.add_parser("debug", help="Run channel-native module-level diagnostics")
    dbgsub = dbg.add_subparsers(dest="debug_module", required=True)
    dtg = dbgsub.add_parser("telegram", help="Inspect Telegram ingress without consuming updates")
    dtg.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    dtg.add_argument("--limit", type=int, default=5, help="Maximum pending updates to inspect")
    dlk = dbgsub.add_parser("lark", help="Check Lark credentials, domain, and SDK availability")
    dlk.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    ns = nsub.add_parser("serve", help="Run channel-native V3 runtime")
    ns.add_argument("--once", action="store_true", help="Process one polling cycle and exit")
    ns.add_argument("--poll-timeout", type=int, default=30, help="Telegram getUpdates timeout in seconds")
    ns.add_argument("--limit", type=int, default=25, help="Telegram getUpdates limit")

    nh = nsub.add_parser("hook", help="Handle a channel-native TUI hook event (reads JSON from stdin)")
    nh.add_argument(
        "hook_type",
        help="Native TUI hook event type, e.g. Stop, UserPromptSubmit, stop, or user-prompt-submit",
    )
    nh.add_argument("--agent", choices=["claude", "codex"], default="", help="Agent type for the TUI session")
    nh.add_argument(
        "--defer",
        action="store_true",
        help="Persist the hook locally and let the running native service process Telegram side effects",
    )
    nh.add_argument(
        "--gate",
        action="store_true",
        help=(
            "Blocking PreToolUse gate: spool the observation copy (implies --defer), then "
            "hold the tool call until a channel-side permission/AskUserQuestion decision "
            "lands; requires a larger Claude hook timeout (e.g. 1830)"
        ),
    )
    nh.add_argument("--json", action="store_true", help="Print machine-readable JSON")

    args = parser.parse_args()
    cmds = {
        "serve": cmd_removed_legacy,
        "start": cmd_removed_legacy,
        "stop": cmd_removed_legacy,
        "restart": cmd_removed_legacy,
        "status": cmd_removed_legacy,
        "hook": cmd_removed_legacy,
        "install-hooks": cmd_install_hooks,
        "upgrade": cmd_upgrade,
        "uninstall": cmd_uninstall,
        "clean-images": cmd_removed_legacy,
        "test-inject": cmd_removed_legacy,
        "native": cmd_native,
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
