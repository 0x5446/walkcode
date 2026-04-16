"""WalkCode CLI."""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path

from .i18n import t
from .tty import detect_tmux_session

_RUNTIME_DIR = Path.home() / ".walkcode"


def _quick_load_env(keys: set[str]):
    """Load specific keys from the env file into os.environ (if not already set)."""
    env_file_override = os.environ.get("WALKCODE_ENV_FILE")
    path = Path(env_file_override).expanduser() if env_file_override else _RUNTIME_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k in keys and k not in os.environ:
            os.environ[k] = v


def _instance_name() -> str:
    """Return effective instance name for PID/log file naming."""
    _quick_load_env({"WALKCODE_AGENT", "WALKCODE_INSTANCE"})
    agent = os.environ.get("WALKCODE_AGENT", "claude")
    instance = os.environ.get("WALKCODE_INSTANCE", "")
    effective = instance or agent
    # backward compat: default claude instance uses "walkcode" prefix
    if agent == "claude" and not instance:
        return "walkcode"
    return effective


def _pid_file() -> Path:
    return _RUNTIME_DIR / f"{_instance_name()}.pid"


def _log_file() -> Path:
    return _RUNTIME_DIR / f"{_instance_name()}.log"


def _preflight_check():
    """Verify required external tools are available before starting."""
    import shutil
    from .agent import get_agent
    if not shutil.which("tmux"):
        print(t("preflight.tmux_not_found"), file=sys.stderr)
        sys.exit(1)
    agent_name = os.environ.get("WALKCODE_AGENT", "claude")
    agent = get_agent(agent_name)
    if not shutil.which(agent.command):
        print(t("preflight.agent_not_found", agent=agent.command), file=sys.stderr)


def cmd_serve(_args):
    import uvicorn
    from .config import Config
    from .server import app, init, start_ws_client

    _preflight_check()
    # Auto-cleanup images older than 180 days
    try:
        cleaned = _clean_images(180)
        if cleaned:
            print(f"Auto-cleaned {cleaned} image(s) older than 180 days")
    except Exception:
        pass
    cfg = Config.load()
    init(cfg)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    start_ws_client(cfg)
    print(t("serve.listening", port=cfg.port))
    if cfg.feishu_receive_id:
        print(t("serve.feishu_target", id_type=cfg.feishu_receive_id_type, receive_id=cfg.feishu_receive_id))
    else:
        print(t("serve.no_receive_id"))
    print(t("serve.hook_url", port=cfg.port))
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_level="warning")


# --- Daemon management ---

def _read_pid() -> int | None:
    pf = _pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)  # check alive
        return pid
    except (ValueError, OSError):
        pf.unlink(missing_ok=True)
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
        print(t("start.already_running", pid=pid))
        sys.exit(1)

    log_path = Path(args.log) if args.log != "-" else None
    _RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    # Build command: use the same Python + module to run serve
    cmd = [sys.executable, "-m", "walkcode", "serve"]

    # Propagate env file and agent config to child process
    env = os.environ.copy()

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
        env=env,
    )

    _pid_file().write_text(str(proc.pid))
    if log_path:
        print(t("start.started_with_log", pid=proc.pid, log=log_path))
    else:
        print(t("start.started", pid=proc.pid))


def cmd_stop(_args):
    pid = _read_pid()
    if not pid:
        print(t("not_running"))
        sys.exit(1)

    os.kill(pid, signal.SIGTERM)
    pf = _pid_file()
    if _wait_exit(pid):
        pf.unlink(missing_ok=True)
        print(t("stop.stopped", pid=pid))
    else:
        os.kill(pid, signal.SIGKILL)
        pf.unlink(missing_ok=True)
        print(t("stop.killed", pid=pid))


