"""Claude headless transport (Claude Agent SDK) and its permission bridge."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import re
import signal
import time
import uuid

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import claude_gate
from .models import (
    agent_session_id,
    AgentEvent,
    AgentEventType,
    attachment_download_dir,
    BlockedReason,
    CapabilityUnsupported,
    _compact_tool_summary,
    ControlResult,
    _humanize_seconds,
    LaunchSpec,
    _log_degrade,
    _maybe_await,
    ResumeSpec,
    TransportCapabilities,
    TransportHandle,
    TransportUnavailable,
    TurnInput,
)
from .process_control import _probe_process, _proc_identity_matches


def _options_supports_field(cls: Any, name: str) -> bool:
    """Whether an options class accepts ``name`` (dataclass field or kwarg).

    Guards forward/backward compatibility with the Claude Agent SDK: passing an
    unknown kwarg to the options constructor raises ``TypeError`` and would fail
    client creation, so optional kwargs are only supplied when supported.
    """
    fields = getattr(cls, "__dataclass_fields__", None)
    if isinstance(fields, dict) and name in fields:
        return True
    with contextlib.suppress(ValueError, TypeError):
        return name in inspect.signature(cls).parameters
    return False


def _sdk_block_field(content: Any, key: str) -> Any:
    if isinstance(content, dict):
        return content.get(key, "")
    return getattr(content, key, "")


# Tools that only read or observe are low-risk: any authorized collaborator may
# approve them. Everything else (writes, command execution, MCP tools, unknown
# tools) is treated as high-risk so approval is gated to owner/admin — matching
# the fail-safe posture of denying/escalating when the blast radius is unclear.
_CLAUDE_LOW_RISK_TOOLS = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "WebSearch",
        "NotebookRead",
        "TodoRead",
        "TodoWrite",
    }
)


def _claude_tool_is_high_risk(tool_name: str) -> bool:
    return str(tool_name or "") not in _CLAUDE_LOW_RISK_TOOLS


class _ClaudePermissionBridge:
    """Bridges the Claude Agent SDK ``can_use_tool`` callback to channel-native
    permission / AskUserQuestion events and back.

    The SDK invokes ``can_use_tool`` from a *separate* spawned task while its read
    loop keeps running (``query.py:_spawn_control_request_handler``), so the
    callback is free to ``await`` a Future that only resolves when a human taps a
    card button — the SDK layer never deadlocks. This bridge owns:

    - ``_queue``: floats ``PERMISSION_REQUESTED`` / ``ASK_USER_REQUESTED`` events
      into the transport's event stream so the orchestrator can post a card
      mid-turn instead of waiting for the turn to finish.
    - ``_pending``: rid -> Future the callback awaits and that
      ``approve_permission`` / ``answer_user_question`` resolve (write-once).
    - ``_entries``: rid -> request metadata used to build the SDK PermissionResult
      (kind, tool name, original input, ToolPermissionContext for suggestions).

    Fail-safe: on timeout, cancellation, or any error the pending decision
    resolves to *deny*. There is no terminal fallback here, so allowing an
    un-acknowledged tool would be an escalation — we never fail open.
    """

    _ASK_USER_TOOL_NAMES = frozenset({"AskUserQuestion", "ask_user_question"})

    def __init__(self, *, sdk: Any, timeout: float = 1800.0):
        self._sdk = sdk
        self._timeout = timeout
        self._queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}
        self._entries: dict[str, dict[str, Any]] = {}
        self._resolved: set[str] = set()

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], ctx: Any) -> Any:
        rid = str(getattr(ctx, "tool_use_id", "") or "") or f"perm-{uuid.uuid4().hex}"
        # Dedupe on (tool_use_id): a replayed callback for an already-settled rid
        # must not float a second card. Deny the replay fail-safe.
        if rid in self._resolved:
            return self._deny_result("Duplicate permission request")
        is_ask = str(tool_name or "") in self._ASK_USER_TOOL_NAMES
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        self._entries[rid] = {
            "kind": "ask_user_question" if is_ask else "permission",
            "tool_name": str(tool_name or ""),
            "tool_input": dict(tool_input or {}),
            "ctx": ctx,
        }
        await self._queue.put(self._build_event(rid, tool_name, dict(tool_input or {}), ctx, is_ask))
        try:
            decision = await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            decision = {"action": "deny", "reason": "timeout"}
        except asyncio.CancelledError:
            self._resolved.add(rid)
            self._pending.pop(rid, None)
            return self._result_from_decision(rid, {"action": "deny", "reason": "cancelled"})
        except Exception:
            decision = {"action": "deny", "reason": "error"}
        self._resolved.add(rid)
        self._pending.pop(rid, None)
        return self._result_from_decision(rid, decision)

    async def next_event(self) -> AgentEvent:
        return await self._queue.get()

    def drain_ready_events(self) -> list[AgentEvent]:
        events: list[AgentEvent] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    def has_pending(self, rid: str) -> bool:
        future = self._pending.get(rid)
        return future is not None and not future.done()

    def has_any_pending(self) -> bool:
        """A human decision is still outstanding (blocks stream settle)."""
        return any(not future.done() for future in self._pending.values())

    def resolve(self, rid: str, decision: dict[str, Any]) -> bool:
        """Write-once: only the first decision for an rid takes effect."""
        future = self._pending.get(rid)
        if future is None or future.done():
            return False
        future.set_result(dict(decision))
        return True

    def fail_pending_default_deny(self, reason: str = "aborted") -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_result({"action": "deny", "reason": reason})

    def _build_event(
        self,
        rid: str,
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: Any,
        is_ask: bool,
    ) -> AgentEvent:
        if is_ask:
            return AgentEvent(
                AgentEventType.ASK_USER_REQUESTED,
                {
                    "rid": rid,
                    "questions": self._map_ask_questions(tool_input),
                    "native_method": "can_use_tool",
                },
            )
        return AgentEvent(
            AgentEventType.PERMISSION_REQUESTED,
            {
                "rid": rid,
                "tool_name": str(tool_name or ""),
                "tool_input": tool_input,
                "actions": ["allow", "always_allow", "deny"],
                "high_risk": _claude_tool_is_high_risk(tool_name),
                "native_method": "can_use_tool",
                "title": str(getattr(ctx, "title", "") or ""),
                "description": str(getattr(ctx, "description", "") or ""),
            },
        )

    @staticmethod
    def _map_ask_questions(tool_input: dict[str, Any]) -> list[dict[str, Any]]:
        raw_questions = tool_input.get("questions")
        if not isinstance(raw_questions, list) or not raw_questions:
            return [{"prompt": str(tool_input.get("prompt", "") or ""), "options": [], "allow_other": True}]
        mapped: list[dict[str, Any]] = []
        for question in raw_questions:
            if not isinstance(question, dict):
                continue
            options: list[str] = []
            for option in question.get("options", []) or []:
                if isinstance(option, dict):
                    options.append(str(option.get("label", option.get("value", "")) or ""))
                else:
                    options.append(str(option))
            mapped.append(
                {
                    "prompt": str(
                        question.get("question")
                        or question.get("header")
                        or question.get("prompt")
                        or ""
                    ),
                    "header": str(question.get("header", "") or ""),
                    "options": options,
                    "allow_multiple": bool(question.get("multiSelect") or question.get("allow_multiple")),
                    "allow_other": True,
                }
            )
        if not mapped:
            mapped = [{"prompt": str(tool_input.get("prompt", "") or ""), "options": [], "allow_other": True}]
        return mapped

    def _result_from_decision(self, rid: str, decision: dict[str, Any]) -> Any:
        entry = self._entries.get(rid, {})
        allow_cls = getattr(self._sdk, "PermissionResultAllow", None)
        if allow_cls is None:
            return self._deny_result(str(decision.get("reason", "") or "denied"))
        if entry.get("kind") == "ask_user_question":
            answers = decision.get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
            return allow_cls(updated_input=self._build_ask_updated_input(entry, answers))
        action = str(decision.get("action", "deny"))
        if action in {"allow", "allow_once", "accept", "acceptForSession"}:
            return allow_cls()
        if action == "always_allow":
            # The CLI persists these at the scope the suggestion names. Do
            # not also write the bare tool name into the profile settings.json:
            # approving one command must not allow every Bash call everywhere.
            updates = self._always_allow_updates(entry)
            return allow_cls(updated_permissions=updates or None)
        return self._deny_result(str(decision.get("reason", "") or "Denied via WalkCode"))

    def _deny_result(self, message: str) -> Any:
        deny_cls = getattr(self._sdk, "PermissionResultDeny", None)
        if deny_cls is None:
            raise CapabilityUnsupported("Claude Agent SDK PermissionResultDeny is unavailable")
        return deny_cls(message=message or "Denied via WalkCode", interrupt=False)

    def _build_ask_updated_input(self, entry: dict[str, Any], answers: dict[Any, Any]) -> dict[str, Any]:
        # Shared with the cross-process PreToolUse gate (claude_gate): the
        # hook builds the same updatedInput payload from its decision file.
        return claude_gate.ask_updated_input(entry.get("tool_input", {}) or {}, answers)

    def _always_allow_updates(self, entry: dict[str, Any]) -> list[Any]:
        suggestions = list(getattr(entry.get("ctx"), "suggestions", []) or [])
        if suggestions:
            return suggestions
        update_cls = getattr(self._sdk, "PermissionUpdate", None)
        rule_cls = getattr(self._sdk, "PermissionRuleValue", None)
        tool_name = str(entry.get("tool_name", "") or "")
        if update_cls is None or rule_cls is None or not tool_name:
            return []
        return [
            update_cls(
                type="addRules",
                rules=[rule_cls(tool_name=tool_name)],
                behavior="allow",
                destination="localSettings",
            )
        ]


# What a turn carrying neither text nor a usable attachment becomes before it
# reaches an agent. An empty string must NEVER be submitted: codex persists it
# in the thread history, and every later turn replays it — the commandcode
# relay's Chat Completions upstream then rejects the WHOLE request with
# `400 user message must have content` (2026-08-07: one attachment-only Lark
# message bricked thread 019fd743 permanently, six silent retries per turn).
EMPTY_TURN_PLACEHOLDER = "（用户发来一条空消息）"

# Shown when a turn ends without ever putting anything in the channel — no
# delta, no tool card, no completion text. Dropping that completion silently
# is what made the 2026-08-07 relay outage invisible: the upstream answered
# `400 user message must have content` six times per turn, codex closed the
# turn with `last_agent_message: null`, and the channel showed nothing at all
# for hours. A turn that produced nothing must say so.
EMPTY_TURN_NOTICE = (
    "⚠️ 本轮代理没有返回任何内容（通常是模型/上游接口异常，例如 provider 或 "
    "relay 报错）。可以直接重发重试；连续多轮为空请查 agent provider 日志。"
)


def _compose_turn_text(turn: TurnInput) -> str:
    """Turn text plus the downloaded attachment paths, never empty.

    No transport has an attachment channel, so downloaded files are named by
    absolute path in the prompt for the agent to open with Read. Without this
    an attachment-only message reaches the agent as empty text.
    """
    paths = [
        str(a.local_path)
        for a in (turn.attachments or [])
        if getattr(a, "local_path", "")
    ]
    text = turn.text or ""
    if not paths:
        return text if text.strip() else EMPTY_TURN_PLACEHOLDER
    refs = "\n".join(f"- {p}" for p in paths)
    note = f"[用户发送了附件，已下载到本地，可用 Read 工具查看]\n{refs}"
    return f"{text}\n\n{note}" if text.strip() else note


class ClaudeHeadlessTransport:
    kind = "claude_headless"
    # Single stream-json message ceiling for the SDK subprocess transport
    # (default is 1 MiB and real turns exceed it); matches the codex
    # app-server _STDOUT_LIMIT rationale.
    _SDK_MAX_BUFFER_SIZE = 64 * 1024 * 1024

    def __init__(
        self,
        *,
        client_factory: Callable[[LaunchSpec], Any] | None = None,
        sdk_loader: Callable[[], Any] | None = None,
        settings: str | None = None,
        cli_path: str | None = None,
        config_dir: str | None = None,
        anthropic_base_url: str | None = None,
        permission_mode: str | None = None,
        permission_timeout: float = 1800.0,
        settle_grace_seconds: float = 5.0,
        background_wait_ceiling_seconds: float = 3600.0,
        environment_context: str = "",
    ):
        self._client_factory = client_factory
        self._sdk_loader = sdk_loader or self._default_sdk_loader
        self.settings = settings
        self.cli_path = cli_path
        self.config_dir = config_dir
        self.anthropic_base_url = anthropic_base_url
        self.permission_mode = permission_mode
        self.permission_timeout = permission_timeout
        # Appended to the agent's system prompt on every worker launch AND
        # resume: the user is on a remote chat channel and cannot see this
        # machine. Covers native channel sessions and post-takeover resumes
        # alike, because both paths build their client here.
        self.environment_context = environment_context
        # Session-level settle policy (persistent listening): after a turn
        # ends, the event stream stays attached while background subagents run.
        # It only "goes off duty" when the ledger is empty and the stream stays
        # quiet for settle_grace_seconds, or — with tasks still pending but no
        # traffic at all — when background_wait_ceiling_seconds elapses
        # (0 disables the ceiling: wait forever).
        self.settle_grace_seconds = settle_grace_seconds
        self.background_wait_ceiling_seconds = background_wait_ceiling_seconds
        self._clients: dict[str, Any] = {}
        self._bridges: dict[str, _ClaudePermissionBridge] = {}
        # handle_id -> (pid, lstart, command)：worker 进程的捕获时身份。
        # 关闭 SDK client 不保证 CLI 进程退出（还挂着后台子进程的 worker 能
        # 平安活过一次"成功"的 disconnect），残留进程占着 Claude Code 的
        # 同会话单进程锁（终端 resume 秒退）且是潜在双写（2026-07-20 实锤：
        # 一个会话攒了三个残留 worker）。关闭路径据此验尸、按身份升级清理。
        self._worker_procs: dict[str, tuple[int, str, str]] = {}
        # handle_id -> 单例验尸任务：shield 起来跑，调用方被取消也要跑完；
        # 同一 handle 并发关闭共享同一个任务，不重复发信号。
        self._exit_tasks: dict[str, asyncio.Task] = {}
        # session_id -> 最近一次拉起的 worker handle：EOF 清算会先注销
        # _session_handles，换代 resume 据此仍能等到旧进程死透再拉新的。
        self._session_last_worker: dict[str, str] = {}
        # session_id -> 串行锁：关旧→验尸→建新→登记→捕获必须原子，否则
        # 并发 resume 双双越过闸门、各自拉起一个 worker（审查 R2 复现）。
        self._session_locks: dict[str, asyncio.Lock] = {}
        # session_id -> live handle_id, so resume() can close the previous
        # worker instead of leaking it, and the orchestrator can reuse a live
        # worker instead of forking a second --resume process.
        self._session_handles: dict[str, str] = {}
        # handle_id -> count of submitted turns not yet accounted for by a
        # stream result. Counter semantics (not a timestamp): each submit_turn
        # increments; the listener decrements when a NON-injected turn
        # completes. Timestamps proved un-attributable — a result can belong
        # to a CLI-injected turn or to an earlier turn, and generator yields
        # suspend at arbitrary points, so time comparison mis-credited results
        # to queued submits (2026-07-18 takeover incident + review round).
        self._pending_turns: dict[str, int] = {}
        # handle_id -> monotonic time of the latest submit; feeds the
        # pending-turn ceiling clock AND the absorbed-submit discrimination
        # at the settlement points (see _bridged_event_stream, ADR 0059 R2).
        self._last_submit_monotonic: dict[str, float] = {}
        # handle_id -> submits currently awaiting the async client write.
        # Their _pending_turns increment is already visible (settle safety),
        # but they must NEVER count as absorption candidates and no silent
        # absorbed settlement may run while one is in flight: an unconfirmed
        # submit has an unknown fate, and a failed one rolls back only the
        # counter — candidates computed from it would then cover a REAL
        # queued message (deep-review round 2, ADR 0059 R2).
        self._inflight_submits: dict[str, int] = {}

    def capabilities(self) -> TransportCapabilities:
        available = self._available()
        return TransportCapabilities(
            structured_input=available,
            structured_output=available,
            permission_callback=available,
            ask_user_question=available,
            set_model=available,
            resume_after_complete=available,
            external_tui_takeover=available,
        )

    async def launch_session(self, *, cwd: str, session_id: str) -> TransportHandle:
        return await self.launch(LaunchSpec(cwd=cwd, session_id=session_id))

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        if not self._available():
            raise TransportUnavailable("claude_agent_sdk is not installed or no client factory is configured")
        async with self._session_lock(spec.session_id):
            client, bridge = self._create_client(spec)
            await self._connect_client(client)
            handle = TransportHandle(
                handle_id=f"claude-{uuid.uuid4().hex}",
                transport_kind=self.kind,
                ref={
                    "session_id": spec.session_id,
                    # Stable WalkCode-local id for log correlation: after a
                    # resume, "session_id" drifts to the agent-native id
                    # (ADR 0059 R2 observability review).
                    "walkcode_session_id": spec.session_id,
                    "cwd": spec.cwd,
                },
            )
            self._clients[handle.handle_id] = client
            if bridge is not None:
                self._bridges[handle.handle_id] = bridge
            self._session_handles[spec.session_id] = handle.handle_id
            await self._capture_worker_proc(handle.handle_id, spec.session_id, client)
            return handle

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        if not self._available():
            raise TransportUnavailable("claude_agent_sdk is not installed or no client factory is configured")
        resume_id = agent_session_id("claude_headless", spec.resume_ref)
        if not resume_id:
            raise CapabilityUnsupported("Claude headless resume requires an agent session id")
        # Close-old → verify-dead → create-new → register → capture must be
        # ATOMIC per session: two concurrent resumes both slipping past the
        # barrier would each spawn a worker (review R2 reproduced it).
        async with self._session_lock(spec.session_id):
            # A resume replaces this session's worker: close the previous
            # client (and its subprocess) instead of leaking it forever.
            previous_handle_id = self._session_handles.get(spec.session_id, "")
            if previous_handle_id:
                await self._close_handle_client(previous_handle_id)
            # The EOF/settle path unregisters _session_handles BEFORE its
            # disconnect+exit-verify runs (or that verify got cancelled): wait
            # for the session's LAST KNOWN worker too.
            lingering_handle_id = self._session_last_worker.get(spec.session_id, "")
            if lingering_handle_id and lingering_handle_id != previous_handle_id:
                await self._disconnect_client(lingering_handle_id, None)
            # The barrier must end in a CONFIRMED terminal state. A record
            # that survived (probe error / unkillable process) means the old
            # worker may still hold the session file — spawning next to it
            # recreates the double-writer incident. Refuse; the next message
            # retries the whole ladder.
            for blocked_handle in (previous_handle_id, lingering_handle_id):
                if blocked_handle and blocked_handle in self._worker_procs:
                    raise TransportUnavailable(
                        "previous claude worker not confirmed dead; retry later"
                    )
            client, bridge = self._create_client(
                LaunchSpec(cwd=spec.cwd, session_id=spec.session_id),
                resume_id=resume_id,
            )
            await self._connect_client(client)
            resumed_session_id = resume_id or spec.session_id
            handle = TransportHandle(
                handle_id=f"claude-{uuid.uuid4().hex}",
                transport_kind=self.kind,
                ref={
                    "session_id": resumed_session_id,
                    "agent_session_id": resumed_session_id,
                    # Stable WalkCode-local id for log correlation (the
                    # session_id key must stay agent-native: --resume and
                    # other consumers read it).
                    "walkcode_session_id": spec.session_id,
                    "cwd": spec.cwd,
                },
            )
            self._clients[handle.handle_id] = client
            if bridge is not None:
                self._bridges[handle.handle_id] = bridge
            self._session_handles[spec.session_id] = handle.handle_id
            await self._capture_worker_proc(handle.handle_id, spec.session_id, client)
            return handle

    @staticmethod
    def _client_worker_pid(client: Any) -> int:
        """Best-effort pid of the SDK client's CLI subprocess (0 if unknown)."""
        transport = getattr(client, "_transport", None)
        process = getattr(transport, "_process", None)
        pid = getattr(process, "pid", None)
        try:
            value = int(pid) if pid is not None else 0
        except (TypeError, ValueError):
            return 0
        return value if value > 1 else 0

    async def _capture_worker_proc(self, handle_id: str, session_id: str, client: Any) -> None:
        pid = self._client_worker_pid(client)
        if not pid:
            return
        probe = None
        for attempt in range(3):
            if attempt:
                await asyncio.sleep(0.2)
            probe = await asyncio.to_thread(_probe_process, pid)
            if probe.status != "error":
                break
        if probe is None or probe.status != "ok":
            # Untracked worker == pre-ADR-0056 behavior for this one process
            # (fail-closed: we never signal what we cannot identify). Loud in
            # the logs so an environment where ps keeps failing is visible.
            _log_degrade(
                "headless_worker_capture_failed",
                handle_id=handle_id,
                pid=pid,
                status=getattr(probe, "status", "none"),
            )
            return
        if handle_id not in self._clients:
            # Closed while we were probing: recording it now would resurrect
            # tracking for a handle the close path already retired.
            return
        self._worker_procs[handle_id] = (pid, probe.lstart, probe.command)
        self._session_last_worker[session_id] = handle_id

    def handle_is_live(self, handle_id: str) -> bool:
        """True while this handle's worker client is still attached."""
        return bool(handle_id) and handle_id in self._clients

    def _unregister_handle(self, handle_id: str) -> tuple[Any, Any]:
        """Synchronously detach a handle from all registries.

        Must stay await-free: the settle path calls this at the decision point
        so a concurrent submit immediately sees handle_is_live() == False and
        raises TransportUnavailable (triggering the resume fallback) instead of
        writing into a worker that is about to be disconnected.
        """
        client = self._clients.pop(handle_id, None)
        bridge = self._bridges.pop(handle_id, None)
        self._pending_turns.pop(handle_id, None)
        self._last_submit_monotonic.pop(handle_id, None)
        self._inflight_submits.pop(handle_id, None)
        for session_id, mapped in list(self._session_handles.items()):
            if mapped == handle_id:
                self._session_handles.pop(session_id, None)
        return client, bridge

    async def _disconnect_client(self, handle_id: str, client: Any) -> None:
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                # Logged, not raised: the process verify below still runs and
                # escalates if the CLI survived the failed close.
                _log_degrade(
                    "headless_worker_close_failed",
                    handle_id=handle_id,
                    error=exc,
                )
        # A "successful" close only closed the SDK-side pipes — verify the CLI
        # process actually died, whether or not any close method worked. The
        # verify runs as a per-handle SINGLETON task behind a shield: a
        # cancelled caller must not disarm the cleanup, and concurrent closes
        # of the same handle must not double-signal.
        task = self._exit_tasks.get(handle_id)
        if task is None or task.done():
            task = asyncio.create_task(self._ensure_worker_process_exited(handle_id))
            self._exit_tasks[handle_id] = task
            task.add_done_callback(lambda _t, _h=handle_id: self._clear_exit_task(_h, _t))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A crashed verify must not replace/amplify the caller's own
            # exception path (EOF finally, stream errors).
            _log_degrade(
                "headless_worker_exit_verify_crashed",
                handle_id=handle_id,
                error=exc,
            )

    def _clear_exit_task(self, handle_id: str, task: "asyncio.Task") -> None:
        # Identity-conditional: an old task's late callback must not evict a
        # NEWER task registered for the same handle (ABA race, review R2).
        if self._exit_tasks.get(handle_id) is task:
            self._exit_tasks.pop(handle_id, None)

    def _resolve_worker_proc(self, handle_id: str) -> None:
        """The worker reached a terminal state: retire its tracking records."""
        self._worker_procs.pop(handle_id, None)
        for session_id, mapped in list(self._session_last_worker.items()):
            if mapped == handle_id:
                self._session_last_worker.pop(session_id, None)

    async def _ensure_worker_process_exited(self, handle_id: str) -> None:
        """Verify the worker CLI process died after close; escalate if not.

        A worker with lingering background children survives a clean
        disconnect: the leftover keeps the Claude session file's
        single-process lock (a terminal `claude --resume` exits on startup)
        and remains a latent double-writer (live incident 2026-07-20: one
        session accumulated three such leftovers). ADR 0053 identity rules
        apply — never signal on a probe error or an identity mismatch (pid
        reuse); a fresh healthy worker exits within the grace window and is
        never signalled at all.
        """
        record = self._worker_procs.get(handle_id)
        if record is None:
            return
        pid, lstart, command = record

        async def _observe() -> str:
            """gone | live | reused | error."""
            probe = await asyncio.to_thread(_probe_process, pid)
            if probe.status == "gone":
                return "gone"
            if probe.status == "error":
                return "error"
            if not _proc_identity_matches(probe, lstart, command):
                return "reused"  # pid recycled: the worker IS gone
            return "live"

        def _finish(state: str) -> None:
            if state in {"gone", "reused"}:
                # Terminal: the worker no longer exists — retire the record.
                self._resolve_worker_proc(handle_id)
                return
            # error / survived: KEEP the record so a later close attempt (the
            # resume barrier, a retried shutdown) re-verifies instead of being
            # permanently disarmed by one transient ps failure.
            if state == "error":
                _log_degrade(
                    "headless_worker_exit_verify_failed",
                    handle_id=handle_id,
                    pid=pid,
                )
            else:
                _log_degrade("headless_worker_survived_kill", handle_id=handle_id, pid=pid)

        # Grace: a healthy CLI exits promptly once its pipes close.
        deadline = time.monotonic() + 1.5
        while True:
            state = await _observe()
            if state != "live":
                _finish(state)
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        for sig, wait_seconds in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 1.5)):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                _finish("gone")
                return
            except OSError as exc:
                _log_degrade(
                    "headless_worker_kill_failed",
                    handle_id=handle_id,
                    pid=pid,
                    error=exc,
                )
                return
            _log_degrade(
                "headless_worker_terminated_after_close",
                handle_id=handle_id,
                pid=pid,
                signal=int(sig),
            )
            waited = 0.0
            while waited < wait_seconds:
                await asyncio.sleep(0.25)
                waited += 0.25
                state = await _observe()
                if state != "live":
                    _finish(state)
                    return
        _finish("survived")

    async def _close_handle_client(self, handle_id: str) -> None:
        client, bridge = self._unregister_handle(handle_id)
        if bridge is not None:
            bridge.fail_pending_default_deny(reason="worker_closed")
        await self._disconnect_client(handle_id, client)

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        client = self._clients.get(handle.handle_id)
        if client is None:
            # The worker settled (listener closed it) or the runtime restarted
            # between the caller's liveness check and this submit. Raising the
            # typed error lets the orchestrator fall back to a fresh resume.
            raise TransportUnavailable("claude headless worker is gone (settled or restarted)")
        # Every accepted submit counts — including mid-turn ones. A mid-turn
        # submit has TWO possible fates the CLI never discloses at submit
        # time: absorbed into the running turn (one result covers several
        # submits) or queued as its own steering turn (one result each). The
        # counter must stay conservative here so settle can never close the
        # worker under a queued steering turn (2026-07-18 takeover incident);
        # phantom leftovers from absorbed submits are recognized and cleared
        # at the settlement points instead (ceiling / EOF discrimination in
        # _bridged_event_stream, ADR 0059 R2).
        self._pending_turns[handle.handle_id] = self._pending_turns.get(handle.handle_id, 0) + 1
        # Timestamp for the pending-turn ceiling clock and the settlement-
        # point absorption recency check (never for per-result attribution —
        # results are accounted by counters): a fresh submit must get the
        # full ceiling even when the stream has already been quiet for a
        # long time (background tasks). NOTE: stamped before the async
        # submit is awaited — the absorption predicates therefore never
        # trust this timestamp alone (ADR 0059 R2).
        previous_submit_ts = self._last_submit_monotonic.get(handle.handle_id)
        this_submit_ts = time.monotonic()
        self._last_submit_monotonic[handle.handle_id] = this_submit_ts
        # Mark the submit as in flight until the async client write returns:
        # the drain loop must neither count it as an absorption candidate nor
        # run a silent absorbed settlement while its fate is unknown (ADR
        # 0059 R2, deep-review round 2).
        self._inflight_submits[handle.handle_id] = (
            self._inflight_submits.get(handle.handle_id, 0) + 1
        )
        try:
            # query(text) has no attachment channel, so downloaded files are
            # named by absolute path in the prompt for Claude to open with
            # Read. Without this an attachment-only message reaches Claude as
            # empty text. Called exactly once: a retry on TypeError would send
            # the user message twice when the TypeError came from inside it.
            await client.query(_compose_turn_text(turn))
        except BaseException:
            # A failed submit must not leave a "turn in flight" marker behind:
            # the persistent listener would wait for a turn that never started
            # and never settle. The ceiling timestamp also rolls back — a
            # failed attempt must not extend an OLDER pending submit's
            # timeout window (review round 3).
            remaining = self._pending_turns.get(handle.handle_id, 1) - 1
            if remaining > 0:
                self._pending_turns[handle.handle_id] = remaining
            else:
                self._pending_turns.pop(handle.handle_id, None)
            if self._last_submit_monotonic.get(handle.handle_id) == this_submit_ts:
                # CAS-style rollback: only undo OUR write — an interleaved
                # concurrent submit that succeeded after us must keep its own
                # (newer) clock.
                if previous_submit_ts is None:
                    self._last_submit_monotonic.pop(handle.handle_id, None)
                else:
                    self._last_submit_monotonic[handle.handle_id] = previous_submit_ts
            raise
        finally:
            inflight = self._inflight_submits.get(handle.handle_id, 0) - 1
            if inflight > 0:
                self._inflight_submits[handle.handle_id] = inflight
            else:
                self._inflight_submits.pop(handle.handle_id, None)

    def handle_supports_reuse(self, handle_id: str) -> bool:
        """True when the handle's worker can accept another turn in place.

        Every live worker runs a session-level listener (receive_messages), so
        a turn submitted into it is always observed.
        """
        return self.handle_is_live(handle_id)

    async def events(self, handle: TransportHandle):
        client = self._clients.get(handle.handle_id)
        if client is None:
            # The worker was closed (settle, shutdown) between the submit and
            # this drain starting; a typed error lets the caller treat it as
            # a benign race instead of crashing on KeyError.
            raise TransportUnavailable("claude headless worker is gone (settled or restarted)")
        # Session-level listener. With a bridge, mid-turn permission /
        # AskUserQuestion cards float before the turn ends. The stream does
        # NOT stop at the first turn's result — background subagents keep
        # opening new turns (task notifications), and their messages must
        # reach the channel. It ends only when the session settles (no open
        # turn, task ledger empty, no pending HITL, quiet grace elapsed), when
        # the background-wait ceiling fires, or when the worker dies.
        return self._bridged_event_stream(handle, client, self._bridges.get(handle.handle_id))

    # A terminal task_notification predicts a CLI-injected follow-up turn; the
    # listener must not settle before the injection had a fair chance to land
    # (bounded, so a notification with no follow-up cannot hang the stream).
    _NOTIFICATION_FOLLOWUP_GRACE = 30.0
    # With a HITL decision pending the wait is bounded to this recheck period
    # instead of infinite: a resolve() only completes a Future and does not
    # wake the stream, so the loop must re-derive its state periodically.
    _PENDING_DECISION_RECHECK_SECONDS = 60.0
    # ADR 0059 R2: a settlement point may only classify leftover pending as
    # ABSORBED when the latest terminal result (injected ones included — a
    # queued message cannot run while ANY turn occupies the worker) is at
    # least this old. A genuinely queued steering turn opens within seconds
    # of the previous result, and the drain's yield-suspension can stamp a
    # stale result AFTER a racing submit — a settlement close to a result
    # is therefore ambiguous and must keep the alarm / pending_turn_lost
    # path (visible error + ADR 0058 replay decision), matching pre-R2
    # behavior. This floor is deliberately independent of the configurable
    # background_wait_ceiling_seconds so a shortened ceiling cannot weaken
    # data-safety semantics. 300s, not 30s: a queued turn's first stream
    # message is subject to first-token latency, which stretches past 30s
    # exactly during model-API brownouts — the same windows in which workers
    # die (2026-07-20 incident), so the two failure conditions correlate.
    # Duplicate execution of a truly absorbed message inside this window is
    # accepted over a silent drop.
    _ABSORBED_MIN_RESULT_AGE_SECONDS = 300.0

    async def _bridged_event_stream(
        self,
        handle: TransportHandle,
        client: Any,
        bridge: _ClaudePermissionBridge | None,
    ):
        """Yield SDK events while concurrently floating bridge permission events.

        The SDK message stream and the bridge's permission queue are awaited
        together with ``FIRST_COMPLETED`` so neither starves the other: while
        ``can_use_tool`` is blocked inside the SDK (waiting on a card decision),
        the message stream is naturally idle, yet the floated permission event
        still surfaces immediately. Once the human decides, ``approve_permission``
        resolves the Future, the SDK resumes, and the message stream yields the
        tool result and the turn's completion within this same pass.

        Session-level lifetime: the ``receive_messages`` stream is persistent
        across turns. A ResultMessage only closes the *turn*;
        background subagents launched with run_in_background keep working and
        the CLI auto-opens new turns when they notify. The stream tracks those
        subagents in a ledger (task_started adds, task_notification /
        task_updated with a terminal status removes, background_tasks_changed
        reconciles) and terminates only when:

        - settle: no open turn, ledger empty, no pending HITL decision, and the
          stream stays quiet for ``settle_grace_seconds``; or
        - ceiling: the ledger is non-empty but nothing at all arrived for
          ``background_wait_ceiling_seconds`` (a stuck subagent must not hold
          the worker open forever) — a visible warning event is emitted first;
        - EOF: the worker process exited.

        On any of those the worker client is closed and unregistered, so the
        next user message resumes a fresh process via ``--resume``.
        """
        stream_iter = client.receive_messages().__aiter__()
        msg_task: asyncio.Future | None = asyncio.ensure_future(stream_iter.__anext__())
        queue_task: asyncio.Future | None = (
            asyncio.ensure_future(bridge.next_event()) if bridge is not None else None
        )
        active_tasks: dict[str, dict[str, Any]] = {}
        turn_open = False
        # ADR 0058 traffic_seen 的真源：只统计**非注入**回合的流量（注入
        # 回合的输出不代表排队中的用户消息动过手，R2 复现：注入回合输出
        # + 排队提交 + EOF 会误判"已部分执行"而拒绝安全重放）。浮出的
        # 权限事件也计入——获准的工具可能已产生副作用，宁可错杀重放。
        # 非注入回合终局时清零（该回合已被计数核销）。
        user_turn_traffic = False
        # Submit accounting (live-confirmed 2026-07-18 takeover incident +
        # review round): each submit_turn increments _pending_turns; the
        # result of a NON-injected turn decrements it. A turn is "injected"
        # (CLI-initiated: notification replay after a takeover resume, etc.)
        # when its opening stream traffic is a user-role message — submitted
        # prompts are never echoed back on the stream — or when a
        # task_notification arrives between turns and predicts one. Injected
        # turns never account for submits, so a queued user turn can't be
        # mis-credited and killed by settle. Counters, not timestamps: results
        # are un-attributable by time (generator yields suspend arbitrarily,
        # injected turns interleave), and a counter also represents several
        # queued submits at once.
        current_turn_injected = False
        # Sticky prediction: a between-turns notification announces that the
        # NEXT turn will be CLI-injected — regardless of its opening traffic
        # (the injected turn usually streams assistant text, which must not
        # reclassify it as a user turn). Expires with the injection window if
        # no turn materializes.
        injected_turn_expected = False
        # ADR 0059 R2: absorption evidence for phantom pending markers. A
        # mid-turn submit may be ABSORBED into the running turn by the CLI
        # (one result covers several submits) instead of queuing its own
        # steering turn — indistinguishable at submit time, so the counter
        # stays conservative and can leak a phantom pending. Discrimination
        # happens at the settlement points (ceiling / EOF) and ONLY for
        # leftovers that are absorption CANDIDATES:
        # - pending_at_turn_open snapshots the counter when a NON-injected
        #   turn is observed opening; only submits ABOVE that floor arrived
        #   mid-turn and can have been absorbed. Submits queued before the
        #   turn opened own their own future turns and stay fully protected
        #   (review: two pre-queued submits + one result must still alarm).
        # - absorbable_pending MERGES at each non-injected TURN_COMPLETED
        #   result (min(leftover, carried + this turn's confirmed mid-turn
        #   submits)); SHRINKS BY ONE at every non-injected turn open (the
        #   opened turn consumed one marker — worst case a candidate, since
        #   candidates carry no identity); and is ZEROED by ANY turn ending
        #   in SESSION_ERROR, injected included (an aborted turn proves
        #   nothing). In-flight submits (awaiting the client write) never
        #   become candidates.
        # - last_accounted_result_at is the drain-side consumption time of
        #   the latest accounted TURN_COMPLETED result. It is deliberately
        #   NOT trusted alone (yield-suspension makes time attribution
        #   unsound — see the accounting comment below): it only feeds the
        #   belt-and-braces recency checks next to the counters.
        # Injected results never account submits and never create candidates
        # (takeover incident semantics — a leftover behind one must keep
        # alarming/replaying); they DO refresh last_turn_terminal_at, since
        # a queued message cannot run while any turn holds the worker.
        last_accounted_result_at = 0.0
        absorbable_pending = 0
        pending_at_turn_open: int | None = None
        # ANY terminal result (injected included) refreshes this: a queued
        # message cannot run while a turn occupies the worker, so absorption
        # age must be measured from the LAST turn end, not just the last
        # accounted one (round 2: long injected turn after the candidate).
        last_turn_terminal_at = 0.0
        # Task lifecycle messages (task_started/progress/updated/…) are
        # handled by an early-continue branch that never touches the turn
        # state — but they ARE worker activity: a queued turn may be running
        # and visible ONLY through them (final verify panel: task-only
        # traffic then EOF silently cleared a genuinely running submit).
        # They feed the absorption age basis alongside the terminal clock.
        last_task_activity_at = 0.0
        # Observation clock for the EOF basis: when the stream wait returns
        # ~instantly the EOF was already buffered while this generator was
        # suspended in a yield (channel send, status card), so its true
        # arrival is unknowable — the conservative basis is then the moment
        # the PREVIOUS message was observed, not the resume time (round 2:
        # a >30s delivery hang must not launder a fresh EOF into an old one).
        last_msg_observed_at = 0.0
        eof_observation_basis = 0.0
        # Base for the background-wait ceiling: reset by any stream traffic.
        quiet_since = time.monotonic()
        # Non-zero after a terminal task_notification: hold off settle until
        # the CLI-injected follow-up turn had a fair chance to appear.
        injection_hold_until = 0.0
        settled = False
        stream_failed = False
        # (client, bridge) captured by the synchronous unregister at the settle
        # decision point; disconnected in finally.
        closing: tuple[Any, Any] | None = None
        try:
            while True:
                if msg_task is None:
                    # SDK stream is exhausted (worker exited). Flush any
                    # residual floated permission events, then stop.
                    if bridge is not None:
                        for event in bridge.drain_ready_events():
                            yield event
                    settled = True
                    # Capture BEFORE unregistering clears it: submits that
                    # never produced any stream traffic died with the worker
                    # and must not vanish silently.
                    pending_lost = self._pending_turns.get(handle.handle_id, 0)
                    # ADR 0059 R2: read BEFORE unregister pops the timestamp.
                    # Leftover pending may be a phantom from mid-turn submits
                    # ABSORBED into an already-accounted turn — reporting it
                    # as pending_turn_lost would auto-replay (ADR 0058) and
                    # RE-EXECUTE an already-processed message. Absorption is
                    # only accepted with ALL the evidence lined up: every
                    # leftover is a mid-turn candidate, no open turn and no
                    # unaccounted turn traffic contradicts it, the covering
                    # result postdates the last submit, and the result is old
                    # enough (_ABSORBED_MIN_RESULT_AGE_SECONDS) that a
                    # queued steering turn would already have opened. Any
                    # doubt keeps the pre-R2 pending_turn_lost path: a
                    # duplicate execution is recoverable, a silent drop on
                    # the fire-and-forget Lark ingress is not.
                    eof_last_submit_ts = self._last_submit_monotonic.get(
                        handle.handle_id, float("inf")
                    )
                    # Frozen decision values — the log below runs after yields
                    # and must record what the guard actually saw (round 3).
                    eof_decision_now = time.monotonic()
                    # Same total-silence basis as the ceiling side: any
                    # stream message counts as activity (final verify pass 6).
                    eof_absorption_age = eof_observation_basis - max(
                        last_accounted_result_at,
                        last_turn_terminal_at,
                        last_task_activity_at,
                        last_msg_observed_at,
                    )
                    pending_absorbed = (
                        pending_lost > 0
                        and not turn_open
                        and not user_turn_traffic
                        and self._inflight_submits.get(handle.handle_id, 0) == 0
                        and absorbable_pending >= pending_lost
                        and last_accounted_result_at > eof_last_submit_ts
                        and eof_absorption_age >= self._ABSORBED_MIN_RESULT_AGE_SECONDS
                    )
                    closing = self._unregister_handle(handle.handle_id)
                    if active_tasks:
                        # The worker died with subagents still on the books:
                        # say so visibly and clear the session ledger, or
                        # the status card would show phantom background
                        # work forever.
                        abandoned = len(active_tasks)
                        _log_degrade(
                            "headless_worker_eof_with_background_tasks",
                            handle_id=handle.handle_id,
                            pending_tasks=abandoned,
                        )
                        yield AgentEvent(
                            AgentEventType.TURN_DELTA,
                            {
                                "text": (
                                    f"⚠️ 代理进程已退出，仍有 {abandoned} 个后台任务未完成，"
                                    "它们的结果不会自动送达。你可以直接回复继续对话。"
                                )
                            },
                        )
                        # Close the synthetic warning turn (same rule as
                        # the ceiling path) so the ended stream is not
                        # misread as a mid-turn failure.
                        yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""})
                        yield AgentEvent(
                            AgentEventType.BACKGROUND_TASKS,
                            {"count": 0, "tasks": [], "abandoned": abandoned, "reason": "worker_eof"},
                        )
                    elif bridge is not None and bridge.has_any_pending():
                        # The worker died while a question/permission card
                        # was still waiting for the human: without a
                        # visible signal the session parks in WAITING_*
                        # with a card that can never be answered.
                        yield AgentEvent(
                            AgentEventType.SESSION_ERROR,
                            {
                                "message": (
                                    "代理进程在等待你回答时退出了，上面的卡片已失效；"
                                    "直接回复即可继续。"
                                )
                            },
                        )
                    if pending_lost > 0 and pending_absorbed:
                        # Phantom leftover from absorbed mid-turn submits:
                        # observability only — no error, no replay.
                        _log_degrade(
                            "headless_pending_turns_absorbed_at_eof",
                            handle_id=handle.handle_id,
                            session_id=str(
                                handle.ref.get("walkcode_session_id")
                                or handle.ref.get("session_id", "")
                            ),
                            pending_turns=pending_lost,
                            absorbable_pending=absorbable_pending,
                            absorption_age_seconds=round(eof_absorption_age, 1),
                            result_age_seconds=round(
                                eof_decision_now - last_accounted_result_at, 1
                            ),
                            submit_age_seconds=round(
                                eof_decision_now - eof_last_submit_ts, 1
                            ),
                        )
                    elif pending_lost > 0:
                        # Last (lifecycle-wise it must win over any synthetic
                        # completion above): the worker/stream ended before
                        # producing a single message for an accepted submit.
                        # Silence here = the session stuck on ACTIVE forever
                        # with the user's message gone (review round 3,
                        # 6-dimension consensus).
                        _log_degrade(
                            "headless_worker_eof_with_pending_turns",
                            handle_id=handle.handle_id,
                            pending_turns=pending_lost,
                        )
                        yield AgentEvent(
                            AgentEventType.SESSION_ERROR,
                            {
                                "message": (
                                    "代理进程在生成回复前退出了，你刚发送的消息没有被处理；"
                                    "请重发一次。"
                                ),
                                # ADR 0058：结构化标记，让 orchestrator 识别
                                # "已接受的提交没了"并调度自动重放，而不是
                                # 只靠上面这句可能被代际围栏丢掉的文案。
                                "reason": "pending_turn_lost",
                                # 中途死 vs 零流量死是重放安全性的分界：非注入
                                # 回合已经流出过 delta/工具/权限事件意味着副作用
                                # 可能已发生，重放会重复执行（审查 R1 共识；R2
                                # 修正：注入回合的流量不算，浮出的权限事件算）。
                                "traffic_seen": bool(user_turn_traffic),
                                "pending_lost": int(pending_lost),
                            },
                        )
                    break
                timeout = self._stream_wait_timeout(
                    # Raw turn state on purpose: unaccounted submits must NOT
                    # map to an infinite wait — they get the bounded ceiling
                    # via pending_turns below.
                    turn_open=turn_open,
                    active_tasks=active_tasks,
                    bridge=bridge,
                    quiet_since=quiet_since,
                    injection_hold_until=injection_hold_until,
                    pending_turns=self._pending_turns.get(handle.handle_id, 0),
                    pending_submitted_at=self._last_submit_monotonic.get(handle.handle_id, 0.0),
                )
                pending_before_wait = bridge is not None and bridge.has_any_pending()
                wait_set = {task for task in (msg_task, queue_task) if task is not None}
                wait_started = time.monotonic()
                done, _pending = await asyncio.wait(
                    wait_set,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=timeout,
                )
                if not done:
                    # Quiet period elapsed. Re-derive the state (a submit may
                    # have raced in) and either settle or fire the ceiling.
                    if turn_open or (bridge is not None and bridge.has_any_pending()):
                        continue
                    if pending_before_wait:
                        # A human decision resolved during this wait; the time
                        # spent waiting on the human is not "quiet time" — the
                        # ceiling/grace clocks restart from the answer.
                        quiet_since = time.monotonic()
                        continue
                    if injected_turn_expected and time.monotonic() >= injection_hold_until:
                        # The predicted injected turn never materialized; drop
                        # the prediction so the next real turn's result can
                        # account for a queued submit again.
                        injected_turn_expected = False
                    pending_submits = self._pending_turns.get(handle.handle_id, 0)
                    if pending_submits > 0:
                        # A submitted turn has produced nothing yet. Keep
                        # waiting up to the ceiling — measured from the LATER
                        # of last stream traffic and the latest submit (a
                        # fresh submit must get the full window even when
                        # background tasks were already long quiet) — past
                        # it, give up loudly instead of holding the worker
                        # open forever.
                        pending_clock = max(
                            quiet_since,
                            self._last_submit_monotonic.get(handle.handle_id, quiet_since),
                        )
                        if (
                            self.background_wait_ceiling_seconds <= 0
                            or time.monotonic() - pending_clock < self.background_wait_ceiling_seconds
                        ):
                            continue
                        last_submit_ts = self._last_submit_monotonic.get(
                            handle.handle_id, float("inf")
                        )
                        # Freeze the decision-time values: the log below must
                        # record exactly what the guard saw (round 3
                        # observability), never a later re-read.
                        decision_now = time.monotonic()
                        # Age basis includes last_msg_observed_at: ANY stream
                        # message — recognized or not (task beats, system
                        # status, future SDK types) — is worker activity that
                        # may belong to an unobserved queued turn (final
                        # verify pass 6). Silence must be total.
                        absorption_age = decision_now - max(
                            last_accounted_result_at,
                            last_turn_terminal_at,
                            last_task_activity_at,
                            last_msg_observed_at,
                        )
                        if (
                            absorbable_pending >= pending_submits
                            and not user_turn_traffic
                            and self._inflight_submits.get(handle.handle_id, 0) == 0
                            and last_accounted_result_at > last_submit_ts
                            and absorption_age >= self._ABSORBED_MIN_RESULT_AGE_SECONDS
                        ):
                            # Every leftover pending is a mid-turn absorption
                            # candidate covered by an accounted result, no
                            # unaccounted turn traffic exists, and no steering
                            # turn opened for a full ceiling window: the
                            # submits were ABSORBED into that turn (mid-turn
                            # injection). Clear the phantom counter and settle
                            # normally — firing the "no response" alarm here
                            # was the v0.14.12 false-positive (ADR 0059 R2).
                            _log_degrade(
                                "headless_pending_turns_absorbed",
                                handle_id=handle.handle_id,
                                session_id=str(
                                    handle.ref.get("walkcode_session_id")
                                    or handle.ref.get("session_id", "")
                                ),
                                pending_turns=pending_submits,
                                absorbable_pending=absorbable_pending,
                                ceiling_seconds=self.background_wait_ceiling_seconds,
                                absorption_age_seconds=round(absorption_age, 1),
                                result_age_seconds=round(
                                    decision_now - last_accounted_result_at, 1
                                ),
                                submit_age_seconds=round(
                                    decision_now - last_submit_ts, 1
                                ),
                            )
                            self._pending_turns.pop(handle.handle_id, None)
                            # Fall through to the normal settle path below.
                        else:
                            closing = self._unregister_handle(handle.handle_id)
                            _log_degrade(
                                "headless_pending_turn_ceiling",
                                handle_id=handle.handle_id,
                                pending_turns=pending_submits,
                                ceiling_seconds=self.background_wait_ceiling_seconds,
                            )
                            yield AgentEvent(
                                AgentEventType.TURN_DELTA,
                                {
                                    # 不能说"没有得到任何响应"：pending_submits
                                    # 是"提交数减去已核销的 result 数"，不是流量
                                    # 计数。回合完全可能流出过 delta/工具事件却
                                    # 始终等不到 result（worker 中途出问题、被判
                                    # injected turn），此时旧文案会与用户亲眼看到
                                    # 的输出直接矛盾。只陈述两件可验证的事：流上
                                    # 静默多久，以及这条消息没等到完成回执。
                                    "text": (
                                        f"⚠️ 会话已静默 {_humanize_seconds(self.background_wait_ceiling_seconds)}"
                                        "——没有任何新输出，你的消息也没等到完成回执，"
                                        "已停止本次会话监听。直接回复可重新拉起会话。"
                                    )
                                },
                            )
                            yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""})
                            if active_tasks:
                                # This close also abandons the background ledger;
                                # clear it or the status card keeps advertising
                                # tasks nobody is listening for.
                                yield AgentEvent(
                                    AgentEventType.BACKGROUND_TASKS,
                                    {"count": 0, "tasks": [], "abandoned": len(active_tasks)},
                                )
                            settled = True
                            break
                    if not active_tasks and time.monotonic() < injection_hold_until:
                        # A notification just drained the ledger; its injected
                        # follow-up turn may still be on the way.
                        continue
                    # Point of no return: detach the handle synchronously
                    # BEFORE any further await/yield, so a racing submit gets
                    # TransportUnavailable (and the resume fallback) instead of
                    # writing into a worker that is about to close.
                    closing = self._unregister_handle(handle.handle_id)
                    if active_tasks:
                        count = len(active_tasks)
                        titles = "、".join(
                            self._safe_task_label(task) for task in list(active_tasks.values())[:3]
                        )
                        _log_degrade(
                            "headless_background_wait_ceiling",
                            handle_id=handle.handle_id,
                            pending_tasks=count,
                            ceiling_seconds=self.background_wait_ceiling_seconds,
                        )
                        yield AgentEvent(
                            AgentEventType.TURN_DELTA,
                            {
                                "text": (
                                    f"⚠️ 后台任务等待超时：仍有 {count} 个后台任务（{titles}）在 "
                                    f"{_humanize_seconds(self.background_wait_ceiling_seconds)}内没有任何进展，"
                                    "已停止等待并关闭本次会话监听。它们的结果将不会自动送达；"
                                    "你可以直接回复继续对话。"
                                )
                            },
                        )
                        # Close the synthetic warning turn so the drain does
                        # not read the ended stream as a mid-turn failure and
                        # flip the session to ERROR_RECOVERABLE.
                        yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""})
                        yield AgentEvent(
                            AgentEventType.BACKGROUND_TASKS,
                            {"count": 0, "tasks": [], "abandoned": count},
                        )
                    settled = True
                    break
                if queue_task is not None and queue_task in done:
                    floated = queue_task.result()
                    queue_task = asyncio.ensure_future(bridge.next_event())
                    if floated is not None:
                        # 权限事件可先于任何流消息浮出；获准的工具可能已
                        # 执行副作用——必须计入流量，否则 EOF 会把"已授权
                        # 已执行"判成零流量并自动重放（R2 Critical）。
                        # R3 精化：注入回合开着时浮出的权限属于注入回合，
                        # 不算排队用户消息的流量；回合归属不明时保守计入。
                        if not (turn_open and current_turn_injected):
                            user_turn_traffic = True
                        yield floated
                    continue
                try:
                    message = msg_task.result()
                except StopAsyncIteration:
                    # EOF observation basis (ADR 0059 R2 round 2): if the
                    # wait actually blocked, the EOF arrived just now; if it
                    # returned ~instantly the EOF was buffered while this
                    # generator was suspended in a yield — its true arrival
                    # is only bounded below by the previous message's
                    # observation time.
                    eof_observation_basis = (
                        time.monotonic()
                        if time.monotonic() - wait_started >= 0.05
                        else last_msg_observed_at
                    )
                    msg_task = None
                    continue
                except Exception:
                    # A broken stream means a broken worker: unregister it so
                    # the next submit resumes a fresh process instead of
                    # reusing the dead connection, then let the drain runner
                    # surface the error.
                    stream_failed = True
                    raise
                msg_task = asyncio.ensure_future(stream_iter.__anext__())
                quiet_since = time.monotonic()
                last_msg_observed_at = quiet_since
                is_task_message, ledger_changed, task_subtype = self._apply_task_message(
                    message, active_tasks
                )
                if is_task_message:
                    # Worker activity: blocks absorbed classification for the
                    # next _ABSORBED_MIN_RESULT_AGE_SECONDS — a queued turn
                    # may be running behind this traffic (final verify).
                    last_task_activity_at = time.monotonic()
                    if not turn_open and task_subtype != "task_notification":
                        # Task lifecycle traffic (started/progress/updated/
                        # ledger change) with NO open turn may be the ONLY
                        # visible activity of an unobserved running turn —
                        # its opening traffic was swallowed by this early-
                        # continue branch. Sticky evidence for the queued
                        # user turn (final verify passes 3-5: task-only turns
                        # aged out of the activity clock and their submits
                        # were silently cleared; pass 5 showed non-started
                        # subtypes can be the opening too). Deliberately NOT
                        # ceded to a live injected-turn prediction (pass 4),
                        # and task_notification alone stays out — it is the
                        # designed between-turns injected-turn signal with
                        # its own hold machinery. Mis-crediting background
                        # beats of an OLD task here only costs an extra
                        # alarm / a refused-but-visible replay (v0.14.12
                        # behavior), never a silent drop; the sticky flag is
                        # reset by the next accounting result as usual.
                        user_turn_traffic = True
                    if not turn_open and (
                        task_subtype == "task_notification"
                        or (ledger_changed and not active_tasks)
                    ):
                        # Between turns, a notification — or ANY task event
                        # that just drained the ledger (a bare terminal
                        # task_updated, an empty background_tasks_changed) —
                        # predicts a CLI-injected follow-up turn: hold off
                        # settle for it, and stickily mark the NEXT turn as
                        # injected so its result cannot account for a queued
                        # user submit. Sticky on purpose: the injected turn
                        # usually opens with assistant text, which must not
                        # reclassify it as a user turn (review round 2).
                        # Mid-turn events don't: the follow-up is the very
                        # turn already streaming.
                        injected_turn_expected = True
                        injection_hold_until = time.monotonic() + max(
                            self.settle_grace_seconds, self._NOTIFICATION_FOLLOWUP_GRACE
                        )
                    # Every ledger beat (including task_progress no-ops) is
                    # surfaced: it refreshes session liveness and the status
                    # card's "空闲（后台 N 个任务）" line without channel text.
                    yield AgentEvent(
                        AgentEventType.BACKGROUND_TASKS,
                        {
                            "count": len(active_tasks),
                            "tasks": [dict(task) for task in active_tasks.values()],
                            "changed": ledger_changed,
                        },
                    )
                    continue
                events = self._convert_sdk_message_to_events(message)
                # Advance ALL turn/accounting state BEFORE yielding: the
                # generator suspends at yield while the orchestrator awaits
                # (cards, outbox), and a submit racing into that window must
                # observe consistent state (review round: yield-suspension
                # made time-based attribution unsound).
                if any(
                    event.type in {AgentEventType.TURN_COMPLETED, AgentEventType.SESSION_ERROR}
                    for event in events
                ):
                    # An error result also closes the turn — without this the
                    # stream would wait forever for a completion that never
                    # comes and the worker would never settle.
                    result_is_injected = current_turn_injected or (
                        # A bare result with no opening traffic while an
                        # injected turn is predicted (window still live):
                        # attribute it to the injected turn, not to a queued
                        # submit.
                        not turn_open
                        and injected_turn_expected
                        and time.monotonic() < injection_hold_until
                    )
                    turn_open = False
                    # Any turn end (injected included) refreshes the terminal
                    # clock: absorption age is measured from here — a queued
                    # message could not run while this turn held the worker.
                    last_turn_terminal_at = time.monotonic()
                    turn_completed_cleanly = any(
                        event.type == AgentEventType.TURN_COMPLETED for event in events
                    )
                    if not turn_completed_cleanly:
                        # ANY turn ending in SESSION_ERROR — injected included
                        # — revokes absorption evidence: an aborted turn
                        # proves nothing about injected messages, and a CLI
                        # unhealthy enough to error weakens the "a queued
                        # turn would have opened by now" inference (round 3).
                        absorbable_pending = 0
                    if not result_is_injected:
                        # A non-injected turn completed: it accounts for one
                        # submitted turn. Injected turns (notification
                        # replays) never do — the queued user turn is still
                        # behind them.
                        pending = self._pending_turns.get(handle.handle_id, 0)
                        if pending > 1:
                            self._pending_turns[handle.handle_id] = pending - 1
                        else:
                            self._pending_turns.pop(handle.handle_id, None)
                        # ADR 0059 R2: absorption evidence. Only submits that
                        # arrived while THIS turn was observably open (above
                        # the floor) AND already confirmed by the client
                        # (not in flight) can have been absorbed into it;
                        # anything queued before the turn opened owns its own
                        # future turn and must never be silently cleared.
                        # Candidates MERGE with the carried ones (a mixed
                        # batch — one absorbed, one steering — must not lose
                        # the absorbed evidence when the steering turn runs).
                        # (A turn ending in SESSION_ERROR was already
                        # revoked above, injected turns included.)
                        if turn_completed_cleanly:
                            last_accounted_result_at = time.monotonic()
                            if pending_at_turn_open is not None:
                                mid_turn_submits = max(
                                    0,
                                    pending
                                    - pending_at_turn_open
                                    - self._inflight_submits.get(handle.handle_id, 0),
                                )
                                absorbable_pending = min(
                                    max(0, pending - 1),
                                    absorbable_pending + mid_turn_submits,
                                )
                            else:
                                # Bare result: a non-injected turn ran WITHOUT
                                # observed opening traffic, so the turn-open
                                # deduction never fired. The turn still
                                # consumed one marker — worst case a candidate
                                # (same identity-less rule as the open-time
                                # deduction) — and observed no mid-turn
                                # submits, so it contributes no new candidates
                                # (final verify panel: without this deduction
                                # a stale candidate transfers to a protected
                                # between-turns submit and silently drops it).
                                absorbable_pending = min(
                                    max(0, pending - 1),
                                    max(0, absorbable_pending - 1),
                                )
                        pending_at_turn_open = None
                        # 该非注入回合已终局并核销一个提交：它的流量不再
                        # 属于任何仍在排队的提交。
                        user_turn_traffic = False
                    current_turn_injected = False
                    # Whatever turn just closed satisfies (or supersedes) the
                    # injected-turn prediction and the hold window; keeping
                    # either would only delay settle.
                    injected_turn_expected = False
                    injection_hold_until = 0.0
                elif events or self._is_turn_traffic(message):
                    if not turn_open:
                        # Classify the opening turn: a predicted injected turn
                        # stays injected regardless of traffic type — but the
                        # prediction must be checked against its window HERE
                        # (the pending-turn wait sleeps on the long ceiling
                        # and won't wake at window expiry), or a real reply
                        # arriving after expiry is still misclassified and
                        # its result never accounts for the submit. Otherwise
                        # a stream user-role message means CLI-injected
                        # (submitted prompts are never echoed on the stream).
                        prediction_live = (
                            injected_turn_expected and time.monotonic() < injection_hold_until
                        )
                        current_turn_injected = prediction_live or self._is_user_role_message(
                            message
                        )
                        injected_turn_expected = False
                        if not current_turn_injected:
                            # ADR 0059 R2: floor for absorption candidates —
                            # only submits counted ABOVE this snapshot arrive
                            # mid-turn. Opening a real turn consumes exactly
                            # ONE queued marker, and candidates carry no
                            # identity, so assume the WORST case: the opened
                            # turn consumed a candidate — always deduct one
                            # (round 3: capping at markers-minus-one let an
                            # in-between submit inherit a stale candidate and
                            # get silently cleared). The deduction can only
                            # cause extra alarms, never a silent drop; the
                            # remaining carry keeps the mixed-batch absorbed
                            # evidence (round 2).
                            pending_at_turn_open = self._pending_turns.get(
                                handle.handle_id, 0
                            )
                            absorbable_pending = max(
                                0,
                                min(absorbable_pending, pending_at_turn_open) - 1,
                            )
                    turn_open = True
                    if not current_turn_injected:
                        user_turn_traffic = True
                    injection_hold_until = 0.0
                for event in events:
                    yield event
        finally:
            for task in (msg_task, queue_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            # Fail-safe: unblock any callback still awaiting a decision so the
            # SDK's spawned task returns (deny) instead of leaking.
            if bridge is not None:
                bridge.fail_pending_default_deny()
            if closing is not None:
                # Off duty: the handle was already detached at the decision
                # point; only the process disconnect remains.
                closing_client, closing_bridge = closing
                if closing_bridge is not None:
                    closing_bridge.fail_pending_default_deny(reason="worker_closed")
                await self._disconnect_client(handle.handle_id, closing_client)
            elif settled or stream_failed:
                await self._close_handle_client(handle.handle_id)

    @staticmethod
    def _safe_task_label(task: dict[str, Any]) -> str:
        """Task descriptions come from the SDK stream, not from WalkCode:
        flatten whitespace and cap length before embedding them in a
        system-voiced channel warning."""
        raw = str(task.get("description") or task.get("task_id") or "?")
        return re.sub(r"\s+", " ", raw).strip()[:40] or "?"

    def _stream_wait_timeout(
        self,
        *,
        turn_open: bool,
        active_tasks: dict[str, dict[str, Any]],
        bridge: _ClaudePermissionBridge | None,
        quiet_since: float,
        injection_hold_until: float = 0.0,
        pending_turns: int = 0,
        pending_submitted_at: float = 0.0,
    ) -> float | None:
        """How long the next stream wait may block before a settle check.

        None means wait indefinitely — only for mid-turn silence (long tool
        runs, thinking), which has its own health watchdog. A pending human
        decision waits in bounded rechecks instead: resolve() completes a
        Future without waking this loop, so an infinite wait could outlive the
        answer and freeze the ceiling clock. Unaccounted submits wait up to
        the background ceiling, never forever.
        """
        if turn_open:
            return None
        if bridge is not None and bridge.has_any_pending():
            return self._PENDING_DECISION_RECHECK_SECONDS
        if pending_turns > 0:
            if self.background_wait_ceiling_seconds <= 0:
                return None
            # Fresh submits get the full window even if the stream was
            # already long quiet (background tasks): clock from the later of
            # last traffic and last submit.
            pending_clock = max(quiet_since, pending_submitted_at)
            remaining = self.background_wait_ceiling_seconds - (time.monotonic() - pending_clock)
            return max(remaining, 0.1)
        if not active_tasks:
            hold = injection_hold_until - time.monotonic()
            return max(self.settle_grace_seconds, hold, 0.1)
        if self.background_wait_ceiling_seconds <= 0:
            return None
        remaining = self.background_wait_ceiling_seconds - (time.monotonic() - quiet_since)
        return max(remaining, 0.1)

    _TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})
    _TASK_MESSAGE_CLASSES = frozenset(
        {"TaskStartedMessage", "TaskProgressMessage", "TaskNotificationMessage", "TaskUpdatedMessage"}
    )
    _TASK_SYSTEM_SUBTYPES = frozenset(
        {"task_started", "task_progress", "task_notification", "task_updated", "background_tasks_changed"}
    )

    @classmethod
    def _apply_task_message(
        cls,
        message: Any,
        active_tasks: dict[str, dict[str, Any]],
    ) -> tuple[bool, bool, str]:
        """Fold a background-task lifecycle message into the ledger.

        Returns (is_task_message, ledger_changed, subtype). Rules verified
        against claude-agent-sdk 0.2.x + CLI 2.1.x:

        - task_started adds; task_notification OR task_updated with a terminal
          status (completed/failed/stopped/killed) removes — not every
          terminating task emits a task_notification, so both must count.
        - The same task_id may notify more than once (SendMessage can revive a
          finished subagent): removal is by status, never by notification
          count, and a running/pending task_updated re-adds the task.
        - background_tasks_changed carries the authoritative full list of
          still-running tasks (empty list == ledger drained) and reconciles
          any drift.
        """
        subtype = ""
        data: Any = None
        class_name = message.__class__.__name__
        if class_name == "TaskStartedMessage":
            subtype = "task_started"
        elif class_name == "TaskProgressMessage":
            subtype = "task_progress"
        elif class_name == "TaskNotificationMessage":
            subtype = "task_notification"
        elif class_name == "TaskUpdatedMessage":
            subtype = "task_updated"
        elif class_name == "SystemMessage":
            subtype = str(getattr(message, "subtype", "") or "")
            data = getattr(message, "data", None)
        if subtype not in cls._TASK_SYSTEM_SUBTYPES:
            return False, False, ""
        if data is None:
            data = message

        def _field(name: str) -> Any:
            if isinstance(data, dict):
                return data.get(name)
            return getattr(data, name, None)

        if subtype == "background_tasks_changed":
            tasks_field = _field("tasks")
            if not isinstance(tasks_field, list):
                return True, False, subtype
            rebuilt: dict[str, dict[str, Any]] = {}
            for entry in tasks_field:
                if isinstance(entry, dict) and entry.get("task_id"):
                    task_id = str(entry["task_id"])
                    rebuilt[task_id] = {
                        "task_id": task_id,
                        "description": str(entry.get("description", "") or ""),
                    }
            changed = set(rebuilt) != set(active_tasks)
            active_tasks.clear()
            active_tasks.update(rebuilt)
            return True, changed, subtype

        task_id = str(_field("task_id") or "")
        if not task_id:
            return True, False, subtype
        if subtype == "task_started":
            changed = task_id not in active_tasks
            active_tasks[task_id] = {
                "task_id": task_id,
                "description": str(_field("description") or ""),
            }
            return True, changed, subtype
        if subtype == "task_progress":
            return True, False, subtype
        status = str(_field("status") or "")
        if not status and subtype == "task_updated":
            patch = _field("patch")
            if isinstance(patch, dict):
                status = str(patch.get("status", "") or "")
        if not status and subtype == "task_notification":
            # A notification IS the "this agent stopped" signal; one without a
            # status field must still settle the ledger entry or the task
            # lingers until the wait ceiling fires a false alarm.
            status = "completed"
        if status in cls._TERMINAL_TASK_STATUSES:
            return True, active_tasks.pop(task_id, None) is not None, subtype
        if status in {"running", "pending"} and task_id not in active_tasks:
            active_tasks[task_id] = {
                "task_id": task_id,
                "description": str(_field("description") or ""),
            }
            return True, True, subtype
        return True, False, subtype

    @staticmethod
    def _is_turn_traffic(message: Any) -> bool:
        """Raw messages that mean a turn is (still) in flight.

        Injected task-notification user messages and assistant output both
        signal the CLI opened/continues a turn, even when they convert to no
        channel-visible events.
        """
        return message.__class__.__name__ in {"UserMessage", "AssistantMessage"}

    @classmethod
    def _convert_sdk_message_to_events(cls, message: Any) -> list[AgentEvent]:
        item = cls._convert_sdk_message(message)
        if item is None:
            return []
        if isinstance(item, list):
            return item
        return [item]

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        bridge = self._bridges.get(handle.handle_id)
        if bridge is not None and bridge.has_pending(rid):
            # can_use_tool path: resolve the Future the blocked SDK callback is
            # awaiting. Write-once is enforced inside the bridge.
            bridge.resolve(rid, dict(decision))
            return
        if handle.handle_id not in self._clients:
            # The worker (and its in-flight can_use_tool Future) lived in a
            # previous runtime process; a card clicked after a restart lands
            # here. Raise instead of KeyError so the callback path can tell
            # the user the card is stale rather than dying silently.
            raise TransportUnavailable("claude headless worker is gone (runtime restarted)")
        # Live worker, but no pending can_use_tool call for this rid (the
        # bridge already timed out / resolved it): nothing can take it.
        raise CapabilityUnsupported("Claude headless permission approval is not available")

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        bridge = self._bridges.get(handle.handle_id)
        if bridge is not None and bridge.has_pending(rid):
            bridge.resolve(rid, {"action": "answers", "answers": dict(answers)})
            return
        if handle.handle_id not in self._clients:
            raise TransportUnavailable("claude headless worker is gone (runtime restarted)")
        raise CapabilityUnsupported("Claude headless AskUserQuestion answers are not available")

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        bridge = self._bridges.pop(handle.handle_id, None)
        if bridge is not None:
            bridge.fail_pending_default_deny(reason="shutdown")
        try:
            result = await self._call_client_control(handle, "shutdown", mode, state="stopped")
        finally:
            # The real SDK client has no shutdown() control method;
            # disconnecting here is what actually reaps the worker process
            # instead of leaking it — even when a client-provided shutdown
            # method raised.
            await self._close_handle_client(handle.handle_id)
        if not result.accepted and result.reason in {
            BlockedReason.NOT_FOUND,
            BlockedReason.CAPABILITY_DISABLED,
        }:
            # Idempotent close: the worker is gone (already settled, or the
            # real SDK client simply has no shutdown() control method) and the
            # disconnect above did the actual work. Reporting failure here
            # would leave close_session() unable to mark the session stopped —
            # "process dead, session forever running".
            return ControlResult(True, state="stopped")
        return result

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        return await self._call_client_control(handle, "set_model", model, state="model_set")

    async def _call_client_control(
        self,
        handle: TransportHandle,
        method_name: str,
        *args,
        state: str,
    ) -> ControlResult:
        client = self._clients.get(handle.handle_id)
        if client is None:
            return ControlResult(False, BlockedReason.NOT_FOUND)
        method = getattr(client, method_name, None)
        if method is None:
            return ControlResult(False, BlockedReason.CAPABILITY_DISABLED)
        call_args = args
        if args:
            # Signature drift tolerance decided UP FRONT (the real SDK's
            # interrupt() takes no arguments while ours forwards a reason).
            # Binding beats try/except-TypeError: a retry would double-invoke
            # the method and mask TypeErrors raised inside its body.
            try:
                inspect.signature(method).bind(*args)
            except TypeError:
                call_args = ()
            except (ValueError, RuntimeError):
                pass  # unintrospectable callable: keep the declared args
        await _maybe_await(method(*call_args))
        return ControlResult(True, state=state)

    def _available(self) -> bool:
        if self._client_factory is not None:
            return True
        try:
            sdk = self._sdk_loader()
        except Exception:
            return False
        return getattr(sdk, "ClaudeSDKClient", None) is not None

    def _option_kwargs(self, spec: LaunchSpec, *, resume_id: str = "") -> dict[str, Any]:
        option_kwargs: dict[str, Any] = {"cwd": spec.cwd}
        if self.anthropic_base_url:
            # Confirmed live against a real Vertex-routed profile: plain
            # options.env is merged into the subprocess env (verified via
            # CLAUDE_CONFIG_DIR, which relies on exactly that), but Claude
            # Code still applies this profile's own settings.json (loaded
            # from CLAUDE_CONFIG_DIR) env block with *higher* priority than
            # inherited process env for ANTHROPIC_BASE_URL/ANTHROPIC_VERTEX_
            # BASE_URL specifically — env-only overrides silently never hit
            # a local proxy. --settings is the layer Claude Code actually
            # honors here.
            #
            # Note self.settings (WALKCODE_CLAUDE_SETTINGS) is NOT consulted:
            # combining it with this override is rejected at config-parse
            # time (_configured_agent_options).
            option_kwargs["settings"] = self._anthropic_base_url_settings_override()
        elif self.settings:
            option_kwargs["settings"] = self.settings
        if self.cli_path:
            option_kwargs["cli_path"] = self.cli_path
        if self.config_dir:
            # SDK merges options.env over inherited os.environ, so this pins the
            # profile's Claude config dir (credentials, settings, history) without
            # touching the runtime's own environment.
            option_kwargs["env"] = {"CLAUDE_CONFIG_DIR": self.config_dir}
        if self.permission_mode:
            # Without an interactive can_use_tool callback, default mode denies
            # non-allowlisted tools. Per-instance mode (e.g. acceptEdits) makes
            # headless sessions usable; interactive permission cards are a
            # separate, larger feature.
            option_kwargs["permission_mode"] = self.permission_mode
        if resume_id:
            option_kwargs["resume"] = resume_id
        return option_kwargs

    def _anthropic_base_url_settings_override(self) -> str:
        """Build the --settings payload routing this profile through the debug proxy.

        Two facts, both confirmed live against real profiles, shape this:

        - Claude Code reads ANTHROPIC_VERTEX_BASE_URL (not ANTHROPIC_BASE_URL)
          when Vertex routing is active, and the Vertex switch may live only in
          the profile's settings.json — invisible to this runtime's process env
          (a launchd-run serve has neither). So both variables are always set;
          the inactive one is ignored.
        - The --settings env map REPLACES the profile settings.json env map
          wholesale rather than merging per key. An override carrying only the
          base URLs therefore drops the profile's own env — including
          ANTHROPIC_API_KEY — and every turn fails "Not logged in" on profiles
          that authenticate via settings.json env. The override must re-supply
          the profile env, merged with the base-URL rewrite.

        The merged env can contain secrets, so it is never passed inline on the
        CLI (argv is world-readable via ps); it is written to a 0600 file under
        the profile's own config dir — same directory, same owner, same threat
        model as the settings.json those values came from — and the *path* is
        returned. A corrupt settings.json raises instead of silently degrading.
        """
        env_obj: dict[str, Any] = {}
        settings_path: Path | None = None
        if self.config_dir:
            settings_path = Path(self.config_dir) / "settings.json"
            if settings_path.is_file():
                try:
                    profile_settings = json.loads(settings_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise TransportUnavailable(
                        f"cannot apply WALKCODE_CLAUDE_ANTHROPIC_BASE_URL: unreadable or invalid "
                        f"JSON in {settings_path}: {exc}"
                    ) from exc
                if isinstance(profile_settings, dict) and isinstance(profile_settings.get("env"), dict):
                    env_obj.update(profile_settings["env"])
        env_obj["ANTHROPIC_BASE_URL"] = self.anthropic_base_url
        env_obj["ANTHROPIC_VERTEX_BASE_URL"] = self.anthropic_base_url
        payload = json.dumps({"env": env_obj})
        if settings_path is None:
            # No config dir → no profile env consulted, nothing sensitive in
            # the payload; inline JSON is fine and leaves no file behind.
            return payload
        override_path = Path(self.config_dir) / "walkcode-tap-override-settings.json"
        fd = os.open(
            str(override_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.fchmod(fd, 0o600)  # tighten pre-existing files created with wider modes
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        return str(override_path)

    def _create_client(self, spec: LaunchSpec, *, resume_id: str = ""):
        if self._client_factory is not None:
            return self._client_factory(spec), None
        sdk = self._sdk_loader()
        client_cls = getattr(sdk, "ClaudeSDKClient", None)
        if client_cls is None:
            raise TransportUnavailable("claude_agent_sdk.ClaudeSDKClient is not available")
        options_cls = getattr(sdk, "ClaudeAgentOptions", None)
        option_kwargs = self._option_kwargs(spec, resume_id=resume_id)
        # Downloaded attachments live under attachment_download_dir(); adding it
        # as a working directory means the agent's Read of a file it received
        # doesn't trip a permission prompt for a path outside cwd.
        if options_cls is not None and _options_supports_field(options_cls, "add_dirs"):
            existing = list(option_kwargs.get("add_dirs") or [])
            option_kwargs["add_dirs"] = [*existing, str(attachment_download_dir())]
        if self.environment_context and options_cls is not None:
            if _options_supports_field(options_cls, "append_system_prompt"):
                option_kwargs["append_system_prompt"] = self.environment_context
            elif _options_supports_field(options_cls, "system_prompt"):
                # Current SDK shape: the claude_code preset keeps the standard
                # system prompt and "append" adds the channel context to it.
                option_kwargs["system_prompt"] = {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": self.environment_context,
                }
        if options_cls is not None and _options_supports_field(options_cls, "max_buffer_size"):
            # The SDK's subprocess transport rejects any single stream-json
            # message over 1 MiB ("Agent output stream failed: ... exceeded
            # maximum buffer size"), which real turns hit (large tool results
            # / attachments). Same class of failure as the codex 64 KiB
            # readline limit; sized to match its 64 MiB ceiling.
            option_kwargs["max_buffer_size"] = self._SDK_MAX_BUFFER_SIZE
        bridge: _ClaudePermissionBridge | None = None
        if options_cls is not None and self._permission_bridging_supported(sdk):
            bridge = _ClaudePermissionBridge(sdk=sdk, timeout=self.permission_timeout)
            option_kwargs["can_use_tool"] = bridge.can_use_tool
        try:
            if options_cls is not None:
                return client_cls(options=options_cls(**option_kwargs)), bridge
            return client_cls(), None
        except TypeError as exc:
            raise TransportUnavailable("claude_agent_sdk.ClaudeSDKClient cannot be constructed") from exc

    def _permission_bridging_supported(self, sdk: Any) -> bool:
        # Only wire can_use_tool when the SDK exposes the PermissionResult types
        # the bridge returns. bypassPermissions still keeps the bridge: the CLI
        # auto-approves regular tools without consulting can_use_tool, but it
        # DOES invoke the callback for AskUserQuestion (live-verified 2026-07),
        # and dropping the bridge there would silently kill the IM answer loop.
        return (
            getattr(sdk, "PermissionResultAllow", None) is not None
            and getattr(sdk, "PermissionResultDeny", None) is not None
        )

    @staticmethod
    async def _connect_client(client: Any) -> None:
        # Called exactly once: a retry on TypeError would spawn a second CLI
        # subprocess when the TypeError came from inside connect().
        await client.connect(prompt=None)

    @classmethod
    def _convert_sdk_message(cls, message: Any) -> AgentEvent | list[AgentEvent] | None:
        error = getattr(message, "error", None)
        is_error = bool(getattr(message, "is_error", False))
        if is_error or error is not None:
            result = getattr(message, "result", "")
            return AgentEvent(
                AgentEventType.SESSION_ERROR,
                {"message": str(error or result or "Claude SDK reported an error")},
            )

        content = getattr(message, "content", None)
        events = cls._extract_sdk_tool_events(message)
        tool_block_message = bool(events)
        if content is not None and not tool_block_message:
            events = cls._extract_sdk_tool_events(content)
        text = "" if tool_block_message else cls._extract_sdk_text(content)
        if text and not cls._is_user_role_message(message):
            # User-role messages on the stream are inputs (tool results, or the
            # CLI's injected <task-notification> turns) — echoing their text
            # back to the channel would show the user machine-generated prompts
            # as if the agent said them.
            if events:
                # ADR 0055: text sharing a message with tool blocks is
                # mid-turn narration. It precedes the tools in content order,
                # so it must not become a bubble APPENDED after them (the old
                # behavior: out of order, and it sealed the burst card).
                events.insert(0, AgentEvent(AgentEventType.TURN_NARRATION, {"text": text}))
            else:
                events.append(AgentEvent(AgentEventType.TURN_DELTA, {"text": text}))

        result = getattr(message, "result", None)
        class_name = message.__class__.__name__
        if result is not None or class_name == "ResultMessage":
            payload: dict[str, Any] = {"message": "" if result is None else str(result)}
            session_id = getattr(message, "session_id", "")
            if session_id:
                payload["session_id"] = str(session_id)
            usage = getattr(message, "usage", None)
            if usage is not None:
                payload["usage"] = usage
            events.append(AgentEvent(AgentEventType.TURN_COMPLETED, payload))

        # AssistantMessage carries the live model slug (the init system message
        # is not surfaced by the SDK client); tag it onto the emitted events so
        # the orchestrator can track the session's current model.
        model = str(getattr(message, "model", "") or "")
        if model:
            for event in events:
                event.payload.setdefault("model", model)

        return events or None

    @staticmethod
    def _is_user_role_message(message: Any) -> bool:
        if message.__class__.__name__ == "UserMessage":
            return True
        return str(getattr(message, "role", "") or "") == "user"

    @classmethod
    def _extract_sdk_tool_events(cls, content: Any) -> list[AgentEvent]:
        if content is None:
            return []
        if isinstance(content, list) or isinstance(content, tuple):
            events: list[AgentEvent] = []
            for item in content:
                events.extend(cls._extract_sdk_tool_events(item))
            return events
        block_type = _sdk_block_field(content, "type").lower()
        class_name = content.__class__.__name__.lower()
        if not block_type:
            block_type = class_name
        normalized_block_type = re.sub(r"[^a-z0-9]+", "", block_type)
        if (
            block_type == "tool_result"
            or "toolresult" in normalized_block_type
            or (
                any(token in normalized_block_type for token in ("toolcall", "functioncall"))
                and any(token in normalized_block_type for token in ("result", "output"))
            )
        ):
            failed = bool(_sdk_block_field(content, "is_error") or _sdk_block_field(content, "error"))
            return [
                AgentEvent(
                    AgentEventType.TOOL_FAILED if failed else AgentEventType.TOOL_COMPLETED,
                    {
                        "tool_id": _sdk_block_field(content, "tool_use_id") or _sdk_block_field(content, "id"),
                        "tool_name": _sdk_block_field(content, "name") or _sdk_block_field(content, "tool_name"),
                        "summary": "Tool failed" if failed else "Tool result received",
                    },
                )
            ]
        if (
            block_type in {"tool_use", "server_tool_use"}
            or "tooluse" in normalized_block_type
            or "toolcall" in normalized_block_type
            or "functioncall" in normalized_block_type
        ):
            tool_input = _sdk_block_field(content, "input")
            tool_name = _sdk_block_field(content, "name") or _sdk_block_field(content, "tool_name")
            if str(tool_name) == "AskUserQuestion":
                # The dedicated question card follows immediately; dumping the
                # raw questions JSON here would spoil it and flood the card.
                questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
                count = len(questions) if isinstance(questions, list) else 0
                summary = f"向你提了 {count} 个问题" if count else "向你提问"
            else:
                summary = _compact_tool_summary(tool_input)
            return [
                AgentEvent(
                    AgentEventType.TOOL_STARTED,
                    {
                        "tool_id": _sdk_block_field(content, "id"),
                        "tool_name": tool_name,
                        "summary": summary,
                    },
                )
            ]
        return []

    @classmethod
    def _extract_sdk_text(cls, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            text = content.get("text")
            return "" if text is None else str(text)
        if isinstance(content, list) or isinstance(content, tuple):
            return "".join(part for part in (cls._extract_sdk_text(item) for item in content) if part)
        text = getattr(content, "text", None)
        return "" if text is None else str(text)

    @staticmethod
    def _default_sdk_loader():
        import claude_agent_sdk

        return claude_agent_sdk
