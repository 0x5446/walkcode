#!/usr/bin/env python3
"""Module-level debug runner for channel-native V3.

The script avoids printing secrets. Telegram ingress diagnostics call
getUpdates without an offset, so they do not confirm or consume pending updates.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from walkcode.channel_native import (  # noqa: E402
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ChannelConfigError,
    ChannelNativeConfig,
    DurableOutbox,
    InboundLedger,
    InteractionStore,
    JsonFileStateStore,
    LaunchSpec,
    OutboxDispatcher,
    PermanentDeliveryError,
    render_view_text,
    SessionRegistry,
    StateSnapshot,
    TransientDeliveryError,
    TransportUnavailable,
    TurnInput,
    WriterOwner,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime, _load_native_env  # noqa: E402


TEST_GROUPS = {
    "config": ["tests/test_channel_native_config.py"],
    "telegram": ["tests/test_channel_native_runtime.py", "tests/test_channel_native_telegram_claude.py"],
    "agent": ["tests/test_channel_native_runtime.py", "tests/test_channel_native_codex.py"],
    "state": ["tests/test_channel_native_persistence_reliability.py", "tests/test_channel_native_debug_script.py"],
    "outbox": ["tests/test_channel_native_views_auth_outbox.py", "tests/test_channel_native_persistence_reliability.py"],
    "agent-smoke": ["tests/test_channel_native_runtime.py", "tests/test_channel_native_telegram_claude.py", "tests/test_channel_native_codex.py"],
    "runtime": ["tests/test_channel_native_runtime.py", "tests/test_channel_native_core.py", "tests/test_channel_native_debug_script.py"],
    "lark": [
        "tests/test_channel_native_lark.py",
        "tests/test_channel_native_lark_cards.py",
        "tests/test_channel_native_lark_live.py",
    ],
    "all": ["tests/test_channel_native_*.py"],
}


def main() -> None:
    parser = argparse.ArgumentParser(prog="channel_native_debug.py")
    parser.add_argument(
        "--env-file",
        default=os.environ.get("WALKCODE_ENV_FILE", str(Path.home() / ".walkcode" / ".env")),
        help="channel-native env file path",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Validate and summarize V3 config without secrets")
    config.add_argument("--json", action="store_true")

    agent = sub.add_parser("agent", help="Inspect the configured agent capability without starting a turn")
    agent.add_argument("--json", action="store_true")

    state = sub.add_parser("state", help="Validate state persistence without touching configured state")
    state.add_argument("--json", action="store_true")
    state.add_argument(
        "--repair-stale-errors",
        action="store_true",
        help="Stop expired ERROR_RECOVERABLE sessions that have no durable resume ref; creates a state backup first",
    )
    state.add_argument(
        "--repair-stale-external-tui",
        action="store_true",
        help="Stop completed/dead read-only external TUI sessions; creates a state backup first",
    )

    outbox = sub.add_parser("outbox", help="Inspect outbox state and run synthetic dispatch contracts")
    outbox.add_argument("--json", action="store_true")
    outbox.add_argument(
        "--drop-empty-pending",
        action="store_true",
        help="Drop pending deliveries whose rendered text is empty; creates a state backup first",
    )

    runtime = sub.add_parser("runtime", help="Inspect local runtime consumers that could consume IM updates")
    runtime.add_argument("--json", action="store_true")

    smoke = sub.add_parser("agent-smoke", help="Validate one agent adapter; live turn only with --live")
    smoke.add_argument("--json", action="store_true")
    smoke.add_argument("--agent", choices=["claude", "codex"], default="")
    smoke.add_argument("--live", action="store_true", help="Launch the configured agent and submit a minimal prompt")
    smoke.add_argument(
        "--prompt",
        default="Return exactly: walkcode-agent-smoke-ok",
        help="Prompt for --live smoke",
    )
    smoke.add_argument("--timeout", type=float, default=60.0, help="Per-step timeout for --live smoke")

    telegram = sub.add_parser("telegram", help="Inspect Telegram ingress without consuming updates")
    telegram.add_argument("--json", action="store_true")
    telegram.add_argument("--limit", type=int, default=5)

    lark = sub.add_parser("lark", help="Check Lark credentials/domain; --live sends and patches a card")
    lark.add_argument("--json", action="store_true")
    lark.add_argument(
        "--live",
        action="store_true",
        help="Send a card to WALKCODE_E2E_LARK_CHAT_ID and patch it (requires WALKCODE_E2E_LARK=1)",
    )

    tests = sub.add_parser("tests", help="Run a module-level pytest group")
    tests.add_argument("module", choices=sorted(TEST_GROUPS))

    args = parser.parse_args()
    os.environ["WALKCODE_ENV_FILE"] = str(Path(args.env_file).expanduser())

    try:
        if args.command == "config":
            payload = debug_config()
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "agent":
            payload = debug_agent()
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "state":
            payload = debug_state(
                repair_stale_errors=args.repair_stale_errors,
                repair_stale_external_tui=args.repair_stale_external_tui,
            )
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "outbox":
            payload = asyncio.run(debug_outbox(drop_empty_pending=args.drop_empty_pending))
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "runtime":
            payload = debug_runtime_processes()
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "agent-smoke":
            payload = asyncio.run(
                debug_agent_smoke(
                    agent=args.agent,
                    live=args.live,
                    prompt=args.prompt,
                    timeout=args.timeout,
                )
            )
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "telegram":
            payload = asyncio.run(debug_telegram(limit=args.limit))
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "lark":
            payload = asyncio.run(debug_lark(live=args.live))
            print_payload(payload, as_json=args.json)
            raise SystemExit(0 if payload["ok"] else 1)
        if args.command == "tests":
            raise SystemExit(run_tests(args.module))
    except ChannelConfigError as exc:
        payload = {"ok": False, "error": str(exc)}
        print_payload(payload, as_json=getattr(args, "json", False))
        raise SystemExit(1) from None


def debug_config() -> dict[str, Any]:
    env = _load_native_env(None)
    cfg = ChannelNativeConfig.from_env(env)
    channel = cfg.channel
    return {
        "ok": True,
        "channel": {
            "kind": channel.kind,
            "credential_keys": sorted(channel.credentials),
            "allowlist_configured": bool(channel.options.get("allowed_chat_ids")),
            "allowlist_count": len(channel.options.get("allowed_chat_ids", ())),
            "polling": channel.options.get("polling"),
        },
        "agent": cfg.agent,
        "cwd": cfg.cwd,
        "state_path": cfg.state_path,
    }


def debug_agent() -> dict[str, Any]:
    runtime = ChannelNativeRuntime.from_env()
    status = runtime.describe()
    agent_status = status.get("agent_status", {})
    return {
        "ok": bool(agent_status.get("available")),
        "channel": status.get("channel", {}),
        "agent": status.get("agent", ""),
        "agent_status": agent_status,
    }


def debug_state(
    *,
    repair_stale_errors: bool = False,
    repair_stale_external_tui: bool = False,
) -> dict[str, Any]:
    env = _load_native_env(None)
    cfg = ChannelNativeConfig.from_env(env)
    snapshot, load_report = _load_state_snapshot(cfg)
    repair_report = _repair_stale_error_sessions(cfg, snapshot) if repair_stale_errors else {"enabled": False}
    external_tui_repair_report = (
        _repair_stale_external_tui_sessions(cfg, snapshot)
        if repair_stale_external_tui
        else {"enabled": False}
    )
    if repair_report.get("repaired_count") or external_tui_repair_report.get("repaired_count"):
        snapshot, load_report = _load_state_snapshot(cfg)
    write_probe = _probe_state_write(Path(cfg.state_path))
    counts = _snapshot_counts(snapshot) if snapshot is not None else {}
    expired_writer_leases = int(counts.get("expired_writer_leases", 0) or 0)
    payload: dict[str, Any] = {
        "ok": bool(load_report["ok"] and write_probe["ok"] and expired_writer_leases == 0),
        "state_path": cfg.state_path,
        "state_file": load_report,
        "write_probe": write_probe,
        "repair": repair_report,
        "external_tui_repair": external_tui_repair_report,
        "warnings": [],
    }
    if snapshot is not None:
        payload["counts"] = counts
    if expired_writer_leases:
        payload["warnings"].append(
            "state has running session(s) with expired writer lease; consume commands may confirm IM updates without submitting them"
        )
    return payload


def _repair_stale_error_sessions(
    cfg: ChannelNativeConfig,
    snapshot: StateSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {"enabled": True, "repaired_count": 0, "reason": "state_file_missing"}
    now = time.time()
    repaired: list[str] = []
    for session in snapshot.sessions._sessions.values():
        lease = session.writer_lease
        if session.status == "stopped":
            continue
        if session.lifecycle_state != "ERROR_RECOVERABLE":
            continue
        if lease is None or not lease.expired(now):
            continue
        if _debug_session_has_durable_resume_ref(session):
            continue
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.stop_reason = "repaired_stale_unresumable_error"
        session.writer_lease = None
        session.writer_owner = WriterOwner(kind="none")
        repaired.append(session.session_id)
    if not repaired:
        return {"enabled": True, "repaired_count": 0}
    state_path = Path(cfg.state_path)
    backup_path = state_path.with_suffix(state_path.suffix + f".bak-{int(now)}")
    shutil.copy2(state_path, backup_path)
    JsonFileStateStore(str(state_path)).save(
        sessions=snapshot.sessions,
        interactions=snapshot.interactions,
        outbox=snapshot.outbox,
        authz=snapshot.authz,
        inbound_ledger=snapshot.inbound_ledger,
    )
    return {
        "enabled": True,
        "repaired_count": len(repaired),
        "backup_path": str(backup_path),
    }


def _repair_stale_external_tui_sessions(
    cfg: ChannelNativeConfig,
    snapshot: StateSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {"enabled": True, "repaired_count": 0, "reason": "state_file_missing"}
    now = time.time()
    repaired: list[str] = []
    checked = 0
    for session in snapshot.sessions._sessions.values():
        if session.status == "stopped":
            continue
        owner = session.writer_owner
        if session.transport_kind != "external_tui" and (owner is None or owner.kind != "external_tui"):
            continue
        if _debug_external_tui_stop_hook(session):
            session.status = "stopped"
            session.lifecycle_state = "STOPPED"
            session.stop_reason = "repaired_external_tui_stop_hook"
            session.writer_lease = None
            session.writer_owner = WriterOwner(kind="none")
            repaired.append(session.session_id)
            continue
        process_ref = _debug_external_tui_process_ref(session)
        if not process_ref:
            continue
        checked += 1
        pid = _debug_int(process_ref.get("pid"))
        if pid > 0 and _debug_pid_alive(pid):
            continue
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.stop_reason = "repaired_stale_external_tui_process_gone"
        session.writer_lease = None
        session.writer_owner = WriterOwner(kind="none")
        repaired.append(session.session_id)
    if not repaired:
        return {"enabled": True, "checked_count": checked, "repaired_count": 0}
    state_path = Path(cfg.state_path)
    backup_path = state_path.with_suffix(state_path.suffix + f".bak-{int(now)}")
    shutil.copy2(state_path, backup_path)
    JsonFileStateStore(str(state_path)).save(
        sessions=snapshot.sessions,
        interactions=snapshot.interactions,
        outbox=snapshot.outbox,
        authz=snapshot.authz,
        inbound_ledger=snapshot.inbound_ledger,
    )
    return {
        "enabled": True,
        "checked_count": checked,
        "repaired_count": len(repaired),
        "backup_path": str(backup_path),
    }


def _debug_external_tui_stop_hook(session: Any) -> bool:
    ref = getattr(session, "transport_ref", {}) or {}
    hook_type = str(ref.get("hook_type", "") if isinstance(ref, dict) else "").strip().lower()
    progress = str(getattr(session, "last_progress_event", "") or "").strip().lower()
    return hook_type == "stop" or progress == "external_tui.stop"


def _debug_external_tui_process_ref(session: Any) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    transport_ref = getattr(session, "transport_ref", {}) or {}
    if isinstance(transport_ref, dict):
        refs.append(transport_ref)
    owner = getattr(session, "writer_owner", None)
    owner_ref = getattr(owner, "external_ref", {}) if owner is not None else {}
    if isinstance(owner_ref, dict):
        refs.append(owner_ref)
    for ref in refs:
        terminate_ref = ref.get("terminate_ref", {})
        if not isinstance(terminate_ref, dict):
            continue
        if terminate_ref.get("controller_kind") != "process":
            continue
        process_ref = terminate_ref.get("process_ref", {})
        if isinstance(process_ref, dict):
            return process_ref
    return {}


def _debug_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _debug_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _debug_session_has_durable_resume_ref(session: Any) -> bool:
    ref = getattr(session, "transport_ref", {}) or {}
    transport_kind = str(getattr(session, "transport_kind", ""))
    if transport_kind == "claude_headless":
        return bool(ref.get("agent_session_id") or ref.get("claude_session_id"))
    if transport_kind == "codex_app_server":
        return bool(ref.get("thread_id"))
    return bool(ref)


async def debug_outbox(*, drop_empty_pending: bool = False) -> dict[str, Any]:
    env = _load_native_env(None)
    cfg = ChannelNativeConfig.from_env(env)
    snapshot, load_report = _load_state_snapshot(cfg)
    repair_report = (
        _drop_empty_pending_deliveries(cfg, snapshot)
        if drop_empty_pending
        else {"enabled": False}
    )
    if repair_report.get("dropped_count"):
        snapshot, load_report = _load_state_snapshot(cfg)
    synthetic = await _probe_outbox_dispatch()
    payload: dict[str, Any] = {
        "ok": bool(load_report["ok"] and synthetic["ok"]),
        "state_path": cfg.state_path,
        "state_file": load_report,
        "synthetic_dispatch": synthetic,
        "repair": repair_report,
    }
    if snapshot is not None:
        payload["outbox"] = _outbox_counts(snapshot.outbox)
    return payload


def _drop_empty_pending_deliveries(
    cfg: ChannelNativeConfig,
    snapshot: StateSnapshot | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {"enabled": True, "dropped_count": 0, "reason": "state_file_missing"}
    pending = list(snapshot.outbox._pending.items())
    drop_ids = [
        delivery_id
        for delivery_id, item in pending
        if not render_view_text(item.view_model)
    ]
    if not drop_ids:
        return {"enabled": True, "dropped_count": 0}
    now = time.time()
    state_path = Path(cfg.state_path)
    backup_path = state_path.with_suffix(state_path.suffix + f".bak-{int(now)}")
    shutil.copy2(state_path, backup_path)
    for delivery_id in drop_ids:
        snapshot.outbox._pending.pop(delivery_id, None)
    JsonFileStateStore(str(state_path)).save(
        sessions=snapshot.sessions,
        interactions=snapshot.interactions,
        outbox=snapshot.outbox,
        authz=snapshot.authz,
        inbound_ledger=snapshot.inbound_ledger,
    )
    return {
        "enabled": True,
        "dropped_count": len(drop_ids),
        "backup_path": str(backup_path),
    }


async def debug_agent_smoke(
    *,
    agent: str,
    live: bool,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    runtime = ChannelNativeRuntime.from_env()
    selected = agent or runtime.config.agent
    status = runtime.describe()
    agent_status = status.get("agent_status", {}) if selected == runtime.config.agent else {}
    transport_kind = _transport_kind_for_agent(selected)
    payload: dict[str, Any] = {
        "ok": bool(agent_status.get("available")),
        "agent": selected,
        "transport_kind": transport_kind,
        "live": bool(live),
        "available": bool(agent_status.get("available")),
        "capabilities": agent_status.get("capabilities", {}),
    }
    if selected != runtime.config.agent:
        return {**payload, "ok": False, "error": f"agent not configured for this bot: {selected}"}
    if not live:
        return payload
    if not payload["available"]:
        return {**payload, "ok": False, "error": "configured agent adapter is not available"}

    transport = runtime.transports.get(transport_kind)
    if transport is None:
        return {**payload, "ok": False, "error": "configured agent transport is not wired"}
    try:
        session_id = f"debug-{uuid.uuid4().hex}"
        handle = await asyncio.wait_for(
            transport.launch(LaunchSpec(cwd=runtime.config.cwd, session_id=session_id)),
            timeout=timeout,
        )
        await asyncio.wait_for(
            transport.submit_turn(
                handle,
                TurnInput(text=prompt),
                idempotency_key=f"agent-smoke:{session_id}",
            ),
            timeout=timeout,
        )
        events_value = transport.events(handle)
        if inspect.isawaitable(events_value):
            events = await asyncio.wait_for(events_value, timeout=timeout)
        else:
            events = list(events_value or [])
    except (TransportUnavailable, Exception) as exc:
        return {
            **payload,
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }

    return {
        **payload,
        "ok": not _agent_smoke_error_events(events),
        "handle_created": True,
        "event_count": len(list(events or [])),
        "event_types": [str(getattr(event, "type", "")) for event in list(events or [])],
        **_agent_smoke_error_payload(events),
    }


async def debug_lark(*, live: bool) -> dict[str, Any]:
    from walkcode.channel_native import ChannelNativeE2EGates

    runtime = ChannelNativeRuntime.from_env()
    report = await runtime.diagnose_lark_ingress()
    payload: dict[str, Any] = {
        "module": "lark",
        "ok": bool(report.get("tenant_token", {}).get("ok")),
        **report,
    }
    if not live:
        return payload
    env = _load_native_env(None)
    gate = ChannelNativeE2EGates.from_env(env).evaluate("lark")
    if not gate.enabled:
        payload["live"] = {"ok": False, "reason": gate.reason or "lark E2E gate disabled"}
        payload["ok"] = False
        return payload
    chat_id = str(env.get("WALKCODE_E2E_LARK_CHAT_ID", "") or "")
    channel = runtime.channels["lark"]
    binding = channel.binding_for(chat_id)
    view = {
        "type": "health",
        "status": "running",
        "title": "walkcode lark live gate",
        "session_id": f"e2e-{uuid.uuid4().hex[:8]}",
        "transport": "e2e",
        "elapsed": 0.0,
        "cwd": "-",
    }
    message_id = await channel.send_view(binding, view)
    edited = await channel.edit_view(binding, message_id, {**view, "status": "stopped"})
    payload["live"] = {
        "ok": bool(message_id and edited),
        "sent_message": bool(message_id),
        "patched": bool(edited),
    }
    payload["ok"] = payload["ok"] and payload["live"]["ok"]
    return payload


async def debug_telegram(*, limit: int) -> dict[str, Any]:
    runtime = ChannelNativeRuntime.from_env()
    report = await runtime.diagnose_telegram_ingress(limit=limit)
    process_report = debug_runtime_processes(allow_channel_native=True)
    warnings = list(report.get("warnings", []))
    running_service_owns_polling = (
        _telegram_pending_updates_conflict(report)
        and bool(process_report.get("ok"))
        and int(process_report.get("native_consumer_count") or 0) > 0
    )
    if running_service_owns_polling:
        report["polling_owned_by_running_service"] = True
        warnings = [item for item in warnings if item != "could not inspect Telegram pending updates"]
        warnings.append(
            "running walkcode native serve owns Telegram polling; getUpdates diagnostics are expected to return 409"
        )
    if not process_report["ok"]:
        report["safe_to_run_serve_once"] = False
        warnings.append("competing walkcode serve process(es) can consume Telegram updates before this run")
    return {
        "ok": (
            bool(report.get("bot", {}).get("ok"))
            and bool(report.get("webhook", {}).get("ok"))
            and (bool(report.get("safe_to_run_serve_once")) or running_service_owns_polling)
            and bool(process_report["ok"])
        ),
        **report,
        "runtime_processes": process_report,
        "warnings": warnings,
    }


def run_tests(module: str) -> int:
    paths = TEST_GROUPS[module]
    cmd = ["uv", "run", "--with", "pytest", "python", "-m", "pytest", *paths]
    print("+ " + " ".join(cmd), flush=True)
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


def _telegram_pending_updates_conflict(report: dict[str, Any]) -> bool:
    pending = report.get("pending_updates", {})
    if not isinstance(pending, dict):
        return False
    message = str(pending.get("message", "") or "")
    error = str(pending.get("error", "") or "")
    return "409" in message or "Conflict" in message or error == "Conflict"


def _load_state_snapshot(cfg: ChannelNativeConfig) -> tuple[StateSnapshot | None, dict[str, Any]]:
    path = Path(cfg.state_path).expanduser()
    exists = path.exists()
    if not exists:
        return _empty_snapshot(), {"ok": True, "exists": False, "load_ok": True}
    try:
        snapshot = JsonFileStateStore(path).load()
    except Exception as exc:
        return None, {
            "ok": False,
            "exists": True,
            "load_ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }
    return snapshot, {"ok": True, "exists": True, "load_ok": True}


def _empty_snapshot() -> StateSnapshot:
    return StateSnapshot(
        sessions=SessionRegistry(),
        interactions=InteractionStore(),
        outbox=DurableOutbox(),
        authz=AuthorizationStore(),
        inbound_ledger=InboundLedger(),
    )


def _probe_state_write(state_path: Path) -> dict[str, Any]:
    target = state_path.expanduser()
    probe_path = target.parent / f".walkcode-state-debug-{uuid.uuid4().hex}.json"
    try:
        store = JsonFileStateStore(probe_path)
        snapshot = _empty_snapshot()
        store.save(
            sessions=snapshot.sessions,
            interactions=snapshot.interactions,
            outbox=snapshot.outbox,
            authz=snapshot.authz,
            inbound_ledger=snapshot.inbound_ledger,
        )
        restored = store.load()
        counts = _snapshot_counts(restored)
    except Exception as exc:
        return {
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        probe_path.unlink(missing_ok=True)
    return {
        "ok": True,
        "created_configured_state": target.exists() and target == probe_path,
        "roundtrip_counts": counts,
    }


def _snapshot_counts(snapshot: StateSnapshot) -> dict[str, Any]:
    session_data = snapshot.sessions.to_dict()
    authz_data = snapshot.authz.to_dict()
    ledger_data = snapshot.inbound_ledger.to_dict()
    now = time.time()
    active_sessions = 0
    expired_writer_leases = 0
    for session in session_data.get("sessions", {}).values():
        if session.get("status") == "stopped":
            continue
        active_sessions += 1
        lifecycle_state = str(session.get("lifecycle_state", ""))
        if lifecycle_state in {"IDLE", "EXTERNAL_OBSERVED_READONLY"}:
            continue
        lease = session.get("writer_lease") or {}
        try:
            expires_at = float(lease.get("expires_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if not lease or expires_at <= now:
            expired_writer_leases += 1
    return {
        "sessions": len(session_data.get("sessions", {})),
        "active_sessions": active_sessions,
        "expired_writer_leases": expired_writer_leases,
        "pending_bindings": len(session_data.get("pending", {})),
        "interactions": snapshot.interactions.interaction_count(),
        "callback_tokens": snapshot.interactions.token_count(),
        "awaiting_other": snapshot.interactions.awaiting_other_count(),
        "outbox": _outbox_counts(snapshot.outbox),
        "auth_grants": len(authz_data.get("grants", [])),
        "auth_audit_events": len(authz_data.get("audit", [])),
        "inbound_completed": len(ledger_data.get("completed", {})),
        "inbound_in_progress": len(ledger_data.get("in_progress", {})),
    }


def _outbox_counts(outbox: DurableOutbox) -> dict[str, int]:
    return {
        "pending_count": outbox.pending_count(),
        "ready_pending_count": len(outbox.pending_items()),
        "sent_count": outbox.sent_count(),
        "dead_count": outbox.dead_count(),
    }


def _agent_smoke_error_events(events: Any) -> list[Any]:
    return [event for event in list(events or []) if getattr(event, "type", "") == AgentEventType.SESSION_ERROR]


def _agent_smoke_error_payload(events: Any) -> dict[str, Any]:
    errors = _agent_smoke_error_events(events)
    if not errors:
        return {}
    messages = [str(getattr(event, "payload", {}).get("message", "")) for event in errors]
    return {
        "error": "agent_session_error",
        "messages": [message for message in messages if message],
    }


async def _probe_outbox_dispatch() -> dict[str, Any]:
    binding = ChannelBinding(channel_kind="telegram", account_id="bot", chat_id="debug")
    sent_outbox = DurableOutbox()
    sent_outbox.enqueue(
        channel_binding_key=binding.key(),
        view_model={"type": "text", "text": "debug"},
        idempotency_key="debug:sent",
    )
    sent_channel = _DebugChannel("telegram", mode="sent")
    await OutboxDispatcher(sent_outbox, {"telegram": sent_channel}).flush_once()

    permanent_outbox = DurableOutbox()
    permanent_outbox.enqueue(
        channel_binding_key=binding.key(),
        view_model={"type": "text", "text": "debug"},
        idempotency_key="debug:permanent",
    )
    await OutboxDispatcher(
        permanent_outbox,
        {"telegram": _DebugChannel("telegram", mode="permanent")},
    ).flush_once()

    transient_outbox = DurableOutbox(max_attempts=1)
    transient_outbox.enqueue(
        channel_binding_key=binding.key(),
        view_model={"type": "text", "text": "debug"},
        idempotency_key="debug:transient",
    )
    await OutboxDispatcher(
        transient_outbox,
        {"telegram": _DebugChannel("telegram", mode="transient")},
    ).flush_once()

    result = {
        "ok": (
            sent_outbox.sent_count() == 1
            and permanent_outbox.dead_count() == 1
            and transient_outbox.dead_count() == 1
        ),
        "sent_count": sent_outbox.sent_count(),
        "permanent_dead_count": permanent_outbox.dead_count(),
        "transient_dead_count": transient_outbox.dead_count(),
        "send_calls": sent_channel.send_calls,
    }
    return result


def debug_runtime_processes(*, allow_channel_native: bool = False) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "current_pid": os.getpid(),
            "competing_consumer_count": 0,
            "competing_consumers": [],
            "error": type(exc).__name__,
            "message": str(exc),
        }
    if result.returncode != 0:
        return {
            "ok": False,
            "current_pid": os.getpid(),
            "competing_consumer_count": 0,
            "competing_consumers": [],
            "error": "ps_failed",
            "message": result.stderr.strip(),
        }
    launchd_labels = _launchctl_walkcode_service_labels()
    consumers = _parse_competing_consumers(
        result.stdout,
        current_pid=os.getpid(),
        launchd_labels=launchd_labels,
    )
    hard_consumers = [
        item
        for item in consumers
        if _is_hard_runtime_consumer(item, allow_channel_native=allow_channel_native)
    ]
    native_consumers = [item for item in consumers if item.get("kind") == "channel_native_serve"]
    managed_native_consumers = [
        item
        for item in native_consumers
        if _is_managed_channel_native_consumer(item)
    ]
    legacy_remnants = _detect_legacy_runtime_remnants()
    payload: dict[str, Any] = {
        "ok": len(hard_consumers) == 0 and len(legacy_remnants) == 0,
        "current_pid": os.getpid(),
        "expected_service_label": _expected_channel_native_service_label(),
        "competing_consumer_count": len(hard_consumers),
        "competing_consumers": hard_consumers,
        "native_consumer_count": len(native_consumers),
        "native_consumers": native_consumers,
        "managed_native_consumer_count": len(managed_native_consumers),
        "managed_native_consumers": managed_native_consumers,
        "legacy_remnant_count": len(legacy_remnants),
        "legacy_remnants": legacy_remnants,
        "warnings": [],
    }
    if hard_consumers:
        payload["warnings"].append("stop competing walkcode serve process(es) before consuming IM updates")
    elif payload["native_consumer_count"]:
        payload["warnings"].append(
            "walkcode native serve process(es) are running; Telegram 409/pending diagnostics are authoritative for same-bot conflicts"
        )
    if legacy_remnants:
        payload["warnings"].append("legacy walkcode launchers/hooks/wrappers detected; clean or migrate before V3 release validation")
    return payload


def _parse_competing_consumers(
    ps_output: str,
    *,
    current_pid: int,
    launchd_labels: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    consumers: list[dict[str, Any]] = []
    labels = launchd_labels or {}
    for raw_line in ps_output.splitlines():
        parts = raw_line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid == current_pid:
            continue
        command = parts[2]
        kind = _classify_consumer_command(command)
        if not kind:
            continue
        service_label = labels.get(pid, "")
        consumers.append(
            {
                "pid": pid,
                "ppid": ppid,
                "kind": kind,
                "command": "walkcode native serve" if kind == "channel_native_serve" else "walkcode serve",
                "service_label": service_label,
                "managed_by_launchd": bool(service_label),
            }
        )
    return consumers


def _launchctl_walkcode_service_labels() -> dict[int, str]:
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return {}
    if result.returncode != 0:
        return {}
    labels: dict[int, str] = {}
    for raw_line in result.stdout.splitlines():
        parts = raw_line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        label = parts[2].strip()
        if not label.startswith("com.walkcode.telegram-"):
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid > 0:
            labels[pid] = label
    return labels


def _expected_channel_native_service_label() -> str:
    try:
        config = ChannelNativeConfig.from_env(_load_native_env(None))
    except Exception:
        return ""
    if config.channel.kind != "telegram":
        return ""
    agent = str(config.agent or "").strip().lower()
    if agent not in {"claude", "codex"}:
        return ""
    return f"com.walkcode.telegram-{agent}"


def _is_hard_runtime_consumer(item: dict[str, Any], *, allow_channel_native: bool) -> bool:
    if item.get("kind") == "legacy_walkcode_serve":
        return True
    if item.get("kind") != "channel_native_serve":
        return True
    if allow_channel_native:
        return False
    return not _is_managed_channel_native_consumer(item)


def _is_managed_channel_native_consumer(item: dict[str, Any]) -> bool:
    label = str(item.get("service_label") or "")
    return item.get("kind") == "channel_native_serve" and label.startswith("com.walkcode.telegram-")


def _classify_consumer_command(command: str) -> str:
    tokens = command.split()
    for index, token in enumerate(tokens):
        if token == "-m" and index + 3 < len(tokens):
            if tokens[index + 1] == "walkcode" and tokens[index + 2] == "native" and tokens[index + 3] == "serve":
                return "channel_native_serve"
        if token == "-m" and index + 2 < len(tokens):
            if tokens[index + 1] == "walkcode" and tokens[index + 2] == "serve":
                return "legacy_walkcode_serve"
        if token.endswith("walkcode") and index + 2 < len(tokens):
            if tokens[index + 1] == "native" and tokens[index + 2] == "serve":
                return "channel_native_serve"
        if token.endswith("walkcode") and index + 1 < len(tokens):
            if tokens[index + 1] == "serve":
                return "legacy_walkcode_serve"
    return ""


def _detect_legacy_runtime_remnants(*, home: Path | None = None) -> list[dict[str, Any]]:
    root = Path.home() if home is None else home
    remnants: list[dict[str, Any]] = []

    launch_agents = root / "Library" / "LaunchAgents"
    for plist in sorted(launch_agents.glob("com.walkcode*.plist")):
        text = _read_text_if_exists(plist)
        if _launch_agent_runs_legacy_walkcode(plist, text):
            remnants.append(
                {
                    "kind": "legacy_launch_agent",
                    "path": _home_relative(root, plist),
                    "action": "unload or replace with walkcode native serve before sharing the bot/webhook",
                }
            )

    for hook_path in (root / ".claude" / "settings.json", root / ".codex" / "hooks.json"):
        text = _read_text_if_exists(hook_path)
        if "walkcode hook" in text and "walkcode native hook" not in text:
            remnants.append(
                {
                    "kind": "legacy_hook",
                    "path": _home_relative(root, hook_path),
                    "action": "replace walkcode hook with walkcode native hook for V3 TUI observation",
                }
            )

    wrapper_path = root / ".agent-control-plane" / "agent-wrappers.sh"
    wrapper_text = _read_text_if_exists(wrapper_path)
    wrapper_legacy_reasons = _legacy_shell_wrapper_reasons(wrapper_text)
    if wrapper_legacy_reasons:
        remnants.append(
            {
                "kind": "shell_wrapper",
                "path": _home_relative(root, wrapper_path),
                "evidence": ", ".join(wrapper_legacy_reasons),
                "action": "remove legacy shell wrapper behavior; V3 headless runtime must launch real binaries directly",
            }
        )
    if "--yolo" in wrapper_text and "codex" in wrapper_text:
        remnants.append(
            {
                "kind": "codex_wrapper_approval_override",
                "path": _home_relative(root, wrapper_path),
                "action": "move codex --yolo into an explicit alias instead of a default wrapper",
            }
        )

    for shell_rc_name in (".zshrc", ".zprofile", ".bashrc"):
        shell_rc = root / shell_rc_name
        shell_rc_text = _read_text_if_exists(shell_rc)
        if "agent-wrappers.sh" in shell_rc_text and wrapper_legacy_reasons:
            remnants.append(
                {
                    "kind": "shell_startup_wrapper_source",
                    "path": _home_relative(root, shell_rc),
                    "evidence": ", ".join(wrapper_legacy_reasons),
                    "action": "stop sourcing the legacy wrapper or replace it with the V3 pure pass-through helper",
                }
            )

    walkcode_dir = root / ".walkcode"
    for env_path in sorted(walkcode_dir.glob("*.env")):
        text = _read_text_if_exists(env_path)
        if "FEISHU_" in text:
            remnants.append(
                {
                    "kind": "legacy_feishu_env",
                    "path": _home_relative(root, env_path),
                    "action": "rename as legacy or convert to LARK_* only for the Lark channel adapter",
                }
            )

    remnants.extend(_detect_process_env_remnants(root=root, environ=os.environ))
    return remnants


def _legacy_shell_wrapper_reasons(text: str) -> list[str]:
    if not text:
        return []
    checks = [
        ("tmux", "tmux"),
        ("walkcode legacy command", _text_has_walkcode_command(text, {"hook", "serve", "start", "status", "test-inject"})),
        ("WALKCODE_PORT", "WALKCODE_PORT"),
        ("WALKCODE_INSTANCE", "WALKCODE_INSTANCE"),
        ("legacy codex env", ".walkcode/codex.env"),
        ("FEISHU_", "FEISHU_"),
    ]
    reasons: list[str] = []
    for label, needle in checks:
        if isinstance(needle, bool):
            matched = needle
        else:
            matched = needle in text
        if matched:
            reasons.append(label)
    return reasons


def _launch_agent_runs_legacy_walkcode(plist: Path, text: str) -> bool:
    args = _plist_program_arguments(plist)
    return _program_args_run_walkcode(args, {"serve", "start"}) or _text_has_walkcode_command(text, {"serve", "start"})


def _plist_program_arguments(plist: Path) -> list[str]:
    try:
        with plist.open("rb") as fh:
            data = plistlib.load(fh)
    except Exception:
        return []
    args = data.get("ProgramArguments", []) if isinstance(data, dict) else []
    if not isinstance(args, list):
        return []
    return [str(item) for item in args]


def _program_args_run_walkcode(args: list[str], subcommands: set[str]) -> bool:
    for index, token in enumerate(args):
        if _text_has_walkcode_command(token, subcommands):
            return True
        if token.endswith("walkcode") and index + 1 < len(args) and args[index + 1] in subcommands:
            return True
    return False


def _text_has_walkcode_command(text: str, subcommands: set[str]) -> bool:
    if not text:
        return False
    pattern = (
        r"(?:^|\s|/)(?:[\w.-]+/)*walkcode\s+("
        + "|".join(re.escape(item) for item in subcommands)
        + r")(?:\s|$)"
    )
    return bool(re.search(pattern, text))


def _detect_process_env_remnants(*, root: Path, environ: dict[str, str]) -> list[dict[str, Any]]:
    remnants: list[dict[str, Any]] = []
    if any(key.startswith("FEISHU_") and str(value).strip() for key, value in environ.items()):
        remnants.append(
            {
                "kind": "legacy_shell_feishu_env",
                "path": "process environment",
                "action": "start V3 with a clean shell env; use LARK_* only in a Lark runtime env file",
            }
        )
    for removed in (
        "WALKCODE_CHANNELS",
        "WALKCODE_PRIMARY_CHANNEL",
        "WALKCODE_TRANSPORTS",
        "WALKCODE_DEFAULT_TRANSPORT",
        "WALKCODE_DEFAULT_AGENT",
    ):
        if str(environ.get(removed, "") or "").strip():
            remnants.append(
                {
                    "kind": "removed_runtime_env",
                    "path": f"process environment:{removed}",
                    "action": "remove this pre-V3 env variable before running channel-native V3",
                }
            )
    env_file = str(environ.get("WALKCODE_ENV_FILE") or "").strip()
    if env_file:
        env_path = Path(env_file).expanduser()
        env_text = _read_text_if_exists(env_path)
        if "FEISHU_" in env_text:
            remnants.append(
                {
                    "kind": "legacy_env_file_selected",
                    "path": _home_relative(root, env_path),
                    "action": "point WALKCODE_ENV_FILE at a V3 Telegram/Lark env file instead of a FEISHU_* env",
                }
            )
        if env_text and "WALKCODE_AGENT" not in env_text and not str(environ.get("WALKCODE_AGENT", "") or "").strip():
            remnants.append(
                {
                    "kind": "missing_agent_binding",
                    "path": _home_relative(root, env_path),
                    "action": "set WALKCODE_AGENT=claude or WALKCODE_AGENT=codex so this bot has one agent identity",
                }
            )
    return remnants


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def _home_relative(home: Path, path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


class _DebugChannel:
    def __init__(self, kind: str, *, mode: str):
        self.kind = kind
        self.mode = mode
        self.send_calls = 0

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            thread_context=True,
            editable_message=True,
            interactive_message=True,
            interactive_update=True,
            private_callback_ack=True,
            toast_or_ephemeral_notice=False,
            force_reply=True,
            attachment_download=True,
            forum_or_topic=True,
            max_text_chars=4096,
            max_callback_payload_bytes=64,
        )

    async def send_view(self, _binding: ChannelBinding, _view_model: dict[str, Any]) -> str:
        self.send_calls += 1
        if self.mode == "permanent":
            raise PermanentDeliveryError("synthetic permanent failure")
        if self.mode == "transient":
            raise TransientDeliveryError("synthetic transient failure")
        return "debug-message"

    async def ack_callback(self, _inbound: Any) -> None:
        return None

    async def download_attachment(self, attachment: Any) -> Any:
        return attachment


def _transport_kind_for_agent(agent: str) -> str:
    if agent == "claude":
        return "claude_headless"
    if agent == "codex":
        return "codex_app_server"
    return ""


def print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print_text(payload)


def print_text(payload: dict[str, Any]) -> None:
    print(f"ok: {payload.get('ok')}")
    if payload.get("error"):
        print(f"error: {payload['error']}")
    if payload.get("channel"):
        channel = payload["channel"]
        print(f"channel: {channel.get('kind', '')}")
        for key in ("polling", "polling_enabled", "allowlist_configured", "allowlist_count"):
            if key in channel:
                print(f"{key}: {channel[key]}")
    if payload.get("agent"):
        print(f"agent: {payload['agent']}")
    if payload.get("agent_status"):
        item = payload["agent_status"]
        print(f"agent.available: {item.get('available')}")
    if "legacy_remnant_count" in payload:
        print(f"legacy_remnant_count: {payload.get('legacy_remnant_count')}")
        for item in payload.get("legacy_remnants", []):
            print(
                "legacy_remnant: "
                f"kind={item.get('kind')} "
                f"path={item.get('path', '')} "
                f"action={item.get('action', '')}"
            )
    if payload.get("state_file"):
        state_file = payload["state_file"]
        print(f"state_file.exists: {state_file.get('exists')}")
        print(f"state_file.load_ok: {state_file.get('load_ok')}")
    if payload.get("write_probe"):
        print(f"write_probe.ok: {payload['write_probe'].get('ok')}")
    if payload.get("counts"):
        counts = payload["counts"]
        print(f"sessions.count: {counts.get('sessions')}")
        print(f"sessions.active_count: {counts.get('active_sessions')}")
        print(f"sessions.expired_writer_leases: {counts.get('expired_writer_leases')}")
        outbox_counts = counts.get("outbox", {})
        print(f"outbox.pending_count: {outbox_counts.get('pending_count')}")
        print(f"outbox.dead_count: {outbox_counts.get('dead_count')}")
    if payload.get("outbox"):
        outbox = payload["outbox"]
        print(f"outbox.pending_count: {outbox.get('pending_count')}")
        print(f"outbox.ready_pending_count: {outbox.get('ready_pending_count')}")
        print(f"outbox.sent_count: {outbox.get('sent_count')}")
        print(f"outbox.dead_count: {outbox.get('dead_count')}")
    if payload.get("synthetic_dispatch"):
        synthetic = payload["synthetic_dispatch"]
        print(f"synthetic_dispatch.ok: {synthetic.get('ok')}")
        print(f"synthetic_dispatch.sent_count: {synthetic.get('sent_count')}")
    if payload.get("agent"):
        print(f"agent: {payload.get('agent')}")
        print(f"transport_kind: {payload.get('transport_kind')}")
        print(f"agent.available: {payload.get('available')}")
        print(f"agent.live: {payload.get('live')}")
    if "competing_consumer_count" in payload:
        print(f"competing_consumer_count: {payload.get('competing_consumer_count')}")
        for item in payload.get("competing_consumers", []):
            print(
                "competing_consumer: "
                f"pid={item.get('pid')} "
                f"ppid={item.get('ppid')} "
                f"kind={item.get('kind')} "
                f"command={item.get('command')}"
            )
    if payload.get("runtime_processes"):
        processes = payload["runtime_processes"]
        print(f"runtime.competing_consumer_count: {processes.get('competing_consumer_count')}")
        for item in processes.get("competing_consumers", []):
            print(
                "runtime.competing_consumer: "
                f"pid={item.get('pid')} "
                f"ppid={item.get('ppid')} "
                f"kind={item.get('kind')} "
                f"command={item.get('command')}"
            )
    if payload.get("bot"):
        bot = payload["bot"]
        print(f"bot.ok: {bot.get('ok')}")
        print(f"bot.username: {bot.get('username', '')}")
    if payload.get("webhook"):
        webhook = payload["webhook"]
        print(f"webhook.has_url: {webhook.get('has_url')}")
        print(f"webhook.pending_update_count: {webhook.get('pending_update_count')}")
    if payload.get("pending_updates"):
        pending = payload["pending_updates"]
        print(f"pending_updates.count: {pending.get('count')}")
        for item in pending.get("items", []):
            print(
                "pending_update: "
                f"index={item.get('index')} "
                f"chat_allowed={item.get('chat_allowed')} "
                f"known_chat={item.get('chat_matches_existing_session')} "
                f"active_session={item.get('active_session_present')} "
                f"submit_would_accept={item.get('submit_would_accept')} "
                f"submit_blocked_reason={item.get('submit_blocked_reason', '')} "
                f"text_present={item.get('text_present')}"
            )
    if "safe_to_run_serve_once" in payload:
        print(f"safe_to_run_serve_once: {payload['safe_to_run_serve_once']}")
    for warning in payload.get("warnings", []):
        print(f"warning: {warning}")


if __name__ == "__main__":
    main()