def cmd_restart(args):
    pid = _read_pid()
    if pid:
        os.kill(pid, signal.SIGTERM)
        if not _wait_exit(pid):
            os.kill(pid, signal.SIGKILL)
        _pid_file().unlink(missing_ok=True)
        print(t("stop.stopped", pid=pid))

    cmd_start(args)


def cmd_status(_args):
    pid = _read_pid()
    if pid:
        print(t("status.running", pid=pid))
    else:
        print(t("not_running"))
        sys.exit(1)


def cmd_hook(args):
    """Handle an agent hook event: read stdin, POST to server."""
    # Read hook data from stdin (agent pipes JSON)
    try:
        hook_data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        hook_data = {}

    tmux_session = detect_tmux_session()
    if not tmux_session:
        print(t("hook.not_in_tmux"), file=sys.stderr)
        return

    cwd = hook_data.get("cwd", "") or os.getcwd()
    session_id = hook_data.get("session_id", "")
    port = int(os.environ.get("WALKCODE_PORT", os.environ.get("PORT", "3001")))

    # DEBUG: dump full hook_data to file for analysis
    try:
        import datetime as _dt
        _dbg_path = Path.home() / ".walkcode" / "hook_debug.jsonl"
        with open(_dbg_path, "a") as _f:
            _f.write(json.dumps({
                "ts": _dt.datetime.now().isoformat(),
                "hook_type": args.hook_type,
                "tmux": tmux_session,
                "hook_data_keys": sorted(hook_data.keys()),
                "hook_data": hook_data,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # --- Sync: lightweight tty mapping update (SessionStart) ---
    if args.hook_type == "sync":
        payload = json.dumps({
            "tty": tmux_session,
            "cwd": cwd,
            "session_id": session_id,
        }).encode()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/hook/sync",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass  # best-effort, don't block startup
        return

    # --- PermissionRequest / PreToolUse: send card, poll for decision ---
    if args.hook_type == "permission-request":
        import time as _time
        from .agent import get_agent

        # Codex PreToolUse fires for ALL tools, even auto-approved ones.
        # When permission_mode is "bypassPermissions", just let it proceed.
        if hook_data.get("permission_mode") == "bypassPermissions":
            sys.exit(0)

        tool_name = hook_data.get("tool_name", "")
        tool_input = hook_data.get("tool_input", {})

        payload = json.dumps({
            "tty": tmux_session,
            "cwd": cwd,
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "hook_data_full": hook_data,
        }).encode()

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/hook/permission",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            request_id = result.get("request_id", "")
        except Exception as e:
            print(f"[walkcode] permission hook failed: {e}", file=sys.stderr)
            sys.exit(2)  # deny on failure

        if not request_id:
            sys.exit(2)

        # Poll for decision (long-poll, up to 120s total)
        decision_url = f"http://127.0.0.1:{port}/hook/permission/{request_id}/decision"
        deadline = _time.monotonic() + 120

        agent_name = os.environ.get("WALKCODE_AGENT", "claude")
        agent = get_agent(agent_name)

        while _time.monotonic() < deadline:
            try:
                req = urllib.request.Request(decision_url)
                resp = urllib.request.urlopen(req, timeout=35)
                result = json.loads(resp.read())

                if result.get("status") == "decided":
                    decision = result["decision"]
                    behavior = decision.get("behavior", "deny")

                    # AskUserQuestion: exit without hook response so agent
                    # falls back to the interactive terminal prompt.
                    # The server injects the answer via tmux after we exit.
                    if tool_name == "AskUserQuestion":
                        sys.exit(2)

                    hook_response = agent.build_hook_response(
                        behavior=behavior,
                        updated_permissions=decision.get("updatedPermissions"),
                    )

                    print(json.dumps(hook_response), flush=True)
                    sys.exit(0 if behavior == "allow" else 2)

                # status == "pending" or "not_found", keep polling
            except Exception as e:
                print(f"[walkcode] poll error: {e}", file=sys.stderr)
            _time.sleep(2)

        # Timeout: deny
        sys.exit(2)

    # --- Notification ---
    if args.hook_type == "notification":
        message = hook_data.get("message", "")
        title = hook_data.get("title", "")
        matcher = hook_data.get("notification_type", "") or os.environ.get("CLAUDE_NOTIFICATION_TYPE", "")
    else:
        # Stop hook: last_assistant_message
        message = hook_data.get("last_assistant_message", "")
        title = ""
        matcher = ""
        _SKIP = {"no response requested.", ""}
        if message.strip().lower() in _SKIP:
            message = ""

    payload = json.dumps({
        "type": args.hook_type,
        "tty": tmux_session,
        "cwd": cwd,
        "session_id": session_id,
        "message": message,
        "title": title,
        "matcher": matcher,
    }).encode()

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/hook",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(t("hook.failed", error=e), file=sys.stderr)


# --- Hook installation ---

def _install_claude_hooks(_args):
    """Install hooks into Claude Code settings.json."""
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        print(t("install_hooks.not_found", path=settings_path))
        sys.exit(1)

    settings = json.loads(settings_path.read_text())

    def hook_cmd(hook_type: str, sound: str) -> str:
        return f"afplay /System/Library/Sounds/{sound}.aiff & walkcode hook {hook_type}"

    settings["hooks"] = {
        "SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "walkcode hook sync"}
        ]}],
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": hook_cmd("stop", "Hero")}
        ]}],
        "Notification": [{"matcher": "elicitation_dialog", "hooks": [
            {"type": "command", "command": hook_cmd("notification", "Ping")}
        ]}],
        "PermissionRequest": [{"matcher": "", "hooks": [
            {"type": "command", "command": "afplay /System/Library/Sounds/Ping.aiff & walkcode hook permission-request", "timeout": 120000}
        ]}],
    }

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(t("install_hooks.done", path=settings_path))
    print(t("install_hooks.restart_hint"))


