"""Agent Hotline CLI."""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

from .tty import detect_terminal_binding

_RUNTIME_DIR = Path.home() / ".agent-hotline"
_PID_FILE = _RUNTIME_DIR / "agent-hotline.pid"
_DEFAULT_LOG = _RUNTIME_DIR / "agent-hotline.log"


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
    print(f"Agent Hotline serving on http://localhost:{cfg.port}")
    print(f"  Feishu {cfg.feishu_receive_id_type}: {cfg.feishu_receive_id}")
    print(f"  Hook: POST http://localhost:{cfg.port}/hook")
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")


# --- Daemon management ---

def _read_pid() -> int | None:
    if not _PID_FILE.exists():
        return None
    try:
        pid = int(_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, OSError):
        _PID_FILE.unlink(missing_ok=True)
        return None


def _wait_exit(pid: int, timeout: float = 5.0) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def cmd_start(args):
    pid = _read_pid()
    if pid:
        print(f"Agent Hotline already running (pid {pid})")
        sys.exit(1)

    log_path = Path(args.log) if args.log != "-" else None
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # Build command: use the same Python + module to run serve
    cmd = [sys.executable, "-m", "agent_hotline", "serve"]

    if log_path:
        log_file = open(log_path, "a")
        stdout = stderr = log_file
    else:
        stdout = stderr = subprocess.DEVNULL

    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=os.getcwd(),
    )

    _PID_FILE.write_text(str(proc.pid))
    msg = f"Agent Hotline started (pid {proc.pid})"
    if log_path:
        msg += f", log: {log_path}"
    print(msg)


def cmd_stop(_args):
    pid = _read_pid()
    if not pid:
        print("Agent Hotline is not running")
        sys.exit(1)

    os.kill(pid, signal.SIGTERM)
    if _wait_exit(pid):
        _PID_FILE.unlink(missing_ok=True)
        print(f"Agent Hotline stopped (pid {pid})")
    else:
        os.kill(pid, signal.SIGKILL)
        _PID_FILE.unlink(missing_ok=True)
        print(f"Agent Hotline killed (pid {pid})")


def cmd_restart(args):
    pid = _read_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        if not _wait_exit(pid):
            os.kill(pid, signal.SIGKILL)
        _PID_FILE.unlink(missing_ok=True)
        print(f"Agent Hotline stopped (pid {pid})")

    cmd_start(args)


def cmd_status(_args):
    pid = _read_pid()
    if pid:
        print(f"Agent Hotline is running (pid {pid})")
    else:
        print("Agent Hotline is not running")
        sys.exit(1)


def cmd_hook(args):
    """Handle a Claude Code hook event: read stdin, POST to server."""
    # Read hook data from stdin (Claude Code pipes JSON)
    try:
        hook_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_data = {}

    detected_tty, tty_pid, tty_pid_started_at = detect_terminal_binding()
    tty = os.environ.get("TTY", "") or detected_tty
    cwd = os.getcwd()
    session_id = hook_data.get("session_id", "")

    # Extract message from hook input (Stop hook provides last_assistant_message)
    message = hook_data.get("last_assistant_message", "")
    # Filter out non-useful messages
    _SKIP = {"no response requested.", ""}
    if message.strip().lower() in _SKIP:
        message = ""

    matcher = os.environ.get("CLAUDE_NOTIFICATION_TYPE", "")

    port = int(os.environ.get("AGENT_HOTLINE_PORT", os.environ.get("PORT", "3001")))
    payload = json.dumps({
        "type": args.hook_type,
        "tty": tty,
        "cwd": cwd,
        "session_id": session_id,
        "message": message,
        "matcher": matcher,
        "tty_pid": tty_pid,
        "tty_pid_started_at": tty_pid_started_at,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/hook",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[agent-hotline] hook failed: {e}", file=sys.stderr)


def cmd_install_hooks(_args):
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(f"Error: {settings_path} not found")
        sys.exit(1)

    settings = json.loads(settings_path.read_text())

    def hook_cmd(hook_type: str, sound: str) -> str:
        return f"afplay /System/Library/Sounds/{sound}.aiff & agent-hotline hook {hook_type}"

    settings["hooks"] = {
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": hook_cmd("stop", "Hero")}
        ]}],
        "Notification": [{"matcher": "permission_prompt|idle_prompt|elicitation_dialog", "hooks": [
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
    parser = argparse.ArgumentParser(prog="agent-hotline", description="Let your AI agent call you when it needs help")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start server (foreground)")

    sp = sub.add_parser("start", help="Start server (background)")
    sp.add_argument("--log", default=str(_DEFAULT_LOG), help=f"Log file path, '-' for none (default: {_DEFAULT_LOG})")

    sub.add_parser("stop", help="Stop background server")

    rp = sub.add_parser("restart", help="Restart background server")
    rp.add_argument("--log", default=str(_DEFAULT_LOG), help=f"Log file path, '-' for none (default: {_DEFAULT_LOG})")

    sub.add_parser("status", help="Check if server is running")

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
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
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
