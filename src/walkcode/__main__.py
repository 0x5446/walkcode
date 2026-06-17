"""WalkCode CLI."""

import argparse
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path

from .i18n import t
from .tty import detect_tmux_session, owner_check

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


def _handle_permission_request(hook_data, port, tmux_session, cwd, session_id):
    """Permission request body. Raises SystemExit on valid decision paths;
    any other exception is caught by caller and treated as fail-open."""
    import time as _time
    from .agent import get_agent

    tool_name = hook_data.get("tool_name", "")
    tool_input = hook_data.get("tool_input", {})

    # Codex PreToolUse fires for ALL tools, even auto-approved ones.
    # When permission_mode is "bypassPermissions", just let ordinary tools
    # proceed. AskUserQuestion is HITL, though: if we short-circuit it here,
    # Claude still opens its native TUI dialog while Feishu never sees a card.
    if hook_data.get("permission_mode") == "bypassPermissions" and tool_name != "AskUserQuestion":
        sys.exit(0)

    payload = json.dumps({
        "tty": tmux_session,
        "cwd": cwd,
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        # tool_use_id dedupes codex 0.135's double-fired PreToolUse (it identifies
        # a single tool call; turn_id would wrongly merge a whole turn's requests).
        "tool_use_id": hook_data.get("tool_use_id", ""),
        "turn_id": hook_data.get("turn_id", ""),
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
        # Fail-open: server unreachable or malformed response.
        # Let the agent fall back to its own native permission prompt.
        print(f"[walkcode] permission hook failed, fail-open: {e}", file=sys.stderr)
        sys.exit(0)

    if not request_id:
        print("[walkcode] no request_id from server, fail-open", file=sys.stderr)
        sys.exit(0)

    # Poll for decision (long-poll, up to 30 minutes total). Long ceiling
    # accommodates AskUserQuestion's Other / multiSelect flow where the user
    # may take many minutes typing custom text or selecting multiple options
    # in Feishu before clicking Submit. Must stay <= the matching settings.json
    # hook timeout (1_800_000 ms) — Claude Code kills the hook process at that
    # external limit regardless of internal deadline.
    decision_url = f"http://127.0.0.1:{port}/hook/permission/{request_id}/decision"
    deadline = _time.monotonic() + 1800

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

                # AskUserQuestion: inject answers via PermissionRequest
                # `updatedInput.answers` so Claude consumes them directly and
                # skips its native TUI prompt entirely. The server has already
                # collected answers from Feishu and packaged them in the
                # decision payload as {questions, answers} ready to echo back.
                updated_input = None
                if tool_name == "AskUserQuestion":
                    updated_input = decision.get("updatedInput")
                    if updated_input is None:
                        # Server somehow didn't supply updated_input — fail-open
                        # so the agent falls back to its native prompt.
                        print("[walkcode] AskUserQuestion decision missing updatedInput, fail-open", file=sys.stderr)
                        sys.exit(0)

                hook_response = agent.build_hook_response(
                    behavior=behavior,
                    updated_permissions=decision.get("updatedPermissions"),
                    updated_input=updated_input,
                )

                if hook_response is not None:
                    print(json.dumps(hook_response), flush=True)
                sys.exit(agent.hook_exit_code(behavior))

            if result.get("status") == "invalidated":
                # The TUI already handled this (PostToolUse invalidated the card) →
                # fail-open: the terminal decision stands, stop polling.
                print("[walkcode] permission handled in terminal, fail-open", file=sys.stderr)
                sys.exit(0)

            # status == "pending" or "not_found", keep polling
        except Exception as e:
            print(f"[walkcode] poll error: {e}", file=sys.stderr)
        _time.sleep(2)

    # Timeout: fail-open so agent falls back to its native prompt.
    # A 2-min Feishu silence is not the same as an explicit deny.
    print("[walkcode] permission poll timed out after 1800s, fail-open", file=sys.stderr)
    sys.exit(0)


def _read_last_assistant_text(path: str, max_chars: int = 28000) -> str:
    """Tail transcript JSONL; return most recent assistant message's text content.

    Used as a fallback when Stop hook's `last_assistant_message` is empty
    (e.g. the final assistant message was a pure tool_use with no text block).
    """
    if not path:
        return ""
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        text = "".join(parts).strip()
        if text:
            return text if len(text) <= max_chars else text[:max_chars] + "\n…(truncated)"
    return ""


def _record_owner_event(hook_type: str, tmux: str, session_id: str, reason: str) -> None:
    """Best-effort append of an owner-gate decision to ~/.walkcode/hook_debug.jsonl.

    Called when a hook is dropped (non_owner) or delivered via fail-open — both are
    cases the server never sees, so without this trace they are invisible to
    post-hoc diagnosis (the exact gap that made the v0.10.31 regression hard to
    find). Creates the directory if missing; never raises.
    """
    try:
        import datetime as _dt
        _dbg_path = Path.home() / ".walkcode" / "hook_debug.jsonl"
        _dbg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(_dbg_path, "a") as _f:
            _f.write(json.dumps({
                "ts": _dt.datetime.now().isoformat(),
                "hook_type": hook_type,
                "tmux": tmux,
                "session_id": session_id,
                "owner_reason": reason,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


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

    # Ownership gate. A nested child agent (e.g. a deep-review sub-agent in the
    # parent's background terminal) inherits $TMUX and would otherwise fire hooks
    # reporting the PARENT's tmux — hijacking the parent's Feishu thread, orphaning
    # it, and double-resuming the parent. A non-owner's hooks must never touch tty
    # ownership, so we drop them here at the source (see tty.is_tmux_pane_owner).
    owner, owner_reason = owner_check()
    if not owner:
        # permission-request is the one type that needs a resolution or the agent's
        # tool call hangs: exit 0 without emitting a decision so the child falls
        # back to its OWN sandbox/approval rather than walkcode's. Everything else
        # (sync/stop/notification/user-prompt-submit) is simply not reported — the
        # child has no Feishu thread of its own.
        kind = "permission" if args.hook_type == "permission-request" else args.hook_type
        print(f"[walkcode] non-owner {kind} hook (nested child), not reported", file=sys.stderr)
        # Record the drop. A dropped hook reports nothing to the server, so without
        # this the only trace is the child agent's transcript — which is exactly why
        # the v0.10.31 false-positive (claude detached hooks wrongly dropped) was so
        # hard to diagnose.
        _record_owner_event(args.hook_type, tmux_session, hook_data.get("session_id", ""), owner_reason)
        return
    if owner_reason.startswith("failopen"):
        # Delivered, but the gate could NOT confirm ownership (the pane/ancestry
        # probe was inconclusive). Leave a trace so a later false-drop or hijack via
        # this fail-open path is diagnosable instead of silent.
        _record_owner_event(args.hook_type, tmux_session, hook_data.get("session_id", ""), owner_reason)

    cwd = hook_data.get("cwd", "") or os.getcwd()
    session_id = hook_data.get("session_id", "")
    port = int(os.environ.get("WALKCODE_PORT", os.environ.get("PORT", "3001")))

    # --- UserPromptSubmit: confirm a prompt was actually accepted (fast path) ---
    # Runs on the critical path of EVERY prompt submission, so keep it cheap:
    # short timeout, no stdout (hook stdout is injected into the prompt), exit 0.
    # Returns before the debug dump to avoid per-prompt disk writes.
    if args.hook_type == "user-prompt-submit":
        payload = json.dumps({
            "tty": tmux_session,
            "cwd": cwd,
            "session_id": session_id,
            "prompt": hook_data.get("prompt", ""),
        }).encode()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/hook/prompt",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=1)
        except Exception:
            pass  # best-effort; never block prompt submission
        return

    # --- PostToolUse: a tool finished executing → tell the server to invalidate this
    # session's still-open permission cards (its TUI already settled them). Fires per
    # tool call, so keep it cheap and skip the debug dump, like user-prompt-submit. ---
    if args.hook_type == "post-tool":
        payload = json.dumps({
            "tty": tmux_session,
            "cwd": cwd,
            "session_id": session_id,
        }).encode()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/hook/post-tool",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=2)
        except Exception as e:
            # best-effort; never block the tool result. Log so a failed invalidation
            # (card won't grey out → click falls back to the server's last_poll gate)
            # is diagnosable instead of silent.
            print(f"[walkcode] post-tool hook failed (card invalidation skipped): {e}", file=sys.stderr)
        return

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
        try:
            _handle_permission_request(hook_data, port, tmux_session, cwd, session_id)
        except SystemExit:
            raise
        except BaseException as e:
            print(f"[walkcode] permission hook crashed (fail-open): {e}", file=sys.stderr)
            sys.exit(0)
        return

    # --- Notification ---
    if args.hook_type == "notification":
        message = hook_data.get("message", "")
        title = hook_data.get("title", "")
        matcher = hook_data.get("notification_type", "") or os.environ.get("CLAUDE_NOTIFICATION_TYPE", "")
    else:
        # Stop hook: prefer last_assistant_message; fallback to transcript_path
        # when the final message is a pure tool_use block (no text).
        message = hook_data.get("last_assistant_message", "")
        title = ""
        matcher = ""
        _SKIP = {"no response requested.", ""}
        if message.strip().lower() in _SKIP:
            message = _read_last_assistant_text(hook_data.get("transcript_path", ""))
            if message.strip().lower() in _SKIP:
                message = ""

    payload = json.dumps({
        "type": args.hook_type,
        "tty": tmux_session,
        "cwd": cwd,
        "session_id": session_id,
        # turn_id lets the server dedupe per-turn: codex (>=0.135) fires each hook
        # event twice with an identical payload. Claude carries no turn_id.
        "turn_id": hook_data.get("turn_id", ""),
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

def _shell_env_prefix(values: list[tuple[str, str]]) -> str:
    parts = [f"{key}={shlex.quote(value)}" for key, value in values if value]
    return (" ".join(parts) + " ") if parts else ""


def _read_env_file_values(path: str | None = None) -> dict[str, str]:
    env_path = Path(path).expanduser() if path else _RUNTIME_DIR / ".env"
    if not env_path.exists():
        return {}
    values = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key and value:
            values[key.strip()] = value.strip()
    return values


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
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": "walkcode hook user-prompt-submit"}
        ]}],
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": hook_cmd("stop", "Hero")}
        ]}],
        "Notification": [{"matcher": "elicitation_dialog", "hooks": [
            {"type": "command", "command": hook_cmd("notification", "Ping")}
        ]}],
        "PermissionRequest": [{"matcher": "", "hooks": [
            {"type": "command", "command": "afplay /System/Library/Sounds/Ping.aiff & walkcode hook permission-request", "timeout": 1800000}
        ]}],
        # PostToolUse: a tool actually ran → invalidate this session's stale Feishu
        # permission cards (the TUI already settled them). No sound/timeout needed.
        "PostToolUse": [{"matcher": "", "hooks": [
            {"type": "command", "command": "walkcode hook post-tool"}
        ]}],
    }

    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    print(t("install_hooks.done", path=settings_path))
    print(t("install_hooks.restart_hint"))


