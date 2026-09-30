"""WalkCode V3 CLI."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


_UPGRADE_SH_URL = "https://raw.githubusercontent.com/0x5446/walkcode/main/upgrade.sh"


def _current_version() -> str:
    try:
        from importlib.metadata import version

        return version("walkcode")
    except Exception:
        return "unknown"


def cmd_install_hooks(_args) -> None:
    print(
        "walkcode install-hooks is not part of the V3 runtime. "
        "Use walkcode native hook from a TUI hook config only when read-only observation and takeover are needed.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _checkout_upgrade_script() -> Path | None:
    """upgrade.sh of the source checkout this package runs from, if any.

    Only a checkout (`uv run walkcode`, editable install) has one; a
    `uv tool install` copy lives in site-packages and does not.
    """
    root = Path(__file__).resolve().parents[2]
    script = root / "upgrade.sh"
    if script.is_file() and (root / "pyproject.toml").is_file():
        return script
    return None


def cmd_upgrade(args) -> None:
    """Delegate to upgrade.sh, the single gated upgrade path.

    This used to be a Python re-implementation that drifted from upgrade.sh
    (no claude-agent-sdk floor, no --reinstall, no legacy-remnant gate, no
    lock, aborted on the first failed kickstart). Instead of keeping two
    copies in sync, run the real script when it ships alongside this code
    and otherwise print how to run it.
    """
    extra = ["--dry-run"] if getattr(args, "dry_run", False) else []
    script = _checkout_upgrade_script()
    if script is not None:
        print(f"Running {script} {' '.join(extra)}".rstrip(), flush=True)
        os.execv("/bin/bash", ["/bin/bash", str(script), *extra])
    print(
        "walkcode upgrade delegates to upgrade.sh, which is not bundled with this install.\n"
        "Run it from a WalkCode checkout:  ./upgrade.sh [--dry-run]\n"
        f"or directly:  curl -fsSL {_UPGRADE_SH_URL} | bash -s -- [--dry-run]",
        file=sys.stderr,
    )
    raise SystemExit(1)


def cmd_uninstall(_args) -> None:
    print("Removing walkcode uv tool.")
    subprocess.run(["uv", "tool", "uninstall", "walkcode"], capture_output=True)
    print(
        "Uninstall complete. For LaunchAgents and TUI hooks run uninstall.sh "
        "(keeps env files and the workspace)."
    )


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

    up = sub.add_parser("upgrade", help="Upgrade to the latest V3 release (runs upgrade.sh)")
    up.add_argument("--dry-run", action="store_true", help="Pass --dry-run to upgrade.sh")
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
