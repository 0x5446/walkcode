"""Module-level helpers for Claude/Codex TUI hook handling.

Hook type normalisation, hook payload / transcript / rollout parsing, resume
and terminate refs, TUI process identity and process-tree capture, event ids,
and the hook spool (defer/queue) helpers. They take plain values and hold no
``ChannelNativeRuntime`` state; the runtime imports them from here.

Patch module-level names used by this code here (for example
``walkcode.channel_native.tui_hooks._probe_process``), not on
``walkcode.channel_native_runtime``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .config import ChannelEndpointConfig
from .models import AgentEvent, AgentEventType, agent_session_id
from .orchestrator import Orchestrator
from .process_control import (
    SHARED_APP_SERVER_CONTROLLER,
    _c_locale_env,
    _command_is_claude_headless_sdk_process,
    _command_is_claude_tui_process,
    _command_is_codex_app_server_process,
    _command_is_codex_tui_process,
    _command_is_external_tui_process,
    _probe_process,
    _proc_identity_matches,
    _ProcProbe,
)
from .stores import _atomic_write_json

# Codex session_meta embeds the base instructions (~20KB today); cap the
# first-line read so a malformed rollout cannot pull a huge line into memory.
_CODEX_SESSION_META_MAX_BYTES = 1024 * 1024


def _normalize_tui_agent(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in {"", "default"}:
        return ""
    if text in {"claude", "claude-code", "claudecode", "claude-headless"}:
        return "claude"
    if text in {"codex", "codex-cli", "codex-app-server"}:
        return "codex"
    return text


def _payload_hook_event_name(payload: dict[str, Any]) -> str:
    return str(
        payload.get("hook_event_name")
        or payload.get("hookEventName")
        or payload.get("event")
        or payload.get("eventName")
        or ""
    )


def _transcript_meta_from_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Read model slug + last-turn usage from the transcript a hook points at.

    TUI-observed sessions have no other source for either: the daemon's job
    record and state patches carry tempo/detail/needs but no model
    (live-verified 2026-07), and hook payloads themselves include neither.
    Claude transcripts carry both on assistant records (message.model /
    message.usage); codex rollouts carry the model on turn_context records
    (payload.model) and usage on token_count event_msg records
    (last_token_usage + model_context_window). Tail-read keeps it cheap on
    long sessions.
    """
    path = str(payload.get("transcript_path", "") or "")
    if not path:
        return "", {}
    try:
        transcript = Path(path).expanduser()
        # The path comes from an (unauthenticated) hook payload: refuse
        # non-regular files (pipes, devices) and cap the read so a hostile
        # path can't block the event loop or read unboundedly.
        info = transcript.stat()
        if not stat.S_ISREG(info.st_mode):
            return "", {}
        with transcript.open("rb") as fh:
            if info.st_size > 65536:
                fh.seek(-65536, os.SEEK_END)
            tail = fh.read(65536).decode("utf-8", "replace")
    except OSError:
        return "", {}
    model = ""
    usage: dict[str, Any] = {}
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message")
        if isinstance(message, dict):
            record_model = str(message.get("model", "") or "")
            # "<synthetic>" marks CLI-generated filler messages, not the model.
            if record_model.startswith("<"):
                continue
            if not model and record_model:
                model = record_model
            if not usage:
                record_usage = message.get("usage")
                if isinstance(record_usage, dict) and record_usage:
                    usage = dict(record_usage)
        record_type = str(record.get("type", "") or "")
        record_payload = record.get("payload")
        if isinstance(record_payload, dict):
            if not model and record_type == "turn_context":
                model = str(record_payload.get("model", "") or "")
            if (
                not usage
                and record_type == "event_msg"
                and str(record_payload.get("type", "") or "") == "token_count"
            ):
                usage = _codex_token_count_usage(record_payload.get("info"))
        if model and usage:
            break
    return model, usage


def _codex_token_count_usage(info: Any) -> dict[str, Any]:
    """Shape a codex token_count record into the shared usage dict.

    Only input/output of the last turn are kept (cached_input_tokens is a
    subset of input_tokens — summing it would double-count); the explicit
    model_context_window rides along for the status card's limit display.
    """
    if not isinstance(info, dict):
        return {}
    last = info.get("last_token_usage")
    if not isinstance(last, dict) or not last:
        return {}
    try:
        usage: dict[str, Any] = {
            "input_tokens": int(last.get("input_tokens", 0) or 0),
            "output_tokens": int(last.get("output_tokens", 0) or 0),
        }
        window = int(info.get("model_context_window", 0) or 0)
    except (TypeError, ValueError):
        return {}
    if not usage["input_tokens"] and not usage["output_tokens"]:
        return {}
    if window:
        usage["model_context_window"] = window
    return usage