def _install_codex_hooks(_args):
    """Install hooks into Codex CLI hooks.json and enable feature flag."""
    env_file = os.environ.get("WALKCODE_ENV_FILE")
    file_values = _read_env_file_values(env_file)
    port = (
        os.environ.get("WALKCODE_PORT")
        or file_values.get("WALKCODE_PORT")
        or file_values.get("PORT")
        or os.environ.get("PORT", "3001")
    )
    env_values = []
    if env_file:
        env_values.append(("WALKCODE_ENV_FILE", str(Path(env_file).expanduser())))
    env_values.extend([
        ("WALKCODE_AGENT", "codex"),
        ("WALKCODE_PORT", port),
    ])
    instance = os.environ.get("WALKCODE_INSTANCE") or file_values.get("WALKCODE_INSTANCE")
    if instance:
        env_values.append(("WALKCODE_INSTANCE", instance))
    env_prefix = _shell_env_prefix(env_values)

    def hook_cmd(hook_type: str, sound: str) -> str:
        return f"afplay /System/Library/Sounds/{sound}.aiff & {env_prefix}walkcode hook {hook_type}"

    hooks_config = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"{env_prefix}walkcode hook sync", "timeout": 5}
            ]}],
            "Stop": [{"matcher": "", "hooks": [
                {"type": "command", "command": hook_cmd("stop", "Hero")}
            ]}],
            "PreToolUse": [{"matcher": "", "hooks": [
                {"type": "command", "command": f"afplay /System/Library/Sounds/Ping.aiff & {env_prefix}walkcode hook permission-request", "timeout": 1800}
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


# Match the [features] table header tolerantly: leading whitespace, spaces inside
# the brackets ([ features ]), and a trailing comment ([features] # x) are all
# valid TOML that a literal "[features]" match would miss.
_FEATURES_HEADER = re.compile(r"^\s*\[\s*features\s*\]\s*(?:#.*)?$")
_TABLE_HEADER = re.compile(r"^\s*\[[^\[\]]*\]\s*(?:#.*)?$")
_HOOKS_ASSIGN = re.compile(r"^\s*hooks\s*=")


def _ensure_codex_hooks_feature(config_toml: Path):
    """Ensure [features] hooks = true in Codex config.toml.

    Codex 0.135+ gates hooks behind ``hooks = true`` (older builds used
    ``codex_hooks``, left untouched). tomllib decides whether it's already on;
    _set_features_hooks_true does the edit, tolerant of spaced/commented headers.
    If the original parsed cleanly we re-validate the result and refuse to write a
    file we'd corrupt.
    """
    if not config_toml.exists():
        config_toml.parent.mkdir(parents=True, exist_ok=True)
        config_toml.write_text("[features]\nhooks = true\n")
        return

    content = config_toml.read_text()
    try:
        parsed = tomllib.loads(content)
        orig_ok = True
    except Exception:
        parsed, orig_ok = None, False  # corrupt TOML → best-effort edit

    features = parsed.get("features", {}) if isinstance(parsed, dict) else {}
    if features.get("hooks") is True:
        return  # already enabled

    new_content = _set_features_hooks_true(content)

    if orig_ok:
        # Never turn a previously-valid config into a broken one.
        try:
            check = tomllib.loads(new_content)
        except Exception:
            check = None
        if not isinstance(check, dict) or check.get("features", {}).get("hooks") is not True:
            print(
                f"[walkcode] skipped enabling codex hooks flag: editing {config_toml} "
                "would not yield a valid [features] hooks = true; please set it manually",
                file=sys.stderr,
            )
            return
    config_toml.write_text(new_content)


def _set_features_hooks_true(content: str) -> str:
    """Return content with [features] hooks = true.

    Tolerant of spaced/commented headers. Replaces an existing hooks line inside
    [features]; if [features] exists without one, inserts right after the header;
    if there's no [features] table, appends one. Other tables (e.g. [hooks.state])
    are never touched.
    """
    out: list[str] = []
    in_features = False
    replaced = False
    header_pos = None  # index in `out` of the [features] header line
    for line in content.splitlines(keepends=True):
        if _FEATURES_HEADER.match(line):
            in_features = True
            header_pos = len(out)
            out.append(line)
            continue
        if _TABLE_HEADER.match(line):
            # entering another table; if we were in [features] without a hooks
            # line, insert one right after its header before leaving.
            if in_features and not replaced and header_pos is not None:
                out.insert(header_pos + 1, "hooks = true\n")
                replaced = True
            in_features = False
            out.append(line)
            continue
        if in_features and not replaced and _HOOKS_ASSIGN.match(line):
            out.append("hooks = true\n" if line.endswith("\n") else "hooks = true")
            replaced = True
            continue
        out.append(line)
    if not replaced:
        if header_pos is not None:
            out.insert(header_pos + 1, "hooks = true\n")
        else:
            sep = "" if (not out or out[-1].endswith("\n")) else "\n"
            out.append(sep + "[features]\nhooks = true\n")
    return "".join(out)


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

    # Re-run install-hooks via the freshly installed binary — NOT cmd_install_hooks()
    # in-process. This interpreter is still running the OLD code, so an in-process
    # call writes the *previous* version's hook set, silently dropping any hook the
    # new version added (this is exactly how v0.10.17's UserPromptSubmit hook went
    # missing after upgrade). The `walkcode` shim resolves to the just-installed
    # version; WALKCODE_AGENT (inherited) keeps the right agent's hooks.
    agent_name = os.environ.get("WALKCODE_AGENT", "claude")
    _run(f"walkcode install-hooks --agent {shlex.quote(agent_name)}")

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
    hp.add_argument("hook_type", choices=["stop", "notification", "permission-request", "sync", "user-prompt-submit", "post-tool"], help="Hook event type")

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