def _install_codex_hooks(_args):
    """Install hooks into Codex CLI hooks.json and enable feature flag."""
    _quick_load_env({"WALKCODE_PORT", "PORT"})
    port = os.environ.get("WALKCODE_PORT", os.environ.get("PORT", "3001"))
    port_env = f"WALKCODE_PORT={port} " if port != "3001" else ""

    def hook_cmd(hook_type: str, sound: str) -> str:
        return f"afplay /System/Library/Sounds/{sound}.aiff & {port_env}walkcode hook {hook_type}"

    hooks_config = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"{port_env}walkcode hook sync", "timeout": 5}
            ]}],
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": hook_cmd("stop", "Hero")}
            ]}],
            "PreToolUse": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"afplay /System/Library/Sounds/Ping.aiff & {port_env}walkcode hook permission-request", "timeout": 120}
            ]}],
        }
    }

    hooks_path = Path.home() / ".codex" / "hooks.json"
    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(hooks_config, indent=2) + "\n")

    # Enable feature flag in config.toml
    config_toml = Path.home() / ".codex" / "config.toml"
    _ensure_codex_hooks_feature(config_toml)

    print(t("install_hooks.done", path=hooks_path))
    print(t("install_hooks.restart_hint"))


def _ensure_codex_hooks_feature(config_toml: Path):
    """Ensure [features] codex_hooks = true in Codex config.toml."""
    if config_toml.exists():
        content = config_toml.read_text()
        if "codex_hooks" in content:
            return
        if "[features]" in content:
            content = content.replace("[features]", "[features]\ncodex_hooks = true")
        else:
            content += "\n[features]\ncodex_hooks = true\n"
        config_toml.write_text(content)
    else:
        config_toml.parent.mkdir(parents=True, exist_ok=True)
        config_toml.write_text("[features]\ncodex_hooks = true\n")


def cmd_install_hooks(args):
    agent_name = getattr(args, "agent", None) or os.environ.get("WALKCODE_AGENT", "claude")
    if agent_name == "codex":
        _install_codex_hooks(args)
    else:
        _install_claude_hooks(args)