def _normalize_tui_hook_type(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    compact = re.sub(r"[^A-Za-z0-9]+", "", text).lower()
    aliases = {
        "sync": "sync",
        "tuioutput": "tui-output",
        "sessionstart": "session-start",
        "setup": "setup",
        "instructionsloaded": "instructions-loaded",
        "userpromptsubmit": "user-prompt-submit",
        "userpromptexpansion": "user-prompt-expansion",
        "messagedisplay": "message-display",
        "pretooluse": "pre-tool",
        "permissionrequest": "permission-request",
        "posttooluse": "post-tool",
        "posttoolusefailure": "post-tool-failure",
        "posttoolbatch": "post-tool-batch",
        "permissiondenied": "permission-denied",
        "notification": "notification",
        "subagentstart": "subagent-start",
        "subagentstop": "subagent-stop",
        "taskcreated": "task-created",
        "taskcompleted": "task-completed",
        "stop": "stop",
        "sessionstop": "stop",
        "sessionend": "stop",
        "stopfailure": "stop-failure",
        "precompact": "pre-compact",
        "postcompact": "post-compact",
        "configchange": "config-change",
        "cwdchanged": "cwd-changed",
        "filechanged": "file-changed",
        "worktreecreate": "worktree-create",
        "worktreeremove": "worktree-remove",
        "teammateidle": "teammate-idle",
        "elicitation": "elicitation",
        "elicitationresult": "elicitation-result",
    }
    if compact in aliases:
        return aliases[compact]
    with_boundaries = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", text)
    kebab = re.sub(r"[^A-Za-z0-9]+", "-", with_boundaries).strip("-").lower()
    return kebab


def _tui_hook_observes_session(hook_type: str) -> bool:
    return hook_type in {
        "sync",
        "session-start",
        "user-prompt-submit",
        "message-display",
        "stop",
        "notification",
        "tui-output",
        "pre-tool",
        "permission-request",
        "post-tool",
        "post-tool-failure",
        "permission-denied",
    }


def _tui_hook_can_claim_existing_session(hook_type: str) -> bool:
    return hook_type in {"sync", "session-start"}


def _tui_hook_can_create_session(hook_type: str) -> bool:
    # ADR 0066: a topic appears with the user's first prompt, titled by it.
    # SessionStart (opened, closed or /resume'd away without a word) and
    # activity hooks carry no prompt and would only root an empty
    # "TUI <uuid>" topic; they still act on a session that already exists.
    return hook_type == "user-prompt-submit"


def _tui_resume_ref(transport_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    explicit = payload.get("resume_ref")
    if isinstance(explicit, dict):
        normalized = {
            key: value
            for key, value in explicit.items()
            if key not in {"transport_kind", "kind"} and value not in ("", None)
        }
        if normalized:
            return normalized

    transport_ref = payload.get("transport_ref")
    if isinstance(transport_ref, dict):
        normalized = {
            key: value
            for key, value in transport_ref.items()
            if key not in {"transport_kind", "kind", "handle_id"} and value not in ("", None)
        }
        if normalized:
            return normalized

    value = agent_session_id(transport_kind, payload)
    if not value:
        return {}
    if transport_kind == "claude_headless":
        return {"agent_session_id": value}
    if transport_kind == "codex_app_server":
        return {"thread_id": value}
    return {"session_id": value}


def _tui_terminate_ref(payload: dict[str, Any]) -> dict[str, Any] | None:
    explicit = payload.get("terminate_ref")
    if isinstance(explicit, dict) and explicit:
        return dict(explicit)

    process_ref = payload.get("process_ref")
    if isinstance(process_ref, dict) and process_ref:
        return {"controller_kind": "process", "process_ref": dict(process_ref)}

    pid_value = payload.get("tui_pid") or payload.get("pid")
    if pid_value:
        try:
            pid = int(pid_value)
        except (TypeError, ValueError):
            return None
        return {
            "controller_kind": "process",
            "process_ref": {"pid": pid, "allow_terminate": bool(payload.get("allow_terminate"))},
        }

    if payload.get("_walkcode_infer_tui_pid"):
        # Prefer the CAPTURED process tree (carries hook-time pid+lstart+command)
        # over re-probing by process-group. The process-group path re-runs `ps`
        # at CONSUME time, so for a deferred replay whose pgid was reused by a
        # different same-command terminal it would record the new process's
        # identity and enrich would re-endorse it (round-3 Critical). The
        # captured snapshot is immune to that reuse window.
        entries = _tui_hook_process_tree_entries(payload)
        captured_ref = _external_tui_process_ref_from_entries(entries)
        if captured_ref is not None:
            return {"controller_kind": "process", "process_ref": captured_ref}
        if entries and _command_is_codex_app_server_process(str(entries[0].get("command", "") or "")):
            # The hook ran inside a codex app-server (the shared daemon since
            # codex 0.157), not inside the TUI: the daemon owns the thread and
            # the TUI is just one of its clients. Record that fact instead of
            # letting the parent-pid fallback below pass the daemon off as the
            # TUI — takeover then attaches to the thread, with nothing to stop.
            return {"controller_kind": SHARED_APP_SERVER_CONTROLLER}
        # Fallback only when the payload carries no captured tree (older hook
        # binaries): best-effort process-group re-probe.
        process_group_ref = _external_tui_process_ref_from_process_group(payload.get("_walkcode_hook_process_group"))
        if process_group_ref is not None:
            return {"controller_kind": "process", "process_ref": process_group_ref}
        process_ref = _infer_process_ref_from_hook_pid(payload.get("_walkcode_hook_pid"))
        if process_ref is not None:
            return {"controller_kind": "process", "process_ref": process_ref}
        try:
            parent_pid = int(payload.get("_walkcode_hook_parent_pid") or 0)
        except (TypeError, ValueError):
            parent_pid = 0
        if parent_pid > 1 and parent_pid != os.getpid():
            return {
                "controller_kind": "process",
                "process_ref": {
                    "pid": parent_pid,
                    "allow_terminate": False,
                    "source": "native_hook_parent_captured",
                },
            }
    return None


def _external_tui_process_ref_from_process_group(process_group_value: Any) -> dict[str, Any] | None:
    try:
        process_group = int(process_group_value or 0)
    except (TypeError, ValueError):
        return None
    if process_group <= 1 or process_group == os.getpid():
        return None
    entries = _process_tree_entries(process_group, max_depth=1)
    if not entries:
        return None
    entry = entries[0]
    command = str(entry.get("command", "") or "")
    if not _command_is_external_tui_process(command):
        return None
    try:
        pid = int(entry.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    if pid <= 1 or pid == os.getpid():
        return None
    ref = {
        "pid": pid,
        "allow_terminate": True,
        "source": "native_hook_process_group",
        "command": command,
    }
    lstart = str(entry.get("lstart", "") or "")
    if lstart:
        ref["lstart"] = lstart
    return ref


def _tui_hook_is_walkcode_headless_transport(transport_kind: str, payload: dict[str, Any]) -> bool:
    commands = _tui_hook_process_tree_commands(payload)
    if not commands:
        return False
    if transport_kind == "claude_headless":
        return any(_command_is_claude_headless_sdk_process(command) for command in commands)
    # Codex is deliberately absent: since codex 0.157 the TUI and WalkCode
    # share one managed app-server daemon, which runs every thread's hooks, so
    # an app-server in the process tree no longer means "ours". Codex hooks
    # are attributed by turn id instead (_tui_hook_is_walkcode_codex_turn).
    return False


def _codex_transcript_is_exec(payload: dict[str, Any]) -> bool:
    """True when the hook comes from a `codex exec` run, not a TUI.

    `codex exec` loads the same user hooks.json as the TUI, so scripted runs
    (deep-review, smoke tests) would each open a mirrored channel topic. The
    process tree cannot tell them apart (both are a `codex` executable), but
    the rollout's first record is authoritative: session_meta.source is
    "exec" for `codex exec` ("cli" for a standalone TUI, "vscode" for a TUI
    served by the shared app-server daemon since codex 0.157). The rollout
    already exists when SessionStart fires. Only "exec" is skipped;
    unreadable or unknown shapes stay observed.

    `codex exec --ephemeral` (what deep-review runs) writes no rollout at all:
    codex still sends the transcript_path key, with a null value. A thread
    with no transcript has nothing to mirror, so that is skipped too. A
    payload without the key (older callers) stays observed.
    """
    if "transcript_path" in payload and not payload["transcript_path"]:
        return True
    path = str(payload.get("transcript_path", "") or "")
    if not path:
        return False
    try:
        with open(path, "rb") as fh:
            record = json.loads(fh.readline(_CODEX_SESSION_META_MAX_BYTES))
    except (OSError, ValueError, RecursionError):
        # RecursionError: deeply nested JSON under the byte cap. It is not a
        # ValueError and would otherwise escape and wedge the hook drain.
        return False
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return False
    meta = record.get("payload")
    return isinstance(meta, dict) and meta.get("source") == "exec"


def _tui_hook_has_external_tui_process_identity(transport_kind: str, payload: dict[str, Any]) -> bool:
    commands = _tui_hook_process_tree_commands(payload)
    if not commands:
        return False
    if transport_kind == "claude_headless":
        return any(_command_is_claude_tui_process(command) for command in commands)
    if transport_kind == "codex_app_server":
        return any(_command_is_codex_tui_process(command) for command in commands)
    return True


def _tui_hook_has_live_tui_process(transport_kind: str, payload: dict[str, Any]) -> bool:
    """A matching TUI process must still be RUNNING now AND be the SAME process
    the hook captured — not merely a live pid.

    Reviving/handing back on a bare pid-liveness check is unsafe: a deferred
    replay's captured pid can be reused by any unrelated process, which would
    read as "live TUI" and (a) falsely revive a dead session, or (b) let a
    stale claim bypass the freshness / predates-owner gates and steal the
    session (round-2 Critical). So we re-probe the pid and require its CURRENT
    command still classify as this transport's TUI and match the captured
    identity (lstart + command). Entries without pids, or whose live identity
    no longer matches, do not count as live proof.
    """
    for entry in _tui_hook_process_tree_entries(payload):
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 1:
            continue
        captured_command = str(entry.get("command", "") or "")
        if transport_kind == "claude_headless" and not _command_is_claude_tui_process(captured_command):
            continue
        if transport_kind == "codex_app_server" and not _command_is_codex_tui_process(captured_command):
            continue
        probe = _probe_process(pid)
        if probe.status != "ok":
            continue
        # The pid must STILL be a TUI of this transport (not a reused pid now
        # running something else)...
        if transport_kind == "claude_headless" and not _command_is_claude_tui_process(probe.command):
            continue
        if transport_kind == "codex_app_server" and not _command_is_codex_tui_process(probe.command):
            continue
        # ...and match the captured identity. Empty captured lstart degrades to
        # a command-only comparison (still far better than pid-only).
        if not _proc_identity_matches(probe, str(entry.get("lstart", "") or ""), captured_command):
            continue
        return True
    return False


def _tui_hook_process_tree_commands(payload: dict[str, Any]) -> list[str]:
    entries = _tui_hook_process_tree_entries(payload)
    if entries:
        return [str(item.get("command", "")) for item in entries if str(item.get("command", ""))]

    captured = payload.get("_walkcode_hook_process_tree")
    if isinstance(captured, list):
        commands = [str(item) for item in captured if str(item or "")]
        if commands:
            return commands

    terminate_ref = _tui_terminate_ref(payload)
    process_ref = terminate_ref.get("process_ref", {}) if isinstance(terminate_ref, dict) else {}
    if not isinstance(process_ref, dict):
        return []
    return _process_tree_commands(process_ref.get("pid"), max_depth=4)


def _payload_captured_at(payload: dict[str, Any]) -> float | None:
    """The raw capture timestamp (epoch seconds), or None if absent/corrupt."""
    import math

    raw = payload.get("_walkcode_hook_captured_at")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _tui_hook_captured_age(payload: dict[str, Any]) -> float | None:
    """Seconds since the hook process captured this payload; None if unknown.

    Deferred-queue replays keep their original capture stamp, so age tells a
    live hook apart from a replayed description of a world that may be gone.
    Payloads from pre-0.14.3 hook binaries lack the stamp -> None.
    """
    captured_at = _payload_captured_at(payload)
    if captured_at is None:
        return None
    now = time.time()
    if captured_at > now + 1.0:
        # A capture stamp in the future is corrupt/forged, not "0s old / fresh".
        return None
    return max(0.0, now - captured_at)


# Freshness threshold + sentinel switch now live on ChannelNativeConfig
# (parsed from the merged env incl. WALKCODE_ENV_FILE). See the runtime
# methods _tui_hook_fresh_seconds / _tui_hook_is_fresh / _tui_sentinel_enabled.


def _enrich_terminate_ref(terminate_ref: dict[str, Any] | None) -> dict[str, Any] | None:
    """Stamp identity (lstart) + recorded_at on a freshly inferred terminate ref.

    Ledger hygiene (ADR 0053, revised after deep-review 2026-07-19):

    - We NO LONGER strip `allow_terminate` for a dead / reused pid. Doing so
      made the takeover predetect fall through to manual-only for the most
      common case (user Ctrl+C'd the TUI) — a regression worse than the bug
      it fixed (cluster A). The kill path's own three-state probe + identity
      gate already refuses to signal a dead or reused pid, so leaving the ref
      armed lets takeover proceed automatically (dead pid -> already_exited).

    - A transient probe error must not mutate authorization at all (cluster C):
      only stamp identity when the probe cleanly succeeds; record probe_state
      for observability.
    """
    if not terminate_ref:
        return terminate_ref
    process_ref = terminate_ref.get("process_ref")
    if not isinstance(process_ref, dict):
        return terminate_ref
    process_ref["recorded_at"] = time.time()
    try:
        pid = int(process_ref.get("pid") or 0)
    except (TypeError, ValueError):
        return terminate_ref
    if pid <= 1:
        return terminate_ref
    probe = _probe_process(pid)
    process_ref["probe_state"] = probe.status
    if probe.status == "gone":
        # Target already exited. Leave allow_terminate as-is: _terminate_sync
        # sees target_gone and skips the pid (session sweep still runs), and
        # takeover continues automatically instead of falling to manual-only.
        process_ref["target_gone"] = True
        return terminate_ref
    if probe.status == "error":
        # Cannot verify now; do not touch authorization or identity.
        return terminate_ref
    recorded_command = str(process_ref.get("command", "") or "").strip()
    recorded_lstart = str(process_ref.get("lstart", "") or "").strip()
    if recorded_command and recorded_command != probe.command:
        # Live pid, different process → the recorded target is gone and the pid
        # was reused. Mark target_gone so the kill path skips it entirely.
        process_ref["target_gone"] = True
        return terminate_ref
    if recorded_lstart and recorded_lstart != probe.lstart:
        # Same command but a different start time: the captured process exited
        # and the pid was reused by another instance of the same program.
        # COMPARE, never overwrite — overwriting with the fresh probe lstart
        # would re-endorse the reused pid (round-2 Critical).
        process_ref["target_gone"] = True
        return terminate_ref
    # Only fill identity that the capture stage did not already provide; keep
    # the capture-time lstart authoritative.
    if not recorded_lstart:
        process_ref["lstart"] = probe.lstart
    if not recorded_command:
        process_ref["command"] = probe.command
    return terminate_ref


def _tui_hook_process_tree_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    captured = payload.get("_walkcode_hook_process_tree_entries")
    if isinstance(captured, list):
        entries: list[dict[str, Any]] = []
        for item in captured:
            if not isinstance(item, dict):
                continue
            try:
                pid = int(item.get("pid") or 0)
                ppid = int(item.get("ppid") or 0)
            except (TypeError, ValueError):
                continue
            command = str(item.get("command", "") or "")
            lstart = str(item.get("lstart", "") or "")
            if pid > 1 and command:
                entries.append({"pid": pid, "ppid": ppid, "lstart": lstart, "command": command})
        if entries:
            return entries

    process_ref = payload.get("process_ref")
    if not isinstance(process_ref, dict):
        terminate_ref = payload.get("terminate_ref")
        process_ref = terminate_ref.get("process_ref", {}) if isinstance(terminate_ref, dict) else {}
    if not isinstance(process_ref, dict):
        return []
    return _process_tree_entries(process_ref.get("pid"), max_depth=4)


def _external_tui_process_ref_from_entries(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in entries:
        command = str(entry.get("command", "") or "")
        if not _command_is_external_tui_process(command):
            continue
        try:
            pid = int(entry.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 1 and pid != os.getpid():
            ref = {
                "pid": pid,
                "allow_terminate": True,
                "source": "native_hook_external_tui",
                "command": command,
            }
            # Carry the CAPTURE-time lstart so the identity gate is anchored to
            # when the hook fired, not when the ref is later enriched/consumed
            # (round-2 cluster: captured identity dropped -> reuse re-endorsed).
            lstart = str(entry.get("lstart", "") or "")
            if lstart:
                ref["lstart"] = lstart
            return ref
    return None


def _process_tree_entries(pid_value: Any, *, max_depth: int = 4) -> list[dict[str, Any]]:
    try:
        pid = int(pid_value or 0)
    except (TypeError, ValueError):
        return []
    entries: list[dict[str, Any]] = []
    seen: set[int] = set()
    for _ in range(max_depth):
        if pid <= 1 or pid in seen:
            break
        seen.add(pid)
        try:
            result = subprocess.run(
                ["ps", "-o", "pid=,ppid=,lstart=,command=", "-p", str(pid)],
                env=_c_locale_env(),
                capture_output=True,
                text=True,
                timeout=1,
            )
        except Exception:
            break
        if result.returncode != 0:
            break
        line = result.stdout.strip()
        if not line:
            break
        # pid ppid lstart(Www Mmm dd HH:MM:SS yyyy) command
        match = re.match(
            r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+[\d:]{8}\s+\d{4})\s+(.*)$",
            line,
            flags=re.DOTALL,
        )
        if match is None:
            break
        lstart = match.group(3).strip()
        command = match.group(4)
        try:
            current_pid = int(match.group(1))
            parent_pid = int(match.group(2))
        except ValueError:
            break
        # lstart captured at hook-fire time is the identity that lets the
        # sentinel detect pid reuse between capture and consume (cluster D).
        entries.append({"pid": current_pid, "ppid": parent_pid, "lstart": lstart, "command": command})
        pid = parent_pid
    return entries


def _process_tree_commands(pid_value: Any, *, max_depth: int = 4) -> list[str]:
    return [str(item.get("command", "")) for item in _process_tree_entries(pid_value, max_depth=max_depth)]


# Command classifiers moved into walkcode.channel_native (imported above):
# LocalProcessController needs them for the session sweep TUI filter, and the
# import direction only allows runtime -> channel_native.


def _infer_process_ref_from_hook_pid(hook_pid_value: Any) -> dict[str, Any] | None:
    try:
        hook_pid = int(hook_pid_value or 0)
    except (TypeError, ValueError):
        return None
    if hook_pid <= 1:
        return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(hook_pid)],
            env=_c_locale_env(),
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        parent_pid = int(result.stdout.strip())
    except ValueError:
        return None
    if parent_pid <= 1 or parent_pid == os.getpid():
        return None
    tui_ref = _external_tui_process_ref_from_entries(_process_tree_entries(parent_pid, max_depth=4))
    if tui_ref is not None:
        return tui_ref
    return {
        "pid": parent_pid,
        "allow_terminate": False,
        "source": "native_hook_parent",
    }


def _tui_event_id(
    hook_type: str,
    transport_kind: str,
    resume_ref: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    identity = agent_session_id(transport_kind, resume_ref)
    # Tool lifecycle hooks (PreToolUse/PostToolUse/...) carry a per-call
    # tool_use_id that is unique within the task. Using turn_id here — which
    # codex 0.144+ keeps CONSTANT across every tool in one turn — made the
    # 2nd+ tool event of a long task look like a duplicate of the first and
    # got silently dropped (TUI codex output stopped syncing to Feishu).
    if _tui_hook_is_tool_lifecycle(hook_type):
        tool_id = str(
            _tui_payload_first(
                payload,
                ("tool_use_id", "tool_id", "toolCallId", "tool_call_id", "request_id", "id"),
            )
            or ""
        )
        if tool_id:
            return f"external_tui:{hook_type}:{transport_kind}:{identity}:{tool_id}"
    explicit = (
        payload.get("event_id")
        or payload.get("hook_event_id")
        or payload.get("turn_id")
        or payload.get("request_id")
    )
    if explicit:
        suffix = str(explicit)
    else:
        text = _tui_hook_text(hook_type, payload)
        stable = {
            "hook_type": hook_type,
            "transport_kind": transport_kind,
            "identity": identity,
            "message": text,
            "session_id": payload.get("session_id", ""),
            "tool_id": _tui_payload_first(
                payload,
                ("tool_use_id", "tool_id", "toolCallId", "tool_call_id", "request_id", "id"),
            ),
            "tool_name": _tui_tool_name(payload),
            "timestamp": (
                payload.get("timestamp", "")
                or payload.get("created_at", "")
                or payload.get("_walkcode_deferred_id", "")
            ),
        }
        suffix = hashlib.sha1(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()
    return f"external_tui:{hook_type}:{transport_kind}:{identity}:{suffix}"


def _tui_lark_chat_id(endpoint: ChannelEndpointConfig) -> str:
    # Explicit TUI chat wins, otherwise a single-entry allowlist unambiguously
    # names the observation chat.
    configured = str(endpoint.options.get("tui_chat_id", "") or "").strip()
    if configured:
        return configured
    allowed = tuple(str(item).strip() for item in endpoint.options.get("allowed_chat_ids", ()) if str(item).strip())
    if len(allowed) == 1:
        return allowed[0]
    return ""


_TRANSCRIPT_READ_MAX_BYTES = 2 * 1024 * 1024


def _payload_transcript_boundary(
    payload: dict[str, Any],
) -> tuple[int, tuple[int, int] | None] | None:
    """The (size, file identity) stamped when the hook FIRED, if any.

    The identity ((st_dev, st_ino), when stamped by 0.14.6+) pins the size to
    the file it was measured on — a boundary applied to a DIFFERENT file
    would expose that file's history as live narration (ADR 0055 revision 2).
    """
    raw = payload.get("_walkcode_transcript_size")
    if raw is None:
        return None
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return None
    if size < 0:
        return None
    key_raw = payload.get("_walkcode_transcript_file_key")
    key: tuple[int, int] | None = None
    if isinstance(key_raw, (list, tuple)) and len(key_raw) == 2:
        try:
            key = (int(key_raw[0]), int(key_raw[1]))
        except (TypeError, ValueError):
            key = None
    return size, key


def _stamp_transcript_size(payload: dict[str, Any]) -> None:
    """Stamp the capture-time transcript size AND file identity onto a hook.

    The narration cursor must be bounded by what existed when the hook fired,
    not when it is drained: a delayed drain would otherwise lift the
    turn-final text (written after the last tool call) into a narration line
    right before Stop sends the same text as a bubble. Size and identity are
    taken from one fstat on an open handle so they cannot describe two
    different files.
    """
    if "_walkcode_transcript_size" in payload:
        return
    path = str(payload.get("transcript_path", "") or "")
    if not path:
        return
    try:
        with open(path, "rb") as fh:
            info = os.fstat(fh.fileno())
    except OSError:
        return
    payload["_walkcode_transcript_size"] = int(info.st_size)
    payload["_walkcode_transcript_file_key"] = [int(info.st_dev), int(info.st_ino)]


def _read_transcript_narration(
    path: str,
    cursor: tuple[Any, ...] | None,
    boundary: tuple[int, tuple[int, int] | None] | None = None,
) -> tuple[tuple[Any, ...] | None, list[str]]:
    """Read new assistant narration texts from an agent transcript (ADR 0055).

    Understands both transcript dialects: Claude session files (``type ==
    "assistant"`` entries with ``message.content`` text blocks) and Codex
    rollouts (``type == "event_msg"`` entries whose payload is an
    ``agent_message``).

    ``cursor`` is (path, byte_offset, file_key, discarding) from the previous
    read, where file_key is (st_dev, st_ino): a replaced file at the same
    path must not be read from the old offset — its bytes there are history,
    and replaying history into the channel is never acceptable. First sight
    of a file fast-forwards WITHOUT emitting. ``boundary`` is the (size,
    file identity) stamped at hook fire time; it caps every read (bytes
    written after the hook fired belong to a later hook) but applies ONLY to
    the file it was measured on — against a replaced file it is meaningless
    and the call emits nothing. Only complete JSONL lines are consumed — a
    torn tail waits for the next call, and a single line larger than the
    batch cap flips ``discarding``: subsequent reads drop bytes until that
    line's real newline, so no mid-line fragment ever reaches the JSON
    parser. Returns (new_cursor, texts); new_cursor is None when the file is
    unreadable and no prior cursor exists (storing (path, 0) would replay the
    whole file once it appears).
    """
    try:
        fh = open(path, "rb")
    except OSError:
        return cursor, []
    with fh:
        try:
            info = os.fstat(fh.fileno())
        except OSError:
            return cursor, []
        size = int(info.st_size)
        file_key = (info.st_dev, info.st_ino)
        boundary_size: int | None = None
        boundary_key: tuple[int, int] | None = None
        if boundary is not None:
            boundary_size, boundary_key = boundary
        boundary_foreign = boundary_key is not None and boundary_key != file_key
        stale_cursor = (
            cursor is None
            or len(cursor) < 4
            or cursor[0] != path
            or cursor[2] != file_key
            or int(cursor[1]) > size
        )
        if boundary_foreign:
            # The hook was captured against a file that no longer exists at
            # this path; its boundary says nothing about THIS file. Emit
            # nothing — a later hook stamped on the current file drains. On
            # first sight the current content is all pre-cursor history:
            # skip it entirely.
            if stale_cursor:
                return (path, size, file_key, False), []
            return (path, int(cursor[1]), file_key, bool(cursor[3])), []
        limit = size if boundary_size is None else max(0, min(boundary_size, size))
        if stale_cursor:
            if boundary_size is not None and boundary_key is None:
                # A size-only boundary (legacy payload) cannot prove which
                # file it was measured on; positioning a FRESH cursor with it
                # could land mid-history of a replaced file. Skip to EOF —
                # degraded but safe ("never replay" beats "never miss").
                return (path, size, file_key, False), []
            return (path, limit, file_key, False), []
        offset = int(cursor[1])
        discarding = bool(cursor[3])
        if offset >= limit:
            return (path, offset, file_key, discarding), []
        requested = min(limit - offset, _TRANSCRIPT_READ_MAX_BYTES)
        try:
            fh.seek(offset)
            blob = fh.read(requested)
        except OSError:
            return (path, offset, file_key, discarding), []
    base_offset = offset
    if discarding:
        # Finish dropping the over-long line BEFORE parsing anything: a
        # mid-line fragment could otherwise parse as a valid JSON entry.
        cut = blob.find(b"\n")
        if cut < 0:
            return (path, offset + len(blob), file_key, True), []
        # Crossed the real newline: the rest of this batch parses normally
        # in the SAME call (returning early would delay legit narration by
        # one hook — or lose it to a following Stop advance).
        blob = blob[cut + 1 :]
        base_offset = offset + cut + 1
    end = blob.rfind(b"\n")
    if end < 0:
        if base_offset == offset and (limit - offset) > len(blob):
            # A FULL cap-sized window from a line start with no newline: the
            # line is bigger than the batch cap. Skip what we read and keep
            # discarding until its real newline, so the cursor cannot wedge
            # and no fragment reaches the parser. (A window trimmed by the
            # discard prefix is partial — it cannot prove over-long; the next
            # read starts at the line start with a full window and decides.)
            return (path, offset + len(blob), file_key, True), []
        return (path, base_offset, file_key, False), []
    consumed = blob[: end + 1]
    new_offset = base_offset + end + 1
    texts: list[str] = []
    for raw in consumed.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("isSidechain"):
            continue
        entry_type = str(entry.get("type", ""))
        if entry_type == "event_msg":
            # Codex rollout line. The same assistant text appears TWICE in a
            # rollout (event_msg/agent_message + response_item/message with
            # role assistant), so only the event_msg form is read — parsing
            # both would post every message twice.
            codex_payload = entry.get("payload")
            if (
                isinstance(codex_payload, dict)
                and str(codex_payload.get("type", "")) == "agent_message"
            ):
                codex_text = str(codex_payload.get("message", "") or "").strip()
                if codex_text:
                    texts.append(codex_text)
            continue
        if entry_type != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        parts = [
            str(block.get("text", "") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
        if text:
            texts.append(text)
    return (path, new_offset, file_key, False), texts


def _tui_hook_text(hook_type: str, payload: dict[str, Any]) -> str:
    if _tui_hook_is_tool_lifecycle(hook_type):
        return ""
    if hook_type in {"sync", "session-start"}:
        return ""
    event_name = str(payload.get("eventName") or payload.get("event_name") or payload.get("method") or "")
    if _is_internal_tui_event_name(event_name):
        return ""
    message = _tui_visible_text_from_payload(payload, include_prompt=hook_type == "user-prompt-submit").strip()
    if _looks_like_internal_tui_text(message):
        return ""
    title = str(payload.get("title") or "").strip()
    if hook_type == "notification" and title and message:
        return f"{title}\n\n{message}"
    if title and not message:
        if _looks_like_internal_tui_text(title):
            return ""
        return title
    return message


def _tui_visible_text_from_payload(payload: dict[str, Any], *, include_prompt: bool = False) -> str:
    keys = ("prompt", "message", "text", "last_assistant_message") if include_prompt else ("message", "text", "last_assistant_message")
    for key in keys:
        text = _tui_visible_text_from_value(payload.get(key))
        if text:
            return text
    return ""


def _tui_visible_text_from_value(value: Any) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        direct = value.get("text") or value.get("message") or value.get("last_assistant_message")
        if isinstance(direct, str) and direct:
            return direct
        content = value.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return _tui_visible_text_from_content_blocks(content)
        return ""
    if isinstance(value, list):
        return _tui_visible_text_from_content_blocks(value)
    return str(value)


def _tui_visible_text_from_content_blocks(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type", "") or "").lower()
        if block_type and block_type not in {"text", "output_text", "markdown"}:
            continue
        text = block.get("text") or block.get("content")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _is_idle_notification_text(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return True
    return any(
        marker in lowered
        for marker in (
            "waiting for your input",
            "waiting for input",
            "awaiting your input",
        )
    )


def _tui_hook_is_tool_lifecycle(hook_type: str) -> bool:
    return hook_type in {
        "pre-tool",
        "permission-request",
        "post-tool",
        "post-tool-failure",
        "permission-denied",
    }


def _tui_hook_tool_event(hook_type: str, payload: dict[str, Any]) -> AgentEvent | None:
    if not _tui_hook_is_tool_lifecycle(hook_type):
        return None
    tool_name = _tui_tool_name(payload) or "tool"
    tool_id = str(
        _tui_payload_first(
            payload,
            ("tool_use_id", "tool_id", "toolCallId", "tool_call_id", "request_id", "id"),
        )
        or ""
    )
    if hook_type in {"post-tool-failure", "permission-denied"}:
        event_type = AgentEventType.TOOL_FAILED
        default_summary = "Tool failed" if hook_type == "post-tool-failure" else "Permission denied"
        summary_value = _tui_payload_first(payload, ("summary", "message", "error", "reason"))
    elif hook_type == "post-tool":
        event_type = AgentEventType.TOOL_COMPLETED
        default_summary = "Tool completed"
        summary_value = _tui_payload_first(payload, ("summary", "message"))
    else:
        event_type = AgentEventType.TOOL_STARTED
        default_summary = "Permission requested" if hook_type == "permission-request" else "Tool started"
        summary_value = _tui_payload_first(
            payload,
            ("summary", "message", "tool_input", "input", "arguments", "args"),
        )
    return AgentEvent(
        event_type,
        {
            "tool_id": tool_id,
            "tool_name": tool_name,
            "summary": _compact_tui_hook_summary(summary_value) or default_summary,
        },
    )


def _tui_tool_name(payload: dict[str, Any]) -> str:
    direct = _tui_payload_first(payload, ("tool_name", "toolName", "name", "command"))
    if direct:
        return str(direct)
    tool = payload.get("tool")
    if isinstance(tool, dict):
        nested = _tui_payload_first(tool, ("name", "tool_name", "toolName", "command"))
        if nested:
            return str(nested)
    return ""


def _tui_payload_first(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in ("", None):
            return value
    return ""


def _compact_tui_hook_summary(value: Any, *, limit: int = 240) -> str:
    if value in ("", None):
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    else:
        text = str(value)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _is_internal_tui_event_name(event_name: str) -> bool:
    return event_name in {
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "mcpServer/startupStatus/updated",
        "turn/started",
        "turn/completed",
    }


def _looks_like_internal_tui_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    internal_events = (
        "thread/status/changed",
        "thread/tokenUsage/updated",
        "mcpServer/startupStatus/updated",
        "turn/started",
        "turn/completed",
    )
    if value.startswith("[") and any(event in value for event in internal_events):
        return True
    if value.startswith("hook handler run:") and (
        "handlerType" in value or "executionMode" in value or "sourcePath" in value
    ):
        return True
    return False


def _external_tui_process_ref(session: Any) -> dict[str, Any]:
    terminate_ref = Orchestrator._takeover_terminate_ref(session)
    if not isinstance(terminate_ref, dict) or not terminate_ref:
        return {}
    controller_kind, process_ref = Orchestrator._normalize_takeover_terminate_ref(terminate_ref)
    if controller_kind != "process" or not isinstance(process_ref, dict):
        return {}
    return process_ref


def _process_ref_pid(process_ref: dict[str, Any]) -> int:
    try:
        return int(process_ref.get("pid", 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _process_ref_identity(process_ref: dict[str, Any]) -> tuple[int, str]:
    return _process_ref_pid(process_ref), " ".join(str(process_ref.get("lstart", "") or "").split())


def _process_ref_state(process_ref: dict[str, Any], probe: _ProcProbe) -> str:
    """``alive`` / ``gone`` / ``unknown`` for a recorded TUI process (ADR 0066).

    A live pid whose start time differs from the record is a reused pid —
    the recorded process is gone.
    """
    if probe.status == "error":
        return "unknown"
    if probe.status == "gone":
        return "gone"
    recorded = " ".join(str(process_ref.get("lstart", "") or "").split())
    if recorded and recorded != " ".join(probe.lstart.split()):
        return "gone"
    return "alive"


def _process_ref_state_now(process_ref: dict[str, Any]) -> str:
    pid = _process_ref_pid(process_ref)
    if pid <= 1 or pid == os.getpid():
        return "unknown"
    return _process_ref_state(process_ref, _probe_process(pid))


def _session_is_external_tui_writer(session: Any) -> bool:
    return session.writer_owner is not None and session.writer_owner.kind == "external_tui"


def _defer_tui_hook(
    state_path: str | Path, *, hook_type: str, payload: dict[str, Any], agent: str = "",
) -> dict[str, Any]:
    queue_dir = _tui_hook_queue_dir(Path(state_path).expanduser())
    hook_id = uuid.uuid4().hex
    created_at_ns = time.time_ns()
    queued_payload = dict(payload)
    queued_payload.setdefault("_walkcode_deferred_id", hook_id)
    # Enqueue time IS capture time for direct defer callers; a drain
    # minutes later must not treat the then-current transcript size as
    # this hook's boundary (ADR 0055).
    queued_payload.setdefault("_walkcode_hook_captured_at", created_at_ns / 1_000_000_000)
    _stamp_transcript_size(queued_payload)
    queued = {
        "id": hook_id,
        "created_at": created_at_ns / 1_000_000_000,
        "created_at_ns": created_at_ns,
        "hook_type": str(hook_type or ""),
        "agent": str(agent or ""),
        "payload": queued_payload,
    }
    filename = (
        f"{created_at_ns:019d}-"
        f"{os.getpid()}-{queued['id']}.json"
    )
    final_path = queue_dir / filename
    _atomic_write_json(final_path, queued)
    return {"queued": True, "id": queued["id"], "path": str(final_path)}


def _tui_hook_queue_dir(state_path: Path) -> Path:
    return state_path.parent / f"{state_path.name}.tui-hooks.d"


def _deferred_tui_hook_created_at(path: Path) -> float:
    prefix = path.name.split("-", 1)[0]
    try:
        return int(prefix) / 1_000_000_000
    except (TypeError, ValueError):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
