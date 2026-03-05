"""CBuddy CLI."""

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def cmd_serve(_args):
    import uvicorn
    from .config import Config
    from .server import app, init, start_ws_client

    cfg = Config.load()
    init(cfg)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    start_ws_client(cfg)
    print(f"CBuddy serving on http://localhost:{cfg.port}")
    print(f"  Feishu {cfg.feishu_receive_id_type}: {cfg.feishu_receive_id}")
    print(f"  Hook: POST http://localhost:{cfg.port}/hook")
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")


def _detect_tty() -> str:
    """Detect TTY from process tree (stdin is piped in hook context)."""
    # Walk up process tree to find a real terminal
    pid = str(os.getppid())
    for _ in range(5):
        try:
            r = subprocess.run(
                ["ps", "-o", "tty=", "-p", pid],
                capture_output=True, text=True, timeout=2,
            )
            t = r.stdout.strip()
            if t and t != "??":
                return f"/dev/{t}"
            r = subprocess.run(
                ["ps", "-o", "ppid=", "-p", pid],
                capture_output=True, text=True, timeout=2,
            )
            pid = r.stdout.strip()
            if not pid:
                break
        except Exception:
            break
    return ""


def cmd_hook(args):
    """Handle a Claude Code hook event: read stdin, POST to server."""
    # Read hook data from stdin (Claude Code pipes JSON)
    try:
        hook_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_data = {}

    tty = os.environ.get("TTY", "") or _detect_tty()
    cwd = os.getcwd()
    session_id = hook_data.get("session_id", "")

    # Extract message content
    message = hook_data.get("message", "")
    if not message and args.hook_type == "stop":
        # Stop hook: try to get last assistant message from transcript
        transcript = hook_data.get("transcript", [])
        if isinstance(transcript, list):
            for entry in reversed(transcript):
                if isinstance(entry, dict) and entry.get("role") == "assistant":
                    message = entry.get("message", "")[:500]
                    break
        if not message:
            message = hook_data.get("transcript_summary", "")[:500]

    matcher = os.environ.get("CLAUDE_NOTIFICATION_TYPE", "")

    port = int(os.environ.get("CBUDDY_PORT", os.environ.get("PORT", "3000")))
    payload = json.dumps({
        "type": args.hook_type,
        "tty": tty,
        "cwd": cwd,
        "session_id": session_id,
        "message": message,
        "matcher": matcher,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://localhost:{port}/hook",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # server may be down


def cmd_install_hooks(_args):
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(f"Error: {settings_path} not found")
        sys.exit(1)

    settings = json.loads(settings_path.read_text())

    def hook_cmd(hook_type: str, sound: str) -> str:
        return f"afplay /System/Library/Sounds/{sound}.aiff & cbuddy hook {hook_type}"

    settings["hooks"] = {
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": hook_cmd("stop", "Hero")}
        ]}],
        "Notification": [{"matcher": "", "hooks": [
            {"type": "command", "command": hook_cmd("notification", "Ping")}
        ]}],
    }

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(f"Hooks installed to {settings_path}")
    print("Restart Claude Code sessions to activate.")


def cmd_test_inject(args):
    from .tty import inject, validate_tty

    error = validate_tty(args.tty)
    if error:
        print(f"Error: {error}")
        sys.exit(1)

    inject(args.tty, args.text, enter=not args.no_enter)
    suffix = " (no enter)" if args.no_enter else " + Enter"
    print(f"Injected '{args.text}'{suffix} -> {args.tty}")


def main():
    parser = argparse.ArgumentParser(prog="cbuddy", description="Drive your terminal Claude Code from Feishu")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start CBuddy server")

    hp = sub.add_parser("hook", help="Handle a Claude Code hook event (reads stdin)")
    hp.add_argument("hook_type", choices=["stop", "notification"], help="Hook event type")

    sub.add_parser("install-hooks", help="Install Claude Code hooks")

    p = sub.add_parser("test-inject", help="Test terminal injection")
    p.add_argument("tty", help="TTY path, e.g. /dev/ttys003")
    p.add_argument("text", help="Text to inject")
    p.add_argument("--no-enter", action="store_true", help="Don't press Enter")

    args = parser.parse_args()
    cmds = {
        "serve": cmd_serve,
        "hook": cmd_hook,
        "install-hooks": cmd_install_hooks,
        "test-inject": cmd_test_inject,
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