_GITHUB_REPO = "0x5446/walkcode"
_GITHUB_URL = f"https://github.com/{_GITHUB_REPO}.git"


def _run(cmd, **kwargs):
    print(f"  → {cmd}")
    result = subprocess.run(cmd, shell=True, **kwargs)
    if result.returncode != 0:
        print(t("run.failed", code=result.returncode))
        sys.exit(1)


def _get_latest_tag() -> str | None:
    """Fetch the latest release tag from GitHub API."""
    url = f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return data.get("tag_name")
    except Exception:
        return None


def _current_version() -> str:
    """Read version from installed package metadata."""
    try:
        from importlib.metadata import version
        return version("walkcode")
    except Exception:
        return "unknown"


def cmd_upgrade(_args):
    """Upgrade WalkCode to the latest release via uv tool install."""
    current = _current_version()
    print(t("upgrade.current", version=current))

    tag = _get_latest_tag()
    if tag:
        print(t("upgrade.latest", tag=tag))
        source = f"git+{_GITHUB_URL}@{tag}"
    else:
        print(t("upgrade.no_release"))
        source = f"git+{_GITHUB_URL}"

    _run(f"uv tool install {source} --force")

    cmd_install_hooks(argparse.Namespace(agent=None))

    pid = _read_pid()
    if pid:
        print(t("upgrade.restarting"))
        os.kill(pid, signal.SIGTERM)
        pf = _pid_file()
        if _wait_exit(pid):
            pf.unlink(missing_ok=True)
        else:
            os.kill(pid, signal.SIGKILL)
            pf.unlink(missing_ok=True)
        # Start with default log
        cmd_start(argparse.Namespace(log=str(_log_file())))
    else:
        print(t("upgrade.not_running"))

    print(t("upgrade.complete"))


