"""Cross-process PreToolUse decision channel ("gate") for Claude TUI/daemon sessions.

Design: ``docs/design/claude-daemon-multi-ui-sync.md`` / ADR 0046. The headless
transport closes the permission / AskUserQuestion loop in-process: the SDK's
``can_use_tool`` callback floats a card event, awaits a Future, and maps the
human decision back to a ``PermissionResult``. TUI/daemon sessions run the
PreToolUse hook in a *separate process*, so the Future becomes a file
rendezvous under the instance's TUI hook spool:

    <state>.tui-hooks.d/gate/pending/<rid>.json     hook -> runtime request
    <state>.tui-hooks.d/gate/decisions/<rid>.json   runtime -> hook (write-once)
    <state>.tui-hooks.d/gate/serve.heartbeat        runtime drain liveness

Decision mapping mirrors ``_ClaudePermissionBridge._result_from_decision``:

    allow / always_allow      -> {"permissionDecision": "allow"}
    deny                      -> {"permissionDecision": "deny", reason}
    answers (AskUserQuestion) -> {"permissionDecision": "allow",
                                  "updatedInput": {questions, answers}}
    pass                      -> no output (native permission flow takes over)

Fail-safe posture matches the headless bridge: no decision inside the wait
budget -> deny. But when the runtime is not draining (stale heartbeat) the
hook *abstains* instead of denying blind, so a TUI without its walkcode
service keeps the native terminal prompt flow.

This module must stay stdlib-only: the blocking hook path has to be
import-light, and both ``channel_native/__init__`` and ``claude_daemon``
import it (a dependency back into the package would cycle).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

GATE_DIR_NAME = "gate"
PENDING_DIR_NAME = "pending"
DECISIONS_DIR_NAME = "decisions"
HEARTBEAT_FILE_NAME = "serve.heartbeat"

# Wait budget mirrors the headless bridge default (1800s). The hook entry in
# the profile settings must configure a *larger* Claude-side hook timeout
# (e.g. 1830) or Claude kills the hook first and the gate silently degrades
# to the native prompt.
DEFAULT_WAIT_TIMEOUT_SECONDS = 1800.0
DECISION_POLL_INTERVAL_SECONDS = 0.25
# Runtime drain touches the heartbeat every drain tick (~1s); anything this
# stale means no walkcode service is consuming the spool.
HEARTBEAT_FRESH_SECONDS = 45.0

ASK_USER_TOOL_NAMES = frozenset({"AskUserQuestion", "ask_user_question"})

# Tools that reliably trigger a native permission prompt when no allow rule
# covers them. Everything outside this set (internal tools, read-only tools)
# stays on the native flow so the gate never adds approvals that the terminal
# would not have asked for.
DEFAULT_GATE_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

KIND_PERMISSION = "permission"
KIND_ASK_USER = "ask_user_question"


def gate_root(state_path: Path | str) -> Path:
    state_path = Path(state_path)
    return state_path.parent / f"{state_path.name}.tui-hooks.d" / GATE_DIR_NAME


def pending_dir(state_path: Path | str) -> Path:
    return gate_root(state_path) / PENDING_DIR_NAME


def decisions_dir(state_path: Path | str) -> Path:
    return gate_root(state_path) / DECISIONS_DIR_NAME


def heartbeat_path(state_path: Path | str) -> Path:
    return gate_root(state_path) / HEARTBEAT_FILE_NAME


def trace(event: str, **fields: Any) -> None:
    """One-line stderr trace for gate decision paths.

    The gate is a cross-process rendezvous: every degrade branch (abstain,
    timeout deny, pass) changes user-visible behavior, and without a trace the
    failure mode is undiagnosable after the fact (review finding).
    """
    parts = [f"walkcode-gate {event}"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    print(" ".join(parts), file=sys.stderr, flush=True)


def _safe_rid_filename(rid: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(rid or ""))[:120]
    return cleaned or "unknown"


def _ensure_private_dir(path: Path) -> None:
    """Create spool dirs owner-only: pendings carry tool inputs and answers."""
    path.mkdir(parents=True, exist_ok=True)
    for directory in (path, path.parent):
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)


def pending_path(state_path: Path | str, rid: str) -> Path:
    return pending_dir(state_path) / f"{_safe_rid_filename(rid)}.json"


def decision_path(state_path: Path | str, rid: str) -> Path:
    return decisions_dir(state_path) / f"{_safe_rid_filename(rid)}.json"


def _write_private_file(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    _write_private_file(tmp, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def touch_heartbeat(state_path: Path | str) -> None:
    path = heartbeat_path(state_path)
    _ensure_private_dir(path.parent)
    path.touch()


def heartbeat_fresh(state_path: Path | str, *, max_age: float = HEARTBEAT_FRESH_SECONDS) -> bool:
    try:
        mtime = heartbeat_path(state_path).stat().st_mtime
    except OSError:
        return False
    return (time.time() - mtime) <= max_age


def write_pending(state_path: Path | str, request: dict[str, Any]) -> Path:
    rid = str(request.get("rid", ""))
    path = pending_path(state_path, rid)
    _write_json_atomic(path, request)
    return path


def list_pending(state_path: Path | str) -> list[dict[str, Any]]:
    directory = pending_dir(state_path)
    if not directory.exists():
        return []
    requests: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        request = _read_json(path)
        if request is not None and request.get("rid"):
            requests.append(request)
    return requests


def read_pending(state_path: Path | str, rid: str) -> dict[str, Any] | None:
    data = _read_json(pending_path(state_path, rid))
    # Filename derivation is lossy (sanitize + truncate): reject a file whose
    # embedded rid differs, so colliding rids cannot share a rendezvous.
    if data is not None and str(data.get("rid", rid)) != str(rid):
        return None
    return data


def remove_pending(state_path: Path | str, rid: str) -> None:
    try:
        pending_path(state_path, rid).unlink()
    except OSError:
        pass


def write_decision(state_path: Path | str, rid: str, decision: dict[str, Any]) -> bool:
    """Write-once: only the first decision for an rid lands; later ones no-op."""
    path = decision_path(state_path, rid)
    _ensure_private_dir(path.parent)
    payload = dict(decision)
    payload.setdefault("rid", str(rid))
    payload.setdefault("decided_at", time.time())
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    _write_private_file(tmp, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    try:
        os.link(tmp, path)  # atomic create-if-absent (write-once)
    except FileExistsError:
        return False
    except OSError:
        # Filesystems without hard links: degrade to exclusive create.
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(tmp.read_text(encoding="utf-8"))
        except FileExistsError:
            return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return True


def read_decision(state_path: Path | str, rid: str) -> dict[str, Any] | None:
    data = _read_json(decision_path(state_path, rid))
    if data is not None and str(data.get("rid", rid)) != str(rid):
        return None
    return data


def list_orphan_decision_paths(state_path: Path | str, *, min_age: float = 600.0) -> list[Path]:
    """Decision files with no pending counterpart (nobody will consume them).

    Left behind when a card is clicked after the hook already gave up; the
    drain loop reaps them (documented contract).
    """
    directory = decisions_dir(state_path)
    if not directory.exists():
        return []
    cutoff = time.time() - max(0.0, min_age)
    pending = pending_dir(state_path)
    orphans: list[Path] = []
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        if not (pending / path.name).exists():
            orphans.append(path)
    return orphans


def remove_decision(state_path: Path | str, rid: str) -> None:
    try:
        decision_path(state_path, rid).unlink()
    except OSError:
        pass


def wait_for_decision(
    state_path: Path | str,
    rid: str,
    *,
    timeout: float = DEFAULT_WAIT_TIMEOUT_SECONDS,
    poll_interval: float = DECISION_POLL_INTERVAL_SECONDS,
) -> dict[str, Any] | None:
    """Blocking decision poll for the hook process.

    Returns the decision dict, ``{"action": "pass", ...}`` when the runtime
    stops draining mid-wait (stale heartbeat -> abstain to the native flow),
    or ``None`` on timeout (caller emits a deny).
    """
    deadline = time.monotonic() + max(1.0, float(timeout))
    while time.monotonic() < deadline:
        decision = read_decision(state_path, rid)
        if decision is not None:
            return decision
        if not heartbeat_fresh(state_path):
            return {"action": "pass", "reason": "walkcode_offline"}
        time.sleep(poll_interval)
    return None


def cleanup_gate_files(state_path: Path | str, rid: str) -> None:
    remove_pending(state_path, rid)
    remove_decision(state_path, rid)


def ask_updated_input(tool_input: dict[str, Any], answers: dict[Any, Any]) -> dict[str, Any]:
    """AskUserQuestion answers -> ``updatedInput`` payload.

    Shared with ``_ClaudePermissionBridge._build_ask_updated_input``: answers
    are keyed by question index (int in-process, str after a JSON round trip)
    and map to the question text that Claude Code expects.
    """
    questions = tool_input.get("questions", [])
    if not isinstance(questions, list):
        questions = []
    answers_map: dict[str, str] = {}
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        question_text = str(
            question.get("question") or question.get("header") or question.get("prompt") or ""
        )
        if not question_text:
            continue
        value = answers.get(index)
        if value is None:
            value = answers.get(str(index))
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ",".join(str(item) for item in value)
        answers_map[question_text] = str(value)
    return {"questions": questions, "answers": answers_map}


def gate_kind(tool_name: str) -> str:
    return KIND_ASK_USER if str(tool_name or "") in ASK_USER_TOOL_NAMES else KIND_PERMISSION


def _allow_rule_covers(rule: str, tool_name: str, tool_input: dict[str, Any]) -> bool:
    rule = str(rule or "").strip()
    if not rule:
        return False
    if rule == tool_name or rule == f"{tool_name}(*)":
        return True
    match = re.fullmatch(rf"{re.escape(tool_name)}\((.+)\)", rule)
    if match is None:
        return False
    if tool_name != "Bash":
        # Non-Bash argument rules (e.g. WebFetch(domain:...)) are not evaluated
        # here; treat as not covering so the gate stays on the safe side.
        return False
    pattern = match.group(1).strip()
    command = str(tool_input.get("command", "") or "")
    if pattern.endswith(":*"):
        prefix = pattern[:-2].strip()
        return bool(prefix) and command.startswith(prefix)
    return command.strip() == pattern


def should_gate(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    permission_mode: str = "",
    allow_rules: list[str] | None = None,
    gate_mode: str = "auto",
    gate_tools: list[str] | None = None,
) -> str:
    """Return the gate kind for this tool call, or "" for the native flow.

    AskUserQuestion is always intercepted (it exists to reach the human, and
    the human may only be on the channel side). Permission gating targets the
    tools that would native-prompt: the configured gate set plus ``mcp__*``,
    minus anything the profile's allow rules or permission mode auto-approve.
    """
    mode = str(gate_mode or "auto").strip().lower()
    if mode == "off":
        return ""
    name = str(tool_name or "")
    if name in ASK_USER_TOOL_NAMES:
        return KIND_ASK_USER
    if mode == "ask_only":
        return ""
    permission_mode = str(permission_mode or "").strip()
    # bypassPermissions auto-allows and plan's engine handles its own gating,
    # so abstaining is safe. dontAsk deliberately STAYS gated: it means "don't
    # prompt the terminal", and its native fallback is auto-deny — exactly the
    # case where a channel-side approval card is the only way to say yes.
    if permission_mode in {"bypassPermissions", "plan"}:
        return ""
    gated_names = set(gate_tools) if gate_tools else set(DEFAULT_GATE_TOOLS)
    if name not in gated_names and not name.startswith("mcp__"):
        return ""
    if permission_mode == "acceptEdits" and name in _EDIT_TOOLS:
        return ""
    for rule in allow_rules or []:
        if _allow_rule_covers(rule, name, tool_input):
            return ""
    return KIND_PERMISSION


def profile_allow_rules(config_dir: str | Path) -> list[str]:
    """Best-effort read of ``permissions.allow`` from the profile settings."""
    if not config_dir:
        return []
    settings = _read_json(Path(config_dir).expanduser() / "settings.json")
    if not settings:
        return []
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return []
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        return []
    return [str(rule) for rule in allow if str(rule or "").strip()]


def pre_tool_use_output(
    kind: str,
    decision: dict[str, Any],
    tool_input: dict[str, Any],
) -> dict[str, Any] | None:
    """Map a decision to PreToolUse ``hookSpecificOutput``; None = abstain."""
    action = str((decision or {}).get("action", "") or "deny")
    if action == "pass":
        return None
    output: dict[str, Any] = {"hookEventName": "PreToolUse"}
    if kind == KIND_ASK_USER:
        if action == "answers":
            answers = decision.get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
            output["permissionDecision"] = "allow"
            output["updatedInput"] = ask_updated_input(tool_input, answers)
        else:
            output["permissionDecision"] = "deny"
            output["permissionDecisionReason"] = str(
                decision.get("reason", "") or "用户未通过 WalkCode 回答此问题"
            )
        return {"hookSpecificOutput": output}
    if action in {"allow", "allow_once", "accept", "acceptForSession", "always_allow"}:
        output["permissionDecision"] = "allow"
        return {"hookSpecificOutput": output}
    output["permissionDecision"] = "deny"
    output["permissionDecisionReason"] = str(decision.get("reason", "") or "Denied via WalkCode")
    return {"hookSpecificOutput": output}


def timeout_decision(kind: str) -> dict[str, Any]:
    # Timing out ABSTAINS instead of denying: the hook returns no decision, so
    # Claude Code falls back to its native prompt and the user answers in the
    # terminal. Deny-on-timeout meant nobody could answer anywhere once the IM
    # card went unnoticed — "IM first, terminal after timeout" keeps both
    # surfaces usable (a later click on the stale card is reaped as an orphan
    # decision by the drain loop).
    return {"action": "pass", "reason": "gate_timeout_fallback_to_native"}