def cmd_uninstall(_args):
    """Uninstall WalkCode: stop daemon, remove CLI, clean up config."""
    # 1. Stop daemon if running
    pid = _read_pid()
    if pid:
        print(t("uninstall.stopping", pid=pid))
        os.kill(pid, signal.SIGTERM)
        pf = _pid_file()
        if _wait_exit(pid):
            pf.unlink(missing_ok=True)
        else:
            os.kill(pid, signal.SIGKILL)
            pf.unlink(missing_ok=True)
        print(t("uninstall.stopped"))

    # 2. Remove uv tool
    print(t("uninstall.removing_cli"))
    subprocess.run(["uv", "tool", "uninstall", "walkcode"], capture_output=True)
    print(t("uninstall.done"))

    # 3. Remove shell wrapper from rc files
    for rc in [Path.home() / ".zshrc", Path.home() / ".bashrc", Path.home() / ".profile"]:
        if not rc.exists():
            continue
        content = rc.read_text()
        start = "# >>> walkcode claude wrapper >>>"
        end = "# <<< walkcode claude wrapper <<<"
        if start in content:
            lines = content.split("\n")
            new_lines = []
            skip = False
            for line in lines:
                if start in line:
                    if new_lines and new_lines[-1].strip() == "":
                        new_lines.pop()
                    skip = True
                    continue
                if end in line:
                    skip = False
                    continue
                if not skip:
                    new_lines.append(line)
            rc.write_text("\n".join(new_lines))
            print(t("uninstall.removed_wrapper", path=rc))

    # 4. Remove tmux config
    tmux_conf = Path.home() / ".tmux.conf"
    if tmux_conf.exists():
        content = tmux_conf.read_text()
        start = "# >>> walkcode tmux config >>>"
        end = "# <<< walkcode tmux config <<<"
        if start in content:
            lines = content.split("\n")
            new_lines = []
            skip = False
            for line in lines:
                if start in line:
                    if new_lines and new_lines[-1].strip() == "":
                        new_lines.pop()
                    skip = True
                    continue
                if end in line:
                    skip = False
                    continue
                if not skip:
                    new_lines.append(line)
            tmux_conf.write_text("\n".join(new_lines))
            print(t("uninstall.removed_tmux", path=tmux_conf))

    # 5. Remove config directory
    if _RUNTIME_DIR.exists():
        print(t("uninstall.config_dir", path=_RUNTIME_DIR))
        print(t("uninstall.config_contents"))
        try:
            answer = input(t("uninstall.remove_prompt")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "y":
            import shutil
            shutil.rmtree(_RUNTIME_DIR)
            print(t("uninstall.removed_dir", path=_RUNTIME_DIR))
        else:
            print(t("uninstall.kept_dir", path=_RUNTIME_DIR))

    print(t("uninstall.complete"))


_IMAGE_DIR = _RUNTIME_DIR / "images"
_AGE_MAP = {"1d": 1, "1w": 7, "1m": 30, "180d": 180}


def _clean_images(max_age_days: int) -> int:
    """Remove images older than max_age_days. Returns count of deleted files."""
    if not _IMAGE_DIR.exists():
        return 0
    import datetime
    cutoff = datetime.datetime.now().timestamp() - max_age_days * 86400
    count = 0
    for f in _IMAGE_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    return count


def cmd_clean_images(args):
    age = args.age
    days = _AGE_MAP.get(age)
    if days is None:
        print(f"Invalid age: {age}. Use one of: {', '.join(_AGE_MAP.keys())}")
        sys.exit(1)
    count = _clean_images(days)
    if count:
        print(t("clean_images.cleaned", count=count, age=age))
    else:
        print(t("clean_images.none"))


def cmd_test_inject(args):
    from .tty import inject, validate_target

    error = validate_target(args.session)
    if error:
        print(t("test_inject.error", error=error))
        sys.exit(1)

    inject(args.session, args.text, enter=not args.no_enter)
    suffix = " (no enter)" if args.no_enter else " + Enter"
    print(t("test_inject.done", text=args.text, suffix=suffix, session=args.session))


def main():
    parser = argparse.ArgumentParser(prog="walkcode", description="Let your AI agent call you when it needs help")
    parser.add_argument("-v", "--version", action="version", version=f"walkcode {_current_version()}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="Start server (foreground)")

    sp = sub.add_parser("start", help="Start server (background)")
    sp.add_argument("--log", default=str(_log_file()), help=f"Log file path, '-' for none (default: {_log_file()})")

    sub.add_parser("stop", help="Stop background server")

    rp = sub.add_parser("restart", help="Restart background server")
    rp.add_argument("--log", default=str(_log_file()), help=f"Log file path, '-' for none (default: {_log_file()})")

    sub.add_parser("status", help="Check if server is running")

    hp = sub.add_parser("hook", help="Handle an agent hook event (reads stdin)")
    hp.add_argument("hook_type", choices=["stop", "notification", "permission-request", "sync"], help="Hook event type")

    ihp = sub.add_parser("install-hooks", help="Install agent hooks")
    ihp.add_argument("--agent", choices=["claude", "codex"], default=None, help="Agent type (default: from WALKCODE_AGENT or claude)")

    sub.add_parser("upgrade", help="Upgrade to latest release")
    sub.add_parser("uninstall", help="Uninstall WalkCode completely")

    cp = sub.add_parser("clean-images", help="Clean downloaded Feishu images")
    cp.add_argument("age", choices=list(_AGE_MAP.keys()), help="Delete images older than: 1d, 1w, 1m, 180d")

    p = sub.add_parser("test-inject", help="Test tmux injection")
    p.add_argument("session", help="tmux session name")
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
        "upgrade": cmd_upgrade,
        "uninstall": cmd_uninstall,
        "clean-images": cmd_clean_images,
        "test-inject": cmd_test_inject,
    }
    fn = cmds.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
