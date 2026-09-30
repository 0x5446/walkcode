"""Channel/agent protocols, outbox dispatcher and the orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import re
import time
import uuid

from collections.abc import Callable
from typing import Any, Protocol

from . import claude_gate
from .models import (
    ActorRef,
    agent_session_id,
    _agent_session_identity,
    AgentEvent,
    AgentEventType,
    AttachmentRef,
    AuthorizationResult,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    _compact_tool_summary,
    ControlResult,
    DeliveryStatus,
    _durable_resume_ref,
    InboundEvent,
    LaunchSpec,
    _log_degrade,
    _maybe_await,
    PermanentDeliveryError,
    ResumeSpec,
    Session,
    _session_is_channel_revival_candidate,
    _session_is_external_tui_takeover_candidate,
    SessionHealth,
    SessionRole,
    SubmitResult,
    TakeoverError,
    TakeoverPhase,
    TakeoverTransaction,
    TransientDeliveryError,
    TransportCapabilities,
    TransportHandle,
    TransportUnavailable,
    TurnInput,
    WriterOwner,
)
from .stores import (
    AuthorizationStore,
    DecisionResult,
    DeliveryItem,
    DurableOutbox,
    HitlRequest,
    HitlStore,
    InboundLedger,
    InteractionContext,
    InteractionStore,
    SessionRegistry,
)
from .views import render_view_text, ViewModelFactory
from .process_control import claude_tui_current_session, SHARED_APP_SERVER_CONTROLLER
from .claude_headless import _claude_tool_is_high_risk, _ClaudePermissionBridge, EMPTY_TURN_NOTICE


# Lifecycle states where a turn is still in flight, so cycling the backend
# under it would kill work the user is waiting on. ACTIVE is running; the two
# WAITING_* states are parked on a human and resume the moment they answer.
#
# Losing such a turn is silent on codex: the drain sees session.status ==
# "stopped", takes the "stale drain must not error-mark the successor" branch
# and drops the failure (`event_drain_failed_after_replacement`, drop=True),
# and codex has no `pending_turn_lost` — that ADR 0058 auto-replay marker is
# produced only by ClaudeHeadlessTransport. The user's question would simply
# never be answered, with nothing in the channel saying so.
_TURN_IN_FLIGHT_STATES = frozenset({"ACTIVE", "WAITING_PERMISSION", "WAITING_USER"})

# ADR 0058：worker 在回答已接受的提交前退出（API 故障窗、进程崩溃）时的
# 自动重放退避表。两档：短退避接住瞬时抖动，长退避跨过典型的过载窗口。
TURN_REPLAY_DELAYS: tuple[float, ...] = (30.0, 300.0)
# 水位比较容差：等值意味着水位就是这条输入自己盖的章，不算"被更新输入推进"。
_TURN_REPLAY_WATERMARK_TOLERANCE_SECONDS = 0.5

# 会话标题：来源分级 + 同级刷新节流。
#
# 话题根卡片显示的就是这个标题。四条产生路径（claude TUI hook、codex TUI
# hook、codex app-server 事件流、claude headless 事件流）都汇到
# Orchestrator._maybe_refresh_session_title，用 rank 决定谁能盖谁：
#
#   tui_hook            观测会话建根时的 uuid 占位，最弱，任何真素材都该盖掉
#   turn_digest         最近一条助手消息的截断，没抓到用户首问时的兜底
#   initial_user_input  用户原话，比助手消息更接近"这个会话在干嘛"
#   llm_summary         预留给小模型精炼（当前生成器还不产出这一档）
#
# 升级（rank 变大）立刻生效，"首轮必刷"就是靠它，不受节流约束；同级覆盖才
# 走节流，避免每个回合都为标题改动 patch 一次卡片。
SESSION_TITLE_SOURCE_RANKS: dict[str, int] = {
    "": 0,
    "tui_hook": 1,
    "turn_digest": 2,
    "initial_user_input": 3,
    "llm_summary": 4,
}
SESSION_TITLE_REFRESH_INTERVAL_SECONDS = 120.0
SESSION_TITLE_MAX_CHARS = 40
# 每回合最多为标题攒这么多字符的助手正文。标题只取第一个非空行的前 40 字，
# 攒够开头就足够，长回合不该把整段 transcript 留在内存里。
SESSION_TITLE_MATERIAL_CHARS = 1000
# 根卡片连续编辑失败多少次后放弃原地重试、降级到子状态卡。瞬时抖动值得等，
# 但根消息被撤回/删除/超出编辑窗口时每个事件都重试一遍只会烧配额——用户看着
# 一张永不更新的状态卡，比看到一个标题过时的根更糟。永久错误不占预算，直接降级。
ROOT_CARD_EDIT_RETRY_BUDGET = 3
# 同级可以再刷新的来源（滚动型）。initial_user_input 不在其中：话题根标题锁在
# 用户的第一个问题上，不跟着后续追问漂移——频道主动发起那条路径也是这个语义
# （建根卡片时取首行，之后不再改）。
SESSION_TITLE_ROLLING_SOURCES = frozenset({"turn_digest", "llm_summary"})


def _session_title_source_rank(source: str) -> int:
    return SESSION_TITLE_SOURCE_RANKS.get(str(source or ""), 0)


def _clean_session_title(value: str) -> str:
    """First non-blank line, whitespace-collapsed, clipped to card width."""
    for line in str(value or "").splitlines():
        text = " ".join(line.split())
        if text:
            return text[:SESSION_TITLE_MAX_CHARS]
    return ""


def compose_session_title(*, user_text: str = "", assistant_text: str = "") -> tuple[str, str]:
    """Build ``(title, source)`` from one turn's material.

    Placeholder generator: no model call, just the user's own words with the
    assistant's last message as fallback. Swapping in an LLM means replacing
    this function (same two inputs) and returning source ``"llm_summary"`` so
    the rank table lets the sharper title win.
    """
    title = _clean_session_title(user_text)
    if title:
        return title, "initial_user_input"
    title = _clean_session_title(assistant_text)
    if title:
        return title, "turn_digest"
    return "", ""


def _external_claude_resume_ref(session: Session) -> dict[str, Any]:
    """The Claude-native resume_ref of a TUI-observed session, if any.

    TUI hooks store it nested as ``transport_ref["resume_ref"]`` with a
    ``transport_kind`` discriminator; only claude sessions have PreToolUse
    gate cards.
    """
    refs: list[dict[str, Any]] = []
    if isinstance(session.transport_ref, dict):
        refs.append(session.transport_ref)
    if session.writer_owner is not None and isinstance(session.writer_owner.external_ref, dict):
        refs.append(session.writer_owner.external_ref)
    for ref in refs:
        nested = ref.get("resume_ref")
        if isinstance(nested, dict) and str(nested.get("transport_kind", "")) == "claude_headless":
            return dict(nested)
    return {}


def _estimate_context_tokens(usage: dict[str, Any]) -> int:
    """Approximate context occupancy from the last turn's usage.

    Claude's input_tokens / cache_read / cache_creation are disjoint slices of
    the prompt; adding the turn's output approximates the next turn's prompt.
    Codex task_complete usage only has input_tokens/output_tokens (its
    cached_input_tokens is a subset of input_tokens, so it is not summed).
    """
    if not isinstance(usage, dict):
        return 0
    total = 0
    for key in (
        "input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
    ):
        try:
            total += int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _context_window_limit(model: str, used: int = 0, usage: dict[str, Any] | None = None) -> int:
    """Estimated context window for display.

    An explicit window reported by the agent wins (codex token_count records
    carry model_context_window; claude never reports one). Otherwise fall
    back to the claude-family heuristic: assistant events report dated ids
    without the [1m] routing marker, so a long-context session's marker is
    lost once the live id overwrites session.model. Bump the estimate when
    observed usage already exceeds the default window — better an upgraded
    limit than a >100% readout.
    """
    if isinstance(usage, dict):
        try:
            explicit = int(usage.get("model_context_window", 0) or 0)
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return explicit
    limit = 1_000_000 if "[1m]" in model else 200_000
    if used > limit:
        limit = 1_000_000
    return limit


class ChannelAdapter(Protocol):
    kind: str

    def capabilities(self) -> ChannelCapabilities: ...

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str: ...

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool: ...

    async def ack_callback(self, inbound: InboundEvent) -> None: ...

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef: ...


class AgentTransport(Protocol):
    kind: str

    def capabilities(self) -> TransportCapabilities: ...

    async def launch(self, spec: LaunchSpec) -> TransportHandle: ...

    async def resume(self, spec: ResumeSpec) -> TransportHandle: ...

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None: ...

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None: ...

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None: ...

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult: ...

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult: ...

    def events(self, handle: TransportHandle) -> Any: ...


class ExternalTuiController(Protocol):
    kind: str

    async def terminate(self, ref: dict[str, Any], reason: str) -> ControlResult: ...


class OutboxDispatcher:
    def __init__(
        self,
        outbox: DurableOutbox,
        channels: dict[str, ChannelAdapter],
        *,
        owner: str | None = None,
        claim_ttl: float = 60.0,
        on_state_changed: Callable[[], None] | None = None,
    ):
        self.outbox = outbox
        self.channels = channels
        self.owner = owner or f"dispatcher-{uuid.uuid4().hex}"
        self.claim_ttl = claim_ttl
        self.on_state_changed = on_state_changed
        self._flush_lock = asyncio.Lock()

    async def flush_once(self) -> None:
        async with self._flush_lock:
            items = self.outbox.claim_ready(owner=self.owner, lease_ttl=self.claim_ttl)
            if items:
                self._notify_state_changed()
            for item in items:
                await self._send_claimed_item(item)

    async def _send_claimed_item(self, item: DeliveryItem) -> None:
        channel_kind, account_id, chat_id, thread_id, root_message_id = item.channel_binding_key
        channel = self.channels.get(channel_kind)
        if channel is None:
            self.outbox.record_result(
                item.delivery_id,
                DeliveryStatus.TRANSIENT_FAILURE,
                claim_owner=self.owner,
            )
            self._notify_state_changed()
            return
        binding = ChannelBinding(
            channel_kind=channel_kind,
            account_id=account_id,
            chat_id=chat_id,
            thread_id=thread_id,
            root_message_id=root_message_id,
        )
        try:
            message_id = await channel.send_view(binding, item.view_model)
        except PermanentDeliveryError as exc:
            self.outbox.record_result(
                item.delivery_id,
                DeliveryStatus.PERMANENT_FAILURE,
                str(exc),
                claim_owner=self.owner,
            )
        except TransientDeliveryError as exc:
            self.outbox.record_result(
                item.delivery_id,
                DeliveryStatus.TRANSIENT_FAILURE,
                str(exc),
                claim_owner=self.owner,
                retry_after=exc.retry_after,
            )
        except Exception as exc:
            self.outbox.record_result(
                item.delivery_id,
                DeliveryStatus.TRANSIENT_FAILURE,
                str(exc),
                claim_owner=self.owner,
            )
        else:
            self.outbox.record_result(
                item.delivery_id,
                DeliveryStatus.SENT,
                claim_owner=self.owner,
                message_id=str(message_id or ""),
            )
        self._notify_state_changed()

    def _notify_state_changed(self) -> None:
        if self.on_state_changed is None:
            return
        try:
            self.on_state_changed()
        except Exception as exc:
            _log_degrade("outbox_state_save_failed", error=exc)
            raise


def _title_from_text(text: str, *, limit: int = 80) -> str:
    collapsed = " ".join(str(text or "").strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."


def _is_takeover_command(text: str) -> bool:
    first = str(text or "").strip().split(maxsplit=1)[0].lower() if str(text or "").strip() else ""
    command = first.split("@", 1)[0]
    return command in {"/takeover", "/take_over"}


def _format_ask_answers(ctx: "InteractionContext") -> str:
    # "周末计划: 出门浪、吃啥: 碳水快乐" — keyed by each question's header,
    # multi-select labels comma-joined (not a Python list repr).
    parts: list[str] = []
    for index, question in enumerate(ctx.questions):
        if index not in ctx.answers:
            continue
        label = str(question.get("header") or question.get("prompt") or f"Q{index + 1}")
        value = ctx.answers[index]
        if isinstance(value, list):
            shown = ", ".join(str(v) for v in value)
        else:
            shown = str(value)
        parts.append(f"{label}: {shown}")
    return "、".join(parts)


# ADR 0051: synthetic turn injected after a takeover-only handoff when
# pending HITL prompts were stale-marked. Channel-invisible (walkcode only
# renders agent events, never the inputs it submits itself), but it DOES
# land in the transcript — keep the wording neutral so a later TUI resume
# reading the history isn't confused, and instruct a re-ask explicitly so
# the fresh answerable card actually re-appears.
HANDOFF_CONTINUE_PROMPT = (
    "[ui-handoff] 会话已从终端切换到聊天端继续。"
    "若此前有未答的提问或未确认的权限请求，请立刻原样重新发起；"
    "否则继续当前任务。"
)

# Bound on the best-effort old-worker shutdown during an external TUI claim
# (ADR 0051): the claim runs on the ingress-locked hook path, so a wedged
# worker must degrade-log and move on instead of blocking ingress.
EXTERNAL_CLAIM_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class Orchestrator:
    def __init__(
        self,
        *,
        sessions: SessionRegistry,
        interactions: InteractionStore,
        outbox: DurableOutbox,
        channels: dict[str, ChannelAdapter],
        transports: dict[str, AgentTransport],
        external_tui_controllers: dict[str, ExternalTuiController] | None = None,
        authz: AuthorizationStore | None = None,
        hitls: HitlStore | None = None,
        inbound_ledger: InboundLedger | None = None,
        defer_event_drain: bool = False,
        outbox_dispatcher: OutboxDispatcher | None = None,
        on_state_changed: Callable[[], None] | None = None,
        handoff_continue: str = "auto",
        now: Callable[[], float] = time.time,
    ):
        self.sessions = sessions
        self.interactions = interactions
        self.outbox = outbox
        self.channels = channels
        self.transports = transports
        self.external_tui_controllers = external_tui_controllers or {}
        self.authz = authz
        self.hitls = hitls or HitlStore(now=now)
        self.inbound_ledger = inbound_ledger
        self.defer_event_drain = defer_event_drain
        self.outbox_dispatcher = outbox_dispatcher or OutboxDispatcher(
            outbox,
            channels,
            on_state_changed=on_state_changed,
        )
        self.on_state_changed = on_state_changed
        # ADR 0051: "auto" re-drives the agent after a takeover-only handoff
        # that stale-marked pending HITL prompts (see HANDOFF_CONTINUE_PROMPT).
        self.handoff_continue = handoff_continue
        self._background_event_drains: set[asyncio.Task] = set()
        # handle_id -> live drain task: the headless event stream is now
        # session-level (spans turns), so a submit into a handle that already
        # has a listener must NOT start a second one — two consumers would
        # split the SDK message stream between them.
        self._handle_event_drains: dict[str, asyncio.Task] = {}
        self._now = now
        # ADR 0058：每会话最近一次被接受的用户提交，供 worker 死于回答之前
        # 时带退避自动重放。In-memory on purpose：runtime 重启后的下一条
        # 新消息本来就会走复活路径，不需要跨重启的重放。
        self._turn_replays: dict[str, dict[str, Any]] = {}
        self._turn_replay_tasks: set[asyncio.Task] = set()
        # 测试可缩短；生产值见模块级 TURN_REPLAY_DELAYS。
        self.turn_replay_delays: tuple[float, ...] = TURN_REPLAY_DELAYS
        # Status-card dedup: fingerprint of the last successfully delivered
        # card per session. Refresh calls are event-driven, but most events
        # (tool started/completed churn, elapsed-time ticks) don't change what
        # the card materially says — skipping identical sends is what keeps
        # Lark's monthly API quota alive (a busy session used to emit
        # thousands of no-op card patches per day).
        # session_id -> (status card message_id, view fingerprint)
        self._status_card_fingerprints: dict[str, tuple[str, str]] = {}

    async def start_session(
        self,
        binding: ChannelBinding,
        transport_kind: str,
        cwd: str,
        owner: ActorRef,
    ) -> Session:
        transport = self.transports[transport_kind]
        session_id = f"sess-{uuid.uuid4().hex}"
        handle = await transport.launch(LaunchSpec(cwd=cwd, session_id=session_id))
        session = self.sessions.create_structured_session(
            session_id=session_id,
            binding=binding,
            transport_kind=transport_kind,
            transport_ref={"handle_id": handle.handle_id, **handle.ref},
            cwd=cwd,
            owner=owner,
        )
        initial_title = str(binding.capabilities.get("initial_title", "") or "").strip()
        if initial_title:
            session.cached_title = initial_title
            session.title_source = "initial_user_input"
        if self.authz is not None:
            self.authz.grant(session.session_id, owner, SessionRole.OWNER)
        return session

    # Per-channel reaction pools for lightweight acks (Lark values are
    # emoji_type keys).
    _ACK_REACTIONS: dict[str, tuple[str, ...]] = {
        "lark": ("DONE", "OK", "THUMBSUP", "MUSCLE", "APPLAUSE"),
    }

    async def _react_ack(self, session: Session, message_id: str) -> bool:
        """Best-effort emoji reaction on the user's message instead of a text
        receipt — one API call either way, but no extra bubble in the topic."""
        if not message_id:
            return False
        binding = session.channel_binding
        channel = self.channels.get(binding.channel_kind) if binding is not None else None
        react = getattr(channel, "react_to_message", None)
        pool = self._ACK_REACTIONS.get(binding.channel_kind) if binding is not None else None
        if channel is None or react is None or not pool:
            return False
        try:
            return bool(await react(binding, message_id, random.choice(pool)))
        except Exception:
            return False

    async def submit_user_input(
        self,
        session_id: str,
        turn: TurnInput,
        *,
        actor: ActorRef,
        generation: int,
        ack_message_id: str = "",
        replay_attempt: int = 0,
        replay_guard: str = "",
    ) -> SubmitResult:
        session = self.sessions.get(session_id)
        if self.authz is not None:
            authz_result = self.authz.can_submit(session_id, actor)
            if not authz_result.allowed:
                return SubmitResult(False, authz_result.reason)
        # ADR 0054: a message to a stopped STRUCTURED session revives it
        # instead of dead-ending at 会话已结束 — takeover minus the kill. Every
        # restart sweep used to orphan all channel conversations this way.
        # Scope guards:
        #  - only INVOLUNTARY stops revive (restart sweep, failed revival
        #    retry); an explicit close keeps its "blocks future submits"
        #    contract, and an archived session stays archived;
        #  - external-TUI takeover candidates keep their existing
        #    consent-based takeover-prompt path below;
        #  - the caller's generation must match the CURRENT one — a stale
        #    delayed submit must not resurrect the session past the fence.
        revived = False
        if (
            _session_is_channel_revival_candidate(session)
            and generation == session.generation
        ):
            revive_transport = self.transports.get(session.transport_kind)
            if revive_transport is not None and revive_transport.capabilities().resume_after_complete:
                revive = self.sessions.revive_stopped_structured_session(session_id)
                if revive.accepted:
                    revived = True
                    generation = session.generation
                    # Any free-text wait left behind by the dead worker's
                    # AskUserQuestion would swallow later plain messages.
                    self.interactions.clear_awaiting_other_for_session(session_id)
                    _log_degrade(
                        "session_revived_by_channel",
                        session_id=session_id,
                        actor=actor.actor_id,
                    )
        transport = None
        if session.lifecycle_state in {"IDLE", "ERROR_RECOVERABLE"}:
            transport = self.transports.get(session.transport_kind)
            if transport is None:
                return SubmitResult(False, "transport_not_wired")
            ready = await self._ensure_writer_ready_for_submit(session, transport, actor)
            if not ready.accepted:
                if revived:
                    # No worker came up: do not leave a phantom "running"
                    # record behind — the next message will retry the revival.
                    self.sessions.mark_revive_failed(session_id)
                return ready
        validation = self.sessions.validate_submit(session_id, generation)
        if not validation.accepted:
            if validation.reason in {BlockedReason.EXTERNAL_TUI_READONLY, BlockedReason.SESSION_STOPPED} and (
                _session_is_external_tui_takeover_candidate(session)
            ):
                blocked = self.sessions.block_input(
                    session_id,
                    actor=actor,
                    turn=turn,
                    generation=generation,
                )
                if blocked.blocked_input_id and session is not None:
                    await self._send_takeover_prompt(
                        session,
                        blocked.blocked_input_id,
                        requested_by=actor,
                        generation=generation,
                    )
                    await self.refresh_session_status_card(session)
                return blocked
            return validation

        if transport is None:
            transport = self.transports[session.transport_kind]
        caps = transport.capabilities()
        if not caps.structured_input:
            return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        handle = TransportHandle(
            handle_id=str(session.transport_ref.get("handle_id", "")),
            transport_kind=session.transport_kind,
            ref=dict(session.transport_ref),
        )
        # ADR 0058 R2：重放提交在真正发出前的最后一刻复核身份钉子——
        # writer 恢复等 await 期间可能有更新的人类提交完成并覆盖暂存，
        # 旧重放此时必须自灭，不能追在用户修正之后执行。
        if replay_guard:
            current_replay = self._turn_replays.get(session_id)
            if current_replay is None or current_replay.get("replay_id") != replay_guard:
                return SubmitResult(False, "replay_superseded")
        # ADR 0058：先登记再提交（提交等待期间 EOF 时暂存必须已指向本条，
        # 否则会误重放上一条已完成消息）；失败路径按 replay_id 还原旧态。
        # 水位值只预计算一次：登记和成功后落章用同一个值（R3 issue#4）。
        stamp_value = self._effective_input_stamp(session, turn.created_at)
        replay_entry_id, replaced_entry = self._remember_replayable_turn(
            session, turn, actor, attempt=replay_attempt, stamp_value=stamp_value
        )
        self._record_turn_submitted(session)
        try:
            try:
                await transport.submit_turn(handle, turn, idempotency_key=f"{session_id}:{generation}:{turn.text}")
            except TransportUnavailable:
                # Settle race: the persistent listener closed this worker
                # between the reuse check and the submit. Fall back to a fresh
                # resume so the user's message is not lost.
                retry = await self._resume_writer_for_submit(session, transport, actor)
                if not retry.accepted:
                    # ADR 0059 R1: surface the structured refusal instead of
                    # re-raising. A bare raise skips the channel's rejection
                    # note (Lark WS logs it and never redelivers), so the one
                    # message that hit "worker gone AND resume refused" would
                    # vanish with only a status-card error. Same cleanup as
                    # the except-branch below, then return the reason so the
                    # sender gets the resume_failed / missing_resume_ref note.
                    self._rollback_replayable_turn(session_id, replay_entry_id, replaced_entry)
                    session.last_progress_at = self._now()
                    session.last_progress_event = "turn.submit_failed"
                    session.lifecycle_state = "ERROR_RECOVERABLE"
                    await self.refresh_session_status_card(session)
                    return retry
                if replay_guard:
                    # R3 Critical：resume 的 await 窗口里更新的人类提交可能
                    # 已覆盖暂存并先行发出——重放的第二次发送前必须再对
                    # 一次钉子，否则 old 追着 new 执行的顺序又回来了。
                    current_replay = self._turn_replays.get(session_id)
                    if (
                        current_replay is None
                        or current_replay.get("replay_id") != replay_entry_id
                    ):
                        return SubmitResult(False, "replay_superseded")
                handle = self._handle_for_session(session)
                await transport.submit_turn(
                    handle, turn, idempotency_key=f"{session_id}:{generation}:{turn.text}"
                )
        except Exception:
            self._rollback_replayable_turn(session_id, replay_entry_id, replaced_entry)
            session.last_progress_at = self._now()
            session.last_progress_event = "turn.submit_failed"
            session.lifecycle_state = "ERROR_RECOVERABLE"
            await self.refresh_session_status_card(session)
            raise
        # ADR 0057：记录"最近一次被人说话"的时刻。频道消息用渠道产生时刻
        # （不是提交时刻——否则快速连发的第二条会被时效守卫误判），无时间
        # 戳的输入退化为本机当前时间。与登记时同值落章（R3 issue#4）。
        session.last_user_input_at = max(session.last_user_input_at, stamp_value)
        # "Got it, on it" reaction on the user's message — the status card
        # says the turn started, but the emoji is the at-a-glance receipt.
        await self._react_ack(session, ack_message_id)
        await self.refresh_session_status_card(session)
        if self.defer_event_drain:
            self._start_background_event_drain(session.session_id, transport, handle)
            self._notify_state_changed()
            return SubmitResult(True, "turn_submitted")
        await self._drain_events(session, transport, handle)
        await self.refresh_session_status_card(session)
        return SubmitResult(True)

    def _start_background_event_drain(
        self,
        session_id: str,
        transport: AgentTransport,
        handle: TransportHandle,
    ) -> None:
        existing = self._handle_event_drains.get(handle.handle_id)
        if existing is not None and not existing.done():
            # A session-level listener is already attached to this worker; the
            # just-submitted turn's events flow through it.
            return

        async def runner() -> None:
            session = self.sessions.get(session_id)
            try:
                await self._drain_events(session, transport, handle)
                await self.refresh_session_status_card(session)
            except Exception as exc:
                if (
                    session.status == "stopped"
                    or str(session.transport_ref.get("handle_id", "")) != handle.handle_id
                ):
                    # The session was closed, taken over, or moved to a fresh
                    # worker while this listener was still winding down; a
                    # stale drain's failure must not error-mark the successor.
                    _log_degrade(
                        "event_drain_failed_after_replacement",
                        session_id=session.session_id,
                        handle_id=handle.handle_id,
                        error=exc,
                        drop=True,
                    )
                    return
                session.last_progress_at = self._now()
                session.last_progress_event = "turn.event_drain_failed"
                session.lifecycle_state = "ERROR_RECOVERABLE"
                await self._send_session_view(
                    session,
                    {
                        "type": "error",
                        "message": f"Agent output stream failed: {type(exc).__name__}: {exc}",
                    },
                    idempotency_key=f"event-drain-failed:{session.last_event_seq}",
                )
                await self.refresh_session_status_card(session)
            finally:
                self._notify_state_changed()

        task = asyncio.create_task(runner())
        self._background_event_drains.add(task)
        self._handle_event_drains[handle.handle_id] = task

        def _cleanup(done_task: asyncio.Task, handle_id: str = handle.handle_id) -> None:
            self._background_event_drains.discard(done_task)
            if self._handle_event_drains.get(handle_id) is done_task:
                self._handle_event_drains.pop(handle_id, None)

        task.add_done_callback(_cleanup)

    def _notify_state_changed(self) -> None:
        if self.on_state_changed is None:
            return
        try:
            self.on_state_changed()
        except Exception as exc:
            _log_degrade("orchestrator_state_save_failed", error=exc)
            raise

    async def _flush_outbox(self) -> None:
        await self.outbox_dispatcher.flush_once()

    async def _ensure_writer_ready_for_submit(
        self,
        session: Session,
        transport: AgentTransport,
        actor: ActorRef,
    ) -> SubmitResult:
        if session.lifecycle_state not in {"IDLE", "ERROR_RECOVERABLE"}:
            return SubmitResult(True)
        # Persistent-listener reuse: an IDLE headless session whose worker is
        # still alive (listening for background subagents) accepts the next
        # turn directly — resuming here would fork a SECOND process off the
        # same agent session and orphan the listener. Only IDLE qualifies:
        # ERROR_RECOVERABLE means the previous submit or stream broke, and
        # recovery must go through a fresh worker, not the suspect one.
        handle_id = str(session.transport_ref.get("handle_id", ""))
        supports_reuse = getattr(transport, "handle_supports_reuse", None)
        if session.lifecycle_state == "IDLE" and handle_id and callable(supports_reuse):
            try:
                reusable = bool(supports_reuse(handle_id))
            except Exception:
                reusable = False
            if reusable:
                return self.sessions.acquire_structured_writer(
                    session.session_id,
                    transport_kind=session.transport_kind,
                    transport_ref=dict(session.transport_ref),
                    owner=actor,
                )
        return await self._resume_writer_for_submit(session, transport, actor)

    async def _resume_writer_for_submit(
        self,
        session: Session,
        transport: AgentTransport,
        actor: ActorRef,
    ) -> SubmitResult:
        caps = transport.capabilities()
        if not caps.resume_after_complete:
            return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        resume_ref = self._durable_resume_ref(session)
        if not resume_ref:
            return SubmitResult(False, "missing_resume_ref")
        try:
            handle = await transport.resume(
                ResumeSpec(
                    cwd=session.cwd,
                    session_id=session.session_id,
                    resume_ref=resume_ref,
                )
            )
        except Exception as exc:
            # ADR 0059 R1: the flattened "resume_failed" hid the real cause
            # (dead socket vs unconfirmed old worker vs SDK error) — trace it
            # so the silent-degradation path stays debuggable.
            _log_degrade(
                "writer_resume_failed",
                session_id=session.session_id,
                handle_id=str(session.transport_ref.get("handle_id", "")),
                error=exc,
            )
            return SubmitResult(False, "resume_failed")
        return self.sessions.acquire_structured_writer(
            session.session_id,
            transport_kind=session.transport_kind,
            transport_ref={"handle_id": handle.handle_id, **dict(handle.ref)},
            owner=actor,
        )

    def _record_turn_submitted(self, session: Session) -> None:
        session.last_progress_at = self._now()
        session.last_progress_event = "turn.submitted"
        session.lifecycle_state = "ACTIVE"

    @staticmethod
    def _durable_resume_ref(session: Session) -> dict[str, Any]:
        return _durable_resume_ref(session)

    def _effective_input_stamp(self, session: Session, created_at: float | None) -> float:
        """盖章后的水位值（不落章）：非法/未来时间戳退化为本机当前时间。"""
        value = float(created_at or 0.0)
        now = self._now()
        if not (value > 0 and math.isfinite(value) and value <= now + 60.0):
            value = now
        return max(session.last_user_input_at, value)

    def _stamp_last_user_input(self, session: Session, created_at: float | None) -> None:
        """ADR 0057 水位盖章：非法/未来时间戳绝不允许污染水位。"""
        session.last_user_input_at = self._effective_input_stamp(session, created_at)

    _STALE_INBOUND_TOLERANCE_SECONDS = 5.0
    _STALE_INBOUND_MIN_AGE_SECONDS = 30.0

    def _inbound_is_stale_for_session(self, inbound: InboundEvent, session: Session) -> bool:
        """ADR 0057：离线滞留的旧消息不追着已被推进的会话跑。

        断连期间飞书会积压事件、重连后原样补推；若期间会话已被更新的输入
        （终端或更晚的频道消息）推进，滞留消息的语境已失效——自动提交
        （乃至触发复活）弊大于利。双保险防时钟撞车：仅当消息比会话最近
        一次用户输入早 5 秒以上、且自身已滞留 ≥30 秒才拦；同源（频道对
        频道）比较全走飞书同一时钟，无偏差。宁可放过，不可错杀。
        """
        created = float(getattr(inbound, "created_at", 0.0) or 0.0)
        if created <= 0 or session.last_user_input_at <= 0:
            return False
        if self._now() - created < self._STALE_INBOUND_MIN_AGE_SECONDS:
            return False
        return created < session.last_user_input_at - self._STALE_INBOUND_TOLERANCE_SECONDS

    def _remember_replayable_turn(
        self,
        session: Session,
        turn: TurnInput,
        actor: ActorRef,
        *,
        attempt: int,
        stamp_value: float,
    ) -> tuple[str, dict[str, Any] | None]:
        """ADR 0058：记住 worker 已接受、但可能死于回答之前的最后一条提交。

        R2：在**提交发出前**登记（而非提交成功后）——否则提交等待期间
        worker EOF，排水看到 pending_turn_lost 时暂存里还是上一条已完成
        消息，会把它误重放（R2 复现 old-new-old 的另一半）。返回
        (replay_id, 被覆盖的旧条目)；失败路径由调用方调
        _rollback_replayable_turn 还原（R3：光删不还原会把上一条仍在
        等回答的有效暂存永久丢掉）。
        """
        previous = self._turn_replays.get(session.session_id)
        if not (turn.text.strip() or turn.attachments):
            # 空提交只为盖水位（ADR 0057 R2），没有内容可重放——并且它是
            # 更新的人类输入，必须让旧暂存一并失效，否则空提交丢失时会
            # 误重放上一条早已回答过的消息（审查 R1 tests#4）。
            self._turn_replays.pop(session.session_id, None)
            return "", previous
        replay_id = uuid.uuid4().hex
        self._turn_replays[session.session_id] = {
            # 身份钉子：新提交覆盖暂存后，还睡在退避里的旧重放任务靠它
            # 识别自己已被取代——比 0.5s 水位容差硬（审查 R1 consistency#3
            # 的 old-new-old 复现就死在这根钉子上）。
            "replay_id": replay_id,
            "turn": turn,
            "actor": actor,
            "attempt": int(attempt),
            # 与成功后落章**同一个**预计算值（R3：落章时重读当前时间会让
            # 无时间戳消息在慢提交后越过自己的水位，自我封禁重放）。
            "watermark": stamp_value,
        }
        return replay_id, previous

    def _rollback_replayable_turn(
        self,
        session_id: str,
        replay_id: str,
        previous: dict[str, Any] | None,
    ) -> None:
        """还原一次预登记到提交前的状态——仅当没被更新的提交覆盖。"""
        current = self._turn_replays.get(session_id)
        if replay_id:
            if current is not None and current.get("replay_id") == replay_id:
                if previous is not None:
                    self._turn_replays[session_id] = previous
                else:
                    self._turn_replays.pop(session_id, None)
        elif current is None and previous is not None:
            # 空提交把旧条目 pop 掉后自身失败：槽位还空着才还原。
            self._turn_replays[session_id] = previous

    def _maybe_schedule_turn_replay(self, session: Session) -> float | None:
        """pending_turn_lost 时调度自动重放；返回延迟秒数，None=不重放。

        2026-07-20 15:47 事故：复活后的 worker 撞上模型 API 故障窗，死于
        回答之前；当时只有一句走事件流的"请重发"——还被代际围栏丢掉了，
        用户在飞书上盲等半小时。重放给故障窗一个自愈的机会。
        """
        entry = self._turn_replays.get(session.session_id)
        if entry is None:
            return None
        attempt = int(entry.get("attempt", 0))
        delays = self.turn_replay_delays
        if attempt >= len(delays):
            # 退避用尽：清掉暂存（下一条人话重新计数），调用方换终局文案。
            _log_degrade(
                "turn_replay_exhausted",
                session_id=session.session_id,
                attempts=attempt,
            )
            self._turn_replays.pop(session.session_id, None)
            return None
        delay = float(delays[attempt])
        task = asyncio.create_task(
            self._replay_lost_turn(session.session_id, dict(entry), delay)
        )
        self._turn_replay_tasks.add(task)
        task.add_done_callback(self._turn_replay_tasks.discard)
        return delay

    async def _replay_lost_turn(
        self, session_id: str, entry: dict[str, Any], delay: float
    ) -> None:
        await asyncio.sleep(delay)
        try:
            session = self.sessions.get(session_id)
        except Exception as exc:
            _log_degrade(
                "turn_replay_skipped",
                session_id=session_id,
                reason="session_lookup_failed",
                error=exc,
            )
            return
        turn: TurnInput = entry["turn"]
        watermark = float(entry.get("watermark", 0.0))
        # 让位围栏（每条都留 grep 痕迹——"承诺过自动重发然后消失"必须能
        # 从日志复盘，审查 R1 observability#1）：
        # 1) 身份钉子：暂存已被更新的提交覆盖 → 本任务作废；
        # 2) 会话被 TUI 认领或正在跑别的回合 → UX 归继任者；
        # 3) 水位越章 → 有更新的人话进来。
        current = self._turn_replays.get(session_id)
        if current is None or current.get("replay_id") != entry.get("replay_id"):
            _log_degrade(
                "turn_replay_skipped",
                session_id=session_id,
                reason="superseded",
            )
            return
        if session.lifecycle_state in {"EXTERNAL_OBSERVED_READONLY", "ACTIVE"}:
            _log_degrade(
                "turn_replay_skipped",
                session_id=session_id,
                reason="lifecycle",
                lifecycle=session.lifecycle_state,
            )
            return
        if session.last_user_input_at > watermark + _TURN_REPLAY_WATERMARK_TOLERANCE_SECONDS:
            _log_degrade(
                "turn_replay_skipped",
                session_id=session_id,
                reason="watermark_advanced",
                watermark=watermark,
                last_user_input_at=session.last_user_input_at,
            )
            return
        attempt = int(entry.get("attempt", 0)) + 1
        _log_degrade(
            "turn_replay_attempt",
            session_id=session_id,
            attempt=attempt,
            delay=delay,
        )
        try:
            result = await self.submit_user_input(
                session_id,
                turn,
                actor=entry["actor"],
                generation=session.generation,
                replay_attempt=attempt,
                replay_guard=str(entry.get("replay_id", "")),
            )
        except Exception as exc:
            # 重放自己失败不能再沉默——直发通知，不走可能被围栏丢弃的
            # 事件流。之后不再自动续命：交还给人。
            _log_degrade(
                "turn_replay_failed",
                session_id=session_id,
                attempt=attempt,
                error=exc,
            )
            with contextlib.suppress(Exception):
                await self._send_session_view(
                    session,
                    {
                        "type": "error",
                        "message": (
                            f"⚠️ 自动重发第 {attempt} 次失败"
                            f"（{type(exc).__name__}）；请稍后手动重发一次。"
                        ),
                    },
                    idempotency_key=f"turn-replay-failed:{session_id}:{attempt}",
                )
            return
        if not result.accepted:
            _log_degrade(
                "turn_replay_rejected",
                session_id=session_id,
                attempt=attempt,
                reason=result.reason,
            )
            # 拒因分两类：所有权类（时效守卫、只读、接管中……）静默让位，
            # UX 归继任者；故障类（worker 拉不起来、缺恢复凭据）没有继任
            # 者会替我们说话——用户拿着"稍后自动重发"的承诺在等，必须
            # 直发一句实话（审查 R1 feasibility#2）。
            if str(result.reason) in {"resume_failed", "missing_resume_ref", "transport_not_wired"}:
                with contextlib.suppress(Exception):
                    await self._send_session_view(
                        session,
                        {
                            "type": "error",
                            "message": (
                                f"⚠️ 自动重发失败（{result.reason}）；"
                                "请稍后手动重发一次。"
                            ),
                        },
                        idempotency_key=f"turn-replay-rejected:{session_id}:{attempt}",
                    )

    def _revival_transport_ready(self, session: Session) -> bool:
        """Whether an ADR 0054 revival could actually be carried out here.

        Binding resolution must not route a message into a stopped session
        this runtime cannot resume (e.g. an old claude session in a topic now
        served by a codex-only runtime) — that would dead-end at 会话已结束
        where the pre-0054 path would have started a fresh session.
        """
        transport = self.transports.get(session.transport_kind)
        if transport is None:
            return False
        try:
            return bool(transport.capabilities().resume_after_complete)
        except Exception:
            return False

    async def prepare_turn_from_inbound(self, inbound: InboundEvent) -> TurnInput | SubmitResult:
        if not inbound.attachments:
            return TurnInput(text=inbound.text, created_at=inbound.created_at)
        channel = self.channels.get(inbound.channel_kind)
        if channel is None or not channel.capabilities().attachment_download:
            return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        attachments = [await channel.download_attachment(attachment) for attachment in inbound.attachments]
        return TurnInput(text=inbound.text, attachments=attachments, created_at=inbound.created_at)

    async def close_session(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        reason: str,
        mode: str = "graceful",
    ) -> ControlResult:
        session = self.sessions.get(session_id)
        authz_result = self._authorize_session_control(session_id, actor, action="close")
        if not authz_result.allowed:
            return ControlResult(False, authz_result.reason)
        if session.status == "stopped":
            return ControlResult(True, state="stopped")
        # .get, not [] : "external_tui" is a transport_kind with no transport
        # object behind it (the process lives in somebody's terminal), so an
        # indexed lookup would KeyError instead of closing the ledger record.
        transport = self.transports.get(session.transport_kind)
        shutdown = getattr(transport, "shutdown", None) if transport is not None else None
        if shutdown is not None:
            result = await shutdown(self._handle_for_session(session), mode)
            if not result.accepted:
                return result
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.stop_reason = reason
        session.writer_owner = WriterOwner(kind="none")
        # The worker (and its subagents) die with the close; a stopped card
        # must not keep advertising background work.
        session.background_tasks = []
        await self.refresh_session_status_card(session)
        return ControlResult(True, state="stopped")

    async def reload_session_backend(
        self,
        session_id: str,
        *,
        actor: ActorRef,
    ) -> ControlResult:
        """Cycle the agent backend under a session, keeping the conversation.

        Why this cannot just be "close and let the user re-ask": the whole
        point is that the CONVERSATION survives. Config picked up at backend
        start — MCP servers above all — is otherwise unreachable for a
        long-running session, because codex's app-server snapshots
        ``mcp_servers`` at process start and ``thread/resume`` reuses that
        snapshot (only ``thread/start`` re-reads the file). Restarting the
        process is the only way in, and abandoning the thread to get it costs
        the user everything they built up.

        Two steps, in this order:

        1. ``close_session`` with ``backend_reload`` — a reason inside
           ``_CHANNEL_REVIVAL_STOP_REASONS``, so the next message revives THIS
           session instead of dead-ending at 会话已结束. The transport's
           shutdown runs here, so the worker/turn stops before anything is
           torn down under it.
        2. the transport's optional ``restart_backend``. Claude needs none —
           closing reaps the per-session worker and the next resume spawns a
           fresh one. Codex does: one app-server serves every thread on the
           profile, and its config snapshot only refreshes on process start.
        """
        session = self.sessions.get(session_id)
        transport_kind = session.transport_kind
        if session.status != "stopped" and session.lifecycle_state in _TURN_IN_FLIGHT_STATES:
            # Refuse rather than kill a turn the user is waiting on — see
            # _TURN_IN_FLIGHT_STATES for why that loss is silent on codex.
            return ControlResult(False, "turn_in_flight")
        if session.status != "stopped" and not _durable_resume_ref(session):
            # No resumable identity yet (codex before its first turn, claude
            # before the first result carried agent_session_id). Cycling the
            # backend here would stop the session with nothing to revive it
            # from — /reload would silently become a permanent kill.
            return ControlResult(False, "missing_resume_ref")
        result = await self.close_session(session_id, actor=actor, reason="backend_reload")
        if not result.accepted:
            return result
        transport = self.transports.get(transport_kind)
        restart = getattr(transport, "restart_backend", None) if transport is not None else None
        if restart is None:
            return ControlResult(True, state="reloaded")
        try:
            await restart()
        except Exception as exc:
            # The session is already stopped-but-revivable, so the next
            # message still works — it just reconnects to the OLD backend and
            # silently misses the new config. Say so rather than reporting a
            # reload that did not happen.
            _log_degrade(
                "backend_restart_failed",
                session_id=session_id,
                transport_kind=transport_kind,
                error=exc,
            )
            return ControlResult(False, "backend_restart_failed")
        # Shared backend: siblings on this profile lost their connection too,
        # and the caller has to say so instead of letting them look broken.
        return ControlResult(True, state="reloaded_shared_backend")

    async def set_session_model(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        model: str,
    ) -> ControlResult:
        try:
            result = await self._run_transport_control(
                session_id,
                actor=actor,
                action="set_model",
                capability="set_model",
                invoke=lambda transport, handle: transport.set_model(handle, model),
            )
        except Exception as exc:  # noqa: BLE001 - the provider's refusal is the answer the user needs
            # e.g. Claude: 'Couldn't confirm model "x" with the API' when the
            # route does not serve that model. Reported as a failed switch
            # ("模型切换失败：<reason>") instead of an unconfirmed inbound.
            _log_degrade("set_model_failed", session_id=session_id, model=model, error=exc)
            return ControlResult(False, str(exc) or type(exc).__name__)
        if result.accepted:
            self.sessions.get(session_id).model = model
        return result

    async def archive_session(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        reason: str,
    ) -> ControlResult:
        try:
            self.sessions.get(session_id)
        except KeyError:
            return ControlResult(False, BlockedReason.NOT_FOUND)
        authz_result = self._authorize_session_control(session_id, actor, action="archive")
        if not authz_result.allowed:
            return ControlResult(False, authz_result.reason)
        return self.sessions.archive_session(session_id, actor=actor, reason=reason)

    async def _run_transport_control(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        action: str,
        capability: str,
        invoke,
    ) -> ControlResult:
        session = self.sessions.get(session_id)
        authz_result = self._authorize_session_control(session_id, actor, action=action)
        if not authz_result.allowed:
            return ControlResult(False, authz_result.reason)
        if session.status == "stopped":
            return ControlResult(False, BlockedReason.SESSION_STOPPED)
        transport = self.transports[session.transport_kind]
        if not getattr(transport.capabilities(), capability):
            return ControlResult(False, BlockedReason.CAPABILITY_DISABLED)
        return await invoke(transport, self._handle_for_session(session))

    def check_session_health(self, session_id: str, *, progress_timeout: float) -> SessionHealth:
        session = self.sessions.get(session_id)
        now = self._now()
        elapsed = max(0.0, now - (session.running_since or session.created_at or now))
        last_progress_at = session.last_progress_at or session.running_since or session.created_at
        stale = (
            session.status != "stopped"
            and progress_timeout > 0
            and last_progress_at > 0
            and now - last_progress_at >= progress_timeout
        )
        if session.status == "stopped":
            status = "stopped"
            reason = session.stop_reason
        elif stale:
            status = "stale"
            reason = "progress_timeout"
        elif session.lifecycle_state == "WAITING_PERMISSION":
            status = "waiting_permission"
            reason = session.last_progress_event
        elif session.lifecycle_state == "WAITING_USER":
            status = "waiting_user"
            reason = session.last_progress_event
        elif session.lifecycle_state == "IDLE":
            status = "idle"
            reason = session.last_progress_event
        elif session.lifecycle_state.startswith("ERROR"):
            status = "error"
            reason = session.last_progress_event
        else:
            status = "running"
            reason = session.last_progress_event
        view = ViewModelFactory.health_view(
            status=status,
            title=session.cached_title or session.session_id,
            session_id=session.session_id,
            agent_session_id=_agent_session_identity(session),
            transport=session.transport_kind,
            elapsed=elapsed,
            cwd=session.cwd,
            lifecycle_state=session.lifecycle_state,
            writer_owner=session.writer_owner.kind if session.writer_owner is not None else "",
            last_progress_event=session.last_progress_event,
            last_event_seq=session.last_event_seq,
            readonly=bool(session.writer_owner and session.writer_owner.kind == "external_tui"),
            model=session.model,
            context_used=_estimate_context_tokens(session.last_usage),
            context_limit=_context_window_limit(
                session.model,
                _estimate_context_tokens(session.last_usage),
                session.last_usage,
            ),
            background_tasks=len(session.background_tasks),
        )
        view["reason"] = reason
        view["stale"] = stale
        view["last_progress_event"] = session.last_progress_event
        view["last_event_seq"] = session.last_event_seq
        return SessionHealth(
            session_id=session.session_id,
            status=status,
            reason=reason,
            stale=stale,
            elapsed=elapsed,
            last_progress_at=last_progress_at,
            last_progress_event=session.last_progress_event,
            last_event_seq=session.last_event_seq,
            view_model=view,
        )

    # Progress events that flip on every observed tool/message beat. They only
    # add churn to the card's "进展" line; the tool-progress card is where that
    # detail lives. Folding them keeps the fingerprint stable through a turn.
    #
    # Two families reach `last_progress_event`, and for a long time this only
    # covered one of them:
    #   `external_tui.<hook>`  — written by the TUI hook path
    #   `<AgentEventType>`     — written by _record_session_progress from the
    #                            event stream, i.e. "tool.started",
    #                            "turn.delta", …
    # The second family matched nothing, so every single tool event changed the
    # fingerprint and cost one status-card patch. Measured 2026-08-04: that was
    # the largest single consumer of a 10000/month Lark quota — one tool call
    # billed two patches (started + completed) for a card whose only visible
    # delta was the "进展" line.
    #
    # turn.completed and the permission/ask/error types are deliberately NOT
    # folded: those are real state changes the card exists to show.
    _STATUS_NOISE_PROGRESS = re.compile(
        r"^(?:"
        r"external_tui\.(?:pre-tool|post-tool|post-tool-failure|post-tool-batch|"
        r"message-display|notification|user-prompt-submit)"
        r"|tool\.(?:started|completed|failed)"
        r"|turn\.(?:delta|narration)"
        r"|background\.tasks"
        r")$"
    )

    @classmethod
    def _status_card_fingerprint(cls, view: dict[str, Any]) -> str:
        data = {
            key: value
            for key, value in view.items()
            # elapsed ticks on every render and last_event_seq on every event;
            # neither is a material state change worth an API call.
            if key not in {"elapsed", "last_event_seq"}
        }
        for key in ("last_progress_event", "reason"):
            value = str(data.get(key, "") or "")
            if cls._STATUS_NOISE_PROGRESS.match(value):
                data[key] = "external_tui.activity"
        return json.dumps(data, sort_keys=True, ensure_ascii=False)

    # Once a binding is proven undeliverable (bot removed from the chat, or the
    # tenant's monthly API quota is gone), every further send fails the same
    # way. Non-essential surfaces check this first so a dead binding costs one
    # failure instead of one per agent event — the 2026-08-04 logs had ~5100
    # such failures, most of a 10000/month quota, from exactly this pattern.
    _DELIVERY_DEAD_KEY = "delivery_dead"

    @staticmethod
    def _mark_delivery_dead(binding: ChannelBinding, error: BaseException) -> None:
        if isinstance(error, PermanentDeliveryError):
            binding.capabilities[Orchestrator._DELIVERY_DEAD_KEY] = str(error)[:200]

    @staticmethod
    def _delivery_is_dead(binding: ChannelBinding) -> str:
        return str(binding.capabilities.get(Orchestrator._DELIVERY_DEAD_KEY, "") or "")

    @staticmethod
    def revive_delivery(binding: ChannelBinding) -> None:
        """Clear the dead-delivery latch.

        Called when the channel proves it can reach us again (an inbound event
        on this binding): the bot was re-added, or the quota month rolled over.
        """
        binding.capabilities.pop(Orchestrator._DELIVERY_DEAD_KEY, None)

    def _root_card_edit_may_retry(
        self,
        binding: ChannelBinding,
        session: Session,
        message_id: str,
        error: BaseException | None,
    ) -> bool:
        """True when a failed root-card edit should be retried in place.

        The status card IS the thread root here. A replacement card can only
        be sent as a reply UNDER that root (Lark has no "replace the root
        message" API), so the generic fallback would move the pointer onto a
        child card forever: later refreshes would edit the child while the
        root — what the collapsed thread list shows — stays frozen at whatever
        it was created with, typically the raw session id. So a transient
        failure keeps the pointer and retries; the caller deliberately does
        not record a fingerprint, which is what lets the retry through.

        But retrying forever is its own outage: a root that is deleted,
        recalled, or past its edit window fails identically on every event,
        burning API quota while the user watches a status card that never
        updates again. A permanent delivery error, or a spent retry budget,
        therefore gives up on the root and lets the caller demote to a child
        card — a stale root headline beats no live status at all.
        """
        permanent = isinstance(error, PermanentDeliveryError)
        failures = int(binding.capabilities.get("root_card_edit_failures", 0) or 0) + 1
        binding.capabilities["root_card_edit_failures"] = failures
        if not permanent and failures < ROOT_CARD_EDIT_RETRY_BUDGET:
            _log_degrade(
                "status_card_root_edit_failed",
                session_id=session.session_id,
                message_id=message_id,
                failures=failures,
                fallback="retry_next_refresh",
            )
            return True
        _log_degrade(
            "status_card_root_demoted",
            session_id=session.session_id,
            message_id=message_id,
            failures=failures,
            permanent=permanent,
            fallback="child_status_card",
        )
        return False

    async def _maybe_refresh_session_title(
        self,
        session: Session,
        *,
        user_text: str = "",
        assistant_text: str = "",
    ) -> bool:
        """Single entry point for session-title updates; True if it changed.

        Every path that ends a turn funnels through here — TUI hooks for the
        two CLIs, the event stream for the two structured transports — so the
        rank and throttle rules live in one place instead of being re-derived
        per transport. Callers refresh the status card right after; the card's
        fingerprint check swallows the no-op when the title is unchanged.

        Async on purpose even though the current generator is pure: the LLM
        version replaces compose_session_title with an awaited call and no
        call site has to change.
        """
        title, source = compose_session_title(
            user_text=user_text, assistant_text=assistant_text
        )
        if not title:
            return False
        new_rank = _session_title_source_rank(source)
        current_rank = _session_title_source_rank(session.title_source)
        if new_rank < current_rank:
            # A weaker source never overwrites a stronger one. This is what
            # keeps a codex takeover/handback from repainting an LLM title
            # with the raw first prompt every time ownership flips.
            return False
        now = self._now()
        if new_rank == current_rank:
            if source not in SESSION_TITLE_ROLLING_SOURCES:
                # One-shot source already spent: the first prompt stays the
                # title, later prompts in the same session do not repaint it.
                return False
            if title == session.cached_title:
                return False
            if now - session.title_refreshed_at < SESSION_TITLE_REFRESH_INTERVAL_SECONDS:
                return False
        session.cached_title = title
        session.title_source = source
        session.title_refreshed_at = now
        return True

    async def refresh_session_status_card(self, session: Session) -> None:
        binding = session.channel_binding
        if binding is None or not bool(binding.capabilities.get("status_card")):
            return
        if self._delivery_is_dead(binding):
            return
        channel = self.channels.get(binding.channel_kind)
        if channel is None:
            return
        health = self.check_session_health(session.session_id, progress_timeout=0)
        view = dict(health.view_model)
        view["actions"] = self._status_card_actions(session)
        message_id = str(binding.health_message_id or "")
        fingerprint = self._status_card_fingerprint(view)
        # Keyed by the message the fingerprint was taken ON, not just the
        # session: a session can change status cards mid-life (the rootless
        # Lark thread heals onto a fresh root card, an edit fails and falls
        # back to a new send). A session-only key would match the OLD card's
        # fingerprint and skip the first refresh of the new one, freezing it
        # at whatever it was created with. Comparing the pair makes a card
        # swap self-invalidating instead of something each caller must
        # remember to clear.
        if (
            message_id
            and self._status_card_fingerprints.get(session.session_id)
            == (message_id, fingerprint)
        ):
            return
        if message_id and channel.capabilities().editable_message:
            edit_error: BaseException | None = None
            try:
                edited = await channel.edit_view(binding, message_id, view)
            except Exception as exc:
                edited = False
                edit_error = exc
                _log_degrade(
                    "status_card_edit_failed",
                    session_id=session.session_id,
                    message_id=message_id,
                    error=exc,
                )
            if edited:
                # A working edit clears the root-card failure budget: the
                # budget exists to escape a broken root, not to count lifetime
                # blips on a healthy one.
                binding.capabilities.pop("root_card_edit_failures", None)
                self._status_card_fingerprints[session.session_id] = (message_id, fingerprint)
                return
            if message_id == binding.root_message_id and self._root_card_edit_may_retry(
                binding, session, message_id, edit_error
            ):
                return
            binding.health_message_id = ""
        try:
            new_message_id = await channel.send_view(binding, view)
        except Exception as exc:
            # The status card is the only surface where "idle but background
            # tasks running" shows up; a silent drop here would make that
            # state undiagnosable.
            self._mark_delivery_dead(binding, exc)
            _log_degrade(
                "status_card_send_failed",
                session_id=session.session_id,
                error=exc,
                drop=True,
                delivery_dead=bool(self._delivery_is_dead(binding)),
            )
            return
        if new_message_id:
            binding.health_message_id = str(new_message_id)
            binding.last_message_id = str(new_message_id)
            self._status_card_fingerprints[session.session_id] = (
                str(new_message_id),
                fingerprint,
            )

    @staticmethod
    def _status_card_actions(session: Session) -> list[dict[str, Any]]:
        if not _session_is_external_tui_takeover_candidate(session):
            return []
        return [{"action": "request_takeover", "label": "Take over"}]

    def _authorize_session_control(
        self,
        session_id: str,
        actor: ActorRef,
        *,
        action: str,
    ) -> AuthorizationResult:
        if self.authz is None:
            return AuthorizationResult(True)
        return self.authz.can_control_session(session_id, actor, action=action)

    @staticmethod
    def _handle_for_session(session: Session) -> TransportHandle:
        return TransportHandle(
            handle_id=str(session.transport_ref.get("handle_id", "")),
            transport_kind=session.transport_kind,
            ref=dict(session.transport_ref),
        )

    def _interaction_transport(self, session: Session) -> AgentTransport | None:
        """Transport that carries HITL decisions for this session.

        TUI-observed sessions have ``transport_kind == "external_tui"`` which
        has no transport of its own; their permission / AskUserQuestion
        decisions go to the PreToolUse gate spool (ADR 0046 v2, ADR 0068).
        """
        transport = self.transports.get(session.transport_kind)
        if transport is not None:
            return transport
        if _session_is_external_tui_takeover_candidate(session) and _external_claude_resume_ref(session):
            return self.transports.get("claude_gate")
        return None

    async def handle_inbound_event(
        self,
        inbound: InboundEvent,
        *,
        agent_transport_kind: str,
        cwd: str,
    ) -> SubmitResult:
        ledger_started = False
        if self.inbound_ledger is not None and not self.inbound_ledger.start(inbound.event_id):
            return SubmitResult(False, BlockedReason.DUPLICATE_INBOUND)
        ledger_started = self.inbound_ledger is not None
        # Traffic on this binding proves the channel can reach us again — the
        # bot was re-added, or the quota month rolled over. Lift the latch so
        # status cards and progress cards resume.
        resolved = self.sessions.resolve_binding(inbound.binding_key())
        if resolved:
            revived = self.sessions.get(resolved)
            if revived is not None and revived.channel_binding is not None:
                self.revive_delivery(revived.channel_binding)
        try:
            if inbound.callback:
                await self._ack_callback_event(inbound)
                result = await self._handle_callback_event(inbound)
            else:
                key = inbound.binding_key()
                actor = ActorRef(inbound.channel_kind, inbound.sender_id, inbound.sender_display)
                if _is_takeover_command(inbound.text):
                    result = await self._handle_takeover_request_callback(inbound)
                else:
                    awaiting = self.interactions.awaiting_context_for_binding(key)
                    if awaiting is not None:
                        # A stopped session's question has no asker left. A
                        # dead wait must not capture the plain message that
                        # would otherwise revive the session (ADR 0054) or
                        # reach normal routing.
                        try:
                            awaiting_session = self.sessions.get(awaiting.session_id)
                        except KeyError:
                            awaiting_session = None
                        if awaiting_session is None or awaiting_session.status == "stopped":
                            self.interactions.clear_awaiting_other_for_session(awaiting.session_id)
                            awaiting = None
                    if awaiting is not None:
                        session = self.sessions.get(awaiting.session_id)
                        result = None
                        if self._inbound_is_stale_for_session(inbound, session):
                            # ADR 0057：代际围栏只挡接管后的旧答案；会话被
                            # 更新输入推进但代际未变时，滞留文本仍会被当成
                            # 问题答案吃掉——同样按时效拦。
                            _log_degrade(
                                "stale_inbound_refused",
                                session_id=session.session_id,
                                created_at=inbound.created_at,
                                last_user_input_at=session.last_user_input_at,
                                kind="awaiting_answer",
                            )
                            result = SubmitResult(False, "stale_inbound")
                        if self.authz is not None:
                            authz_result = self.authz.can_submit(session.session_id, actor)
                            if not authz_result.allowed:
                                result = SubmitResult(False, authz_result.reason)
                        if result is None:
                            transport = self._interaction_transport(session)
                        if result is None and (
                            transport is None or not transport.capabilities().ask_user_question
                        ):
                            result = SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
                        if result is None:
                            decision = self.interactions.answer_awaiting_other(
                                key,
                                actor=actor,
                                text=inbound.text,
                                current_generation=session.generation,
                            )
                            if decision.accepted:
                                self._stamp_last_user_input(session, inbound.created_at)
                                await self._handle_ask_user_decision(
                                    session,
                                    awaiting,
                                    decision,
                                    actor=actor,
                                    idempotency_key=f"{inbound.event_id}:ask_user_answer",
                                )
                            result = SubmitResult(decision.accepted, decision.reason)
                    else:
                        resolution = self.sessions.resolve_active_binding(
                            key, revival_eligible=self._revival_transport_ready
                        )
                        if resolution.reason:
                            if resolution.reason == BlockedReason.AMBIGUOUS_SESSION:
                                await self._send_session_chooser(inbound)
                                result = SubmitResult(True, resolution.reason)
                            else:
                                result = SubmitResult(False, resolution.reason)
                        elif not resolution.session_id:
                            binding = ChannelBinding(
                                channel_kind=inbound.channel_kind,
                                account_id=inbound.account_id,
                                chat_id=inbound.chat_id,
                                thread_id=inbound.thread_id,
                                root_message_id=self._root_message_id_for_new_binding(inbound),
                                capabilities=self._new_binding_capabilities(inbound),
                            )
                            preset_card = str(
                                (inbound.raw or {}).get("_walkcode_status_card_id", "") or ""
                            ) if isinstance(inbound.raw, dict) else ""
                            if preset_card:
                                # The channel ingress already sent the status card
                                # as the thread root; register it so refreshes
                                # patch that card instead of sending a second one.
                                binding.health_message_id = preset_card
                            session = await self.start_session(
                                binding,
                                agent_transport_kind,
                                cwd,
                                actor,
                            )
                            turn = await self.prepare_turn_from_inbound(inbound)
                            if isinstance(turn, SubmitResult):
                                result = turn
                            else:
                                result = await self.submit_user_input(
                                    session.session_id,
                                    turn,
                                    actor=actor,
                                    generation=session.generation,
                                    ack_message_id=inbound.message_id,
                                )
                        else:
                            session = self.sessions.get(resolution.session_id)
                            if self._inbound_is_stale_for_session(inbound, session):
                                _log_degrade(
                                    "stale_inbound_refused",
                                    session_id=session.session_id,
                                    created_at=inbound.created_at,
                                    last_user_input_at=session.last_user_input_at,
                                )
                                result = SubmitResult(False, "stale_inbound")
                            elif isinstance(turn := await self.prepare_turn_from_inbound(inbound), SubmitResult):
                                result = turn
                            else:
                                result = await self.submit_user_input(
                                    session.session_id,
                                    turn,
                                    actor=actor,
                                    generation=session.generation,
                                    ack_message_id=inbound.message_id,
                                )
        except Exception:
            if ledger_started and self.inbound_ledger is not None:
                self.inbound_ledger.fail(inbound.event_id)
            raise
        if ledger_started and self.inbound_ledger is not None:
            if _submit_result_completes_inbound_ledger(result):
                self.inbound_ledger.complete(inbound.event_id)
            else:
                self.inbound_ledger.fail(inbound.event_id)
        return result

    @staticmethod
    def _root_message_id_for_new_binding(inbound: InboundEvent) -> str:
        if inbound.root_message_id:
            return inbound.root_message_id
        if inbound.thread_id:
            return ""
        return inbound.message_id

    @staticmethod
    def _new_binding_capabilities(inbound: InboundEvent) -> dict[str, Any]:
        capabilities: dict[str, Any] = {}
        if inbound.channel_kind == "lark" and inbound.thread_id:
            capabilities["status_card"] = True
            capabilities["native_topic"] = True
            capabilities["origin"] = inbound.channel_kind
        title = _title_from_text(inbound.text)
        if title:
            capabilities["initial_title"] = title
        return capabilities

    async def _send_session_chooser(self, inbound: InboundEvent) -> None:
        channel = self.channels.get(inbound.channel_kind)
        if channel is None:
            return
        sessions = [
            item
            for item in self.sessions.list_sessions(
                channel_kind=inbound.channel_kind,
                account_id=inbound.account_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
            )
            if item.status != "stopped"
        ]
        binding = ChannelBinding(
            channel_kind=inbound.channel_kind,
            account_id=inbound.account_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
            root_message_id=inbound.root_message_id or inbound.message_id,
        )
        await channel.send_view(
            binding,
            ViewModelFactory.session_chooser(
                reason=BlockedReason.AMBIGUOUS_SESSION,
                sessions=sessions,
            ),
        )

    async def _ack_callback_event(self, inbound: InboundEvent) -> None:
        channel = self.channels.get(inbound.channel_kind)
        if channel is None or not channel.capabilities().private_callback_ack:
            return
        await channel.ack_callback(inbound)

    async def _flip_decided_card(
        self,
        inbound: InboundEvent,
        *,
        kind: str,
        tool_name: str = "",
        action: str = "",
        detail: str = "",
    ) -> None:
        # Replace the interactive prompt with a terminal result card so a
        # settled request stops showing live buttons (avoids the "ran without
        # my approval?" confusion and blocks stale double-clicks). Best-effort:
        # the decision already took effect regardless of this edit.
        channel = self.channels.get(inbound.channel_kind)
        if channel is None or not inbound.message_id:
            return
        if not channel.capabilities().editable_message:
            return
        binding = ChannelBinding(
            channel_kind=inbound.channel_kind,
            account_id=inbound.account_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
            root_message_id=inbound.root_message_id or inbound.message_id,
        )
        view = ViewModelFactory.decision_result(
            kind=kind, tool_name=tool_name, action=action, detail=detail
        )
        # Lark patch occasionally fails with transient 2200 Internal Error;
        # a single silent attempt left live buttons on settled cards. Retry
        # briefly, then leave a trace instead of vanishing.
        last_exc: Exception | None = None
        for delay in (0.0, 0.5, 2.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                if await channel.edit_view(binding, inbound.message_id, view):
                    return
                # A False return is a failure too (adapter refused the edit):
                # treating it as success left live buttons on settled cards.
            except Exception as exc:
                last_exc = exc
        _log_degrade(
            "decided_card_flip_failed",
            kind=kind,
            message_id=inbound.message_id,
            error=f"{type(last_exc).__name__}: {last_exc}" if last_exc else "edit_view returned False",
        )

    async def _notify_interaction_delivery_failure(
        self,
        inbound: InboundEvent,
        session: Session,
        ctx: InteractionContext,
        exc: Exception,
    ) -> None:
        # The decision is on record but never reached the worker (typically:
        # the runtime restarted and the in-flight prompt died with the old
        # process). Silence here reads as "I clicked and nothing happened" —
        # tell the user and retire the card instead.
        _log_degrade(
            "interaction_decision_delivery_failed",
            kind=ctx.kind,
            session_id=ctx.session_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        binding = session.channel_binding
        channel = self.channels.get(binding.channel_kind) if binding is not None else None
        if channel is not None and binding is not None:
            try:
                await channel.send_view(
                    binding,
                    {
                        "type": "text",
                        "text": (
                            "⚠️ 选择已记录，但这张卡片对应的会话进程已经不在了"
                            "（服务重启过）。请回到根会话发一条新消息重新开始。"
                        ),
                    },
                )
            except Exception:
                pass
        await self._flip_decided_card(
            inbound,
            kind=ctx.kind,
            tool_name=ctx.tool_name,
            action="stale",
            detail="会话进程已重启，这张卡片已失效。",
        )

    async def _notify_gate_decision_failure(
        self,
        inbound: InboundEvent,
        session: Session,
        ctx: InteractionContext,
        exc: "claude_gate.GateDecisionFailed",
    ) -> None:
        # The click did not reach a waiting hook: say so on the card instead
        # of pretending it took effect.
        _log_degrade(
            "gate_decision_failed",
            kind=ctx.kind,
            session_id=ctx.session_id,
            reason=exc.reason,
            error=str(exc),
        )
        if exc.reason == "already_resolved":
            action, detail = "terminal", "已在终端处理（或对话框已变化），本卡片未生效。"
        else:
            action, detail = "stale", "这个请求已经结束或服务重启过，这张卡片已失效；如终端仍在等待，请直接在终端处理。"
        await self._flip_decided_card(
            inbound,
            kind=ctx.kind,
            tool_name=ctx.tool_name,
            action=action,
            detail=detail,
        )

    async def _handle_callback_event(self, inbound: InboundEvent) -> SubmitResult:
        token = str((inbound.callback or {}).get("token", ""))
        data = str((inbound.callback or {}).get("data", "") or token)
        if data in {"request_takeover", "takeover"}:
            return await self._handle_takeover_request_callback(inbound)
        if not token:
            return SubmitResult(False, BlockedReason.INVALID_TOKEN)
        ctx = self.interactions.context_for_token(token)
        if ctx is None:
            return SubmitResult(False, BlockedReason.INVALID_TOKEN)
        try:
            session = self.sessions.get(ctx.session_id)
        except KeyError:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        actor = ActorRef(inbound.channel_kind, inbound.sender_id, inbound.sender_display)
        if ctx.kind == "permission":
            transport = self._interaction_transport(session)
            if self.authz is not None:
                authz_result = self.authz.can_decide_permission(
                    ctx.session_id,
                    actor,
                    high_risk=ctx.high_risk,
                )
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if transport is None or not transport.capabilities().permission_callback:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        elif ctx.kind == "ask_user_question":
            transport = self._interaction_transport(session)
            if self.authz is not None:
                authz_result = self.authz.can_submit(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if transport is None or not transport.capabilities().ask_user_question:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        elif ctx.kind == "takeover":
            if self.authz is not None:
                authz_result = self.authz.can_takeover(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if ctx.generation != session.generation:
                return SubmitResult(False, BlockedReason.STALE_GENERATION)
        elif ctx.kind == "model_choice":
            transport = self._interaction_transport(session)
            if self.authz is not None:
                authz_result = self.authz.can_submit(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if transport is None or not transport.capabilities().set_model:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        # Lark form cards submit every field in one callback: fold the form
        # values into the pending answers before the submit token is decided.
        form_values = (inbound.callback or {}).get("form")
        if ctx.kind == "ask_user_question" and isinstance(form_values, dict) and form_values:
            self.interactions.apply_ask_user_form(
                token, form_values, current_generation=session.generation
            )
        decision = self.interactions.decide_from_token(
            token,
            actor=actor,
            current_generation=session.generation,
            binding_key=inbound.binding_key(),
        )
        if decision.accepted and ctx.kind == "permission":
            transport = self._interaction_transport(session)
            if transport is None:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
            approval_decision = dict(decision.decision or {})
            if session.transport_kind == "codex_app_server":
                approval_decision["_tool_input"] = dict(ctx.tool_input)
            try:
                await transport.approve_permission(
                    self._handle_for_session(session),
                    ctx.transport_request_id or ctx.interaction_id,
                    approval_decision,
                )
            except claude_gate.GateDecisionFailed as exc:
                # No hook is waiting for this click any more (timed out to the
                # terminal, runtime restarted, or already decided).
                await self._notify_gate_decision_failure(inbound, session, ctx, exc)
                return SubmitResult(False, BlockedReason.NOT_FOUND)
            except TransportUnavailable as exc:
                # Only the stale-worker path (runtime restarted, worker gone)
                # retires the card. Any other failure must keep propagating so
                # the inbound ledger fails and the click stays retryable —
                # flipping those to "stale" would silently eat the decision.
                await self._notify_interaction_delivery_failure(inbound, session, ctx, exc)
                return SubmitResult(False, BlockedReason.NOT_FOUND)
            if ctx.hitl_request_id:
                self.hitls.mark_decided(ctx.hitl_request_id)
            await self._flip_decided_card(
                inbound,
                kind="permission",
                tool_name=ctx.tool_name,
                action=str(approval_decision.get("action", "")),
            )
        if decision.accepted and ctx.kind == "ask_user_question":
            try:
                await self._handle_ask_user_decision(
                    session,
                    ctx,
                    decision,
                    actor=actor,
                    idempotency_key=f"{inbound.event_id}:ask_user_view",
                    edit_card=inbound,
                )
            except claude_gate.GateDecisionFailed as exc:
                await self._notify_gate_decision_failure(inbound, session, ctx, exc)
                return SubmitResult(False, BlockedReason.NOT_FOUND)
            except TransportUnavailable as exc:
                # Same narrowing as the permission branch: only worker-gone is
                # stale. Local prompt sends (incomplete/awaiting_other/update
                # branches) raising here must not retire a still-valid card.
                await self._notify_interaction_delivery_failure(inbound, session, ctx, exc)
                return SubmitResult(False, BlockedReason.NOT_FOUND)
            # Final answer (all questions done) → flip the clicked card to a
            # result; toggle/next-question keep the card interactive.
            if str((decision.decision or {}).get("action", "")) == "answers":
                await self._flip_decided_card(
                    inbound,
                    kind="ask_user_question",
                    action="answers",
                    detail=_format_ask_answers(ctx),
                )
        if decision.accepted and ctx.kind == "takeover":
            takeover_action = str((decision.decision or {}).get("action", ""))
            try:
                takeover_result = await self._handle_takeover_decision(
                    session,
                    ctx,
                    decision,
                    actor=actor,
                )
            except Exception:
                # Unexpected failure (e.g. the TUI terminate step raising a
                # non-TakeoverError): the button token is already consumed, so
                # the card must still flip to a terminal state before the
                # error propagates — otherwise it keeps inviting clicks that
                # can never succeed.
                await self._flip_decided_card(
                    inbound,
                    kind="takeover",
                    action=takeover_action or "takeover",
                    detail="接管过程中出错。本卡片已失效；重新发送一条消息可再次触发接管。",
                )
                raise
            # The button token was consumed the moment it was clicked, so the
            # card must ALWAYS flip to a terminal state — leaving a live
            # button after a failure invites clicks that can never succeed
            # (review round 3). Failures flip to a failure notice that points
            # at the recovery path (send a new message → fresh prompt).
            if takeover_result.accepted or takeover_result.reason in {
                "keep_readonly",
                TakeoverPhase.MANUAL_ONLY,
            }:
                detail = {
                    "takeover_and_send": "已接管会话并发送消息",
                    "request_takeover": "已接管会话",
                    "keep_readonly": "保持只读，本条输入已取消",
                    "manual_instructions": "已发送手动接管指引",
                }.get(takeover_action, "")
            else:
                detail = (
                    f"接管未完成（{takeover_result.reason or 'failed'}）。"
                    "本卡片已失效；重新发送一条消息可再次触发接管。"
                )
            await self._flip_decided_card(
                inbound,
                kind="takeover",
                action=takeover_action or "takeover",
                detail=detail,
            )
            return takeover_result
        if decision.accepted and ctx.kind == "model_choice":
            model = str((decision.decision or {}).get("action", ""))
            result = await self.set_session_model(session.session_id, actor=actor, model=model)
            channel = self.channels.get(inbound.channel_kind)
            if channel is not None:
                reply_binding = session.channel_binding or ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id=inbound.thread_id,
                    root_message_id=inbound.root_message_id or inbound.message_id,
                )
                text = f"✅ 模型已切换：{model}" if result.accepted else f"模型切换失败：{result.reason}"
                await channel.send_view(reply_binding, {"type": "text", "text": text})
            if result.accepted:
                # Flip the picker to a result card so the settled choice stops
                # showing live buttons (a second click would hit the consumed
                # token and read as an error).
                await self._flip_decided_card(
                    inbound,
                    kind="model_choice",
                    action=model,
                    detail=f"模型已切换：{model}",
                )
            return SubmitResult(result.accepted, result.reason)
        return SubmitResult(decision.accepted, decision.reason)

    async def _handle_takeover_request_callback(self, inbound: InboundEvent) -> SubmitResult:
        resolution = self.sessions.resolve_active_binding(inbound.binding_key())
        if resolution.reason:
            return SubmitResult(False, resolution.reason)
        if not resolution.session_id:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        session = self.sessions.get(resolution.session_id)
        if self._inbound_is_stale_for_session(inbound, session):
            # ADR 0057：滞留的控制命令比普通旧输入更危险——旧 /takeover
            # 补推进来会改变 writer 归属，必须同样拦下。
            _log_degrade(
                "stale_inbound_refused",
                session_id=session.session_id,
                created_at=inbound.created_at,
                last_user_input_at=session.last_user_input_at,
                kind="takeover_command",
            )
            return SubmitResult(False, "stale_inbound")
        actor = ActorRef(inbound.channel_kind, inbound.sender_id, inbound.sender_display)
        if self.authz is not None:
            authz_result = self.authz.can_takeover(session.session_id, actor)
            if not authz_result.allowed:
                return SubmitResult(False, authz_result.reason)
        if not _session_is_external_tui_takeover_candidate(session):
            return SubmitResult(False, BlockedReason.NOT_EXTERNAL_TUI)
        try:
            tx = self.sessions.request_takeover_only(
                session.session_id,
                requested_by=actor,
                generation=session.generation,
            )
        except TakeoverError as exc:
            return SubmitResult(False, exc.reason)
        if tx.phase == TakeoverPhase.FAILED:
            return SubmitResult(False, tx.reason or "takeover_failed", blocked_input_id=tx.blocked_input_id)
        if tx.phase == TakeoverPhase.MANUAL_ONLY:
            return SubmitResult(False, TakeoverPhase.MANUAL_ONLY, blocked_input_id=tx.blocked_input_id)
        if tx.phase == TakeoverPhase.COMPLETED:
            return SubmitResult(True, blocked_input_id=tx.blocked_input_id)
        ctx = self.interactions.register_takeover(
            session_id=session.session_id,
            generation=tx.requested_generation,
            takeover_id=tx.takeover_id,
            blocked_input_id=tx.blocked_input_id,
        )
        return await self._handle_takeover_decision(
            session,
            ctx,
            DecisionResult(True, decision={"action": "takeover_and_send"}),
            actor=actor,
        )

    async def _send_takeover_prompt(
        self,
        session: Session,
        blocked_input_id: str,
        *,
        requested_by: ActorRef,
        generation: int,
    ) -> None:
        try:
            tx = self.sessions.request_takeover(
                session.session_id,
                blocked_input_id,
                requested_by=requested_by,
                generation=generation,
            )
        except TakeoverError:
            return
        blocked = session.blocked_inputs.get(blocked_input_id)
        summary = blocked.text if blocked is not None else ""
        await self._send_takeover_prompt_for_transaction(session, tx, summary=summary)

    async def _send_takeover_prompt_for_transaction(
        self,
        session: Session,
        tx: TakeoverTransaction,
        *,
        summary: str,
    ) -> None:
        resume_ref = self._takeover_resume_ref(session)
        recoverability = "native_resume_available" if resume_ref is not None else "not_importable"
        ctx = self.interactions.register_takeover(
            session_id=session.session_id,
            generation=tx.requested_generation,
            takeover_id=tx.takeover_id,
            blocked_input_id=tx.blocked_input_id,
        )
        view = ViewModelFactory(self.interactions).takeover_prompt_for_context(
            ctx,
            recoverability=recoverability,
            summary=summary,
        )
        await self._send_session_view(
            session,
            view,
            idempotency_key=f"takeover_prompt:{tx.takeover_id}",
        )

    async def _handle_takeover_decision(
        self,
        session: Session,
        ctx: InteractionContext,
        decision: DecisionResult,
        *,
        actor: ActorRef,
    ) -> SubmitResult:
        action = str((decision.decision or {}).get("action", ""))
        takeover_id = str(ctx.tool_input.get("takeover_id", ""))
        blocked_input_id = str(ctx.tool_input.get("blocked_input_id", ""))
        blocked = session.blocked_inputs.get(blocked_input_id)
        summary = blocked.text if blocked is not None else ""
        if action == "keep_readonly":
            if blocked is not None and blocked.state == "blocked":
                blocked.state = "cancelled"
            await self.refresh_session_status_card(session)
            return SubmitResult(False, "keep_readonly", blocked_input_id=blocked_input_id)
        if action == "manual_instructions":
            try:
                self.sessions.authorize_takeover(
                    takeover_id,
                    approved_by=actor,
                    resume_ref=None,
                )
            except TakeoverError as exc:
                return SubmitResult(False, exc.reason, blocked_input_id=blocked_input_id)
            await self._send_session_view(
                session,
                ViewModelFactory.manual_only(
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    summary=summary,
                ),
                idempotency_key=f"takeover_manual:{takeover_id}",
            )
            await self.refresh_session_status_card(session)
            return SubmitResult(False, TakeoverPhase.MANUAL_ONLY, blocked_input_id=blocked_input_id)
        if action not in {"takeover_and_send", "confirm_takeover"}:
            return SubmitResult(False, BlockedReason.INVALID_TOKEN, blocked_input_id=blocked_input_id)

        resume_ref = self._takeover_resume_ref(session)
        terminate_ref = self._takeover_terminate_ref(session)
        requires_termination = self._takeover_requires_external_tui_termination(session)

        try:
            if resume_ref is None:
                return await self._complete_takeover_as_manual_only(
                    session,
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    actor=actor,
                    summary=summary,
                    reason="missing structured resume reference",
                )
            if requires_termination and terminate_ref is None:
                return await self._complete_takeover_as_manual_only(
                    session,
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    actor=actor,
                    summary=summary,
                    reason="missing TUI process reference",
                )
            transport_kind, transport_ref = self._normalize_takeover_resume_ref(resume_ref)
            transport = self.transports.get(transport_kind)
            if transport is None:
                await self._send_takeover_failed(
                    session,
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    summary=summary,
                    reason="transport unavailable",
                )
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED, blocked_input_id=blocked_input_id)
            if not transport.capabilities().external_tui_takeover:
                await self._send_takeover_failed(
                    session,
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    summary=summary,
                    reason="transport does not support TUI takeover",
                )
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED, blocked_input_id=blocked_input_id)
            controller = None
            process_ref = {}
            if requires_termination:
                controller_kind, process_ref = self._normalize_takeover_terminate_ref(terminate_ref or {})
                if controller_kind == "process" and not bool(process_ref.get("allow_terminate")):
                    return await self._complete_takeover_as_manual_only(
                        session,
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        actor=actor,
                        summary=summary,
                        reason="TUI process termination is not authorized",
                    )
                controller = self.external_tui_controllers.get(controller_kind)
                if controller is None:
                    return await self._complete_takeover_as_manual_only(
                        session,
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        actor=actor,
                        summary=summary,
                        reason="TUI process controller unavailable",
                    )
            self.sessions.authorize_takeover(
                takeover_id,
                approved_by=actor,
                resume_ref=resume_ref,
            )
            await self._send_session_view(
                session,
                ViewModelFactory.takeover_progress(
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    phase="resuming_structured",
                    summary=summary,
                ),
                idempotency_key=f"takeover_resuming:{takeover_id}",
            )
            try:
                resumed_handle = await transport.resume(
                    ResumeSpec(
                        cwd=session.cwd,
                        session_id=session.session_id,
                        resume_ref=transport_ref,
                    )
                )
            except Exception as exc:
                # State first, log second: the takeover is already AUTHORIZED,
                # so a raise out of the (stderr-writing) log call must not
                # leave the transaction stuck mid-takeover.
                self.sessions.fail_takeover(takeover_id, reason="resume_failed")
                # ADR 0059 R1 fixed the writer path's flattened "resume_failed"
                # hiding the real cause; the takeover path needs the same trace
                # (e.g. oversized thread/resume response vs dead app-server).
                _log_degrade(
                    "takeover_resume_failed",
                    session_id=session.session_id,
                    takeover_id=takeover_id,
                    error=exc,
                )
                await self._send_session_view(
                    session,
                    ViewModelFactory.takeover_progress(
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        phase="failed",
                        summary=summary,
                        reason="resume_failed",
                    ),
                    idempotency_key=f"takeover_failed:{takeover_id}",
                )
                return SubmitResult(False, "resume_failed", blocked_input_id=blocked_input_id)
            if requires_termination:
                await self._send_session_view(
                    session,
                    ViewModelFactory.takeover_progress(
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        phase="terminating_external_tui",
                        summary=summary,
                    ),
                    idempotency_key=f"takeover_terminating:{takeover_id}",
                )
                own_session = agent_session_id("claude_headless", resume_ref or {})
                termination = await controller.terminate(
                    # ADR 0067: the controller re-checks, right before every
                    # signal, that the process still runs this session — the
                    # user may /clear or /resume away at any point until then.
                    {**process_ref, "expected_claude_session": own_session} if own_session else process_ref,
                    reason=f"takeover:{takeover_id}",
                )
                if not termination.accepted:
                    self.sessions.fail_takeover(
                        takeover_id,
                        reason=termination.reason or "external_tui_termination_failed",
                    )
                    await self._rollback_resumed_takeover_handle(transport, resumed_handle)
                    await self._send_session_view(
                        session,
                        ViewModelFactory.takeover_progress(
                            takeover_id=takeover_id,
                            blocked_input_id=blocked_input_id,
                            phase="failed",
                            summary=summary,
                            reason=termination.reason or "external_tui_termination_failed",
                        ),
                        idempotency_key=f"takeover_terminate_failed:{takeover_id}",
                    )
                    return SubmitResult(
                        False,
                        termination.reason or "external_tui_termination_failed",
                        blocked_input_id=blocked_input_id,
                    )
            self.sessions.complete_takeover(
                takeover_id,
                transport_kind=transport_kind,
                transport_ref={"handle_id": resumed_handle.handle_id, **dict(resumed_handle.ref)},
            )
            updated = self.sessions.get(session.session_id)
            stale_requests = await self._mark_pre_takeover_hitls_stale(
                updated,
                through_generation=ctx.generation,
                takeover_id=takeover_id,
            )
            blocked = updated.blocked_inputs.get(blocked_input_id)
            if blocked is None:
                return SubmitResult(False, BlockedReason.NOT_FOUND, blocked_input_id=blocked_input_id)
            if not blocked.submit_after_takeover:
                await self._send_session_view(
                    updated,
                    ViewModelFactory.takeover_progress(
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        phase="completed",
                        summary=summary,
                    ),
                    idempotency_key=f"takeover_completed:{takeover_id}",
                )
                await self.refresh_session_status_card(updated)
                # ADR 0051: a takeover-only handoff that orphaned pending
                # prompts leaves the agent silently waiting for an answer
                # nobody can deliver. Re-drive it with an invisible continue
                # turn so it re-asks and the channel gets a fresh answerable
                # card. Only here — a takeover carrying user text lets that
                # text drive the continuation instead (no double prompt).
                if stale_requests and self.handoff_continue == "auto":
                    handle = self._handle_for_session(updated)
                    try:
                        await transport.submit_turn(
                            handle,
                            TurnInput(text=HANDOFF_CONTINUE_PROMPT),
                            idempotency_key=f"handoff_continue:{takeover_id}",
                        )
                    except Exception as exc:
                        # Non-fatal: the takeover itself succeeded; the user
                        # can still type to continue.
                        _log_degrade(
                            "handoff_continue_submit_failed",
                            session_id=updated.session_id,
                            takeover_id=takeover_id,
                            stale_hitl_count=len(stale_requests),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        return SubmitResult(True, blocked_input_id=blocked_input_id)
                    # Distinguishable submit-success signal for operators
                    # (the live-E2E gate before flipping the default relies
                    # on being able to tell "injected" from "never fired").
                    updated.last_progress_at = self._now()
                    updated.last_progress_event = "handoff_continue.submitted"
                    if self.defer_event_drain:
                        # Serve mode holds the ingress lock here: the
                        # injected turn may pop a fresh HITL card whose
                        # answer callback needs that lock — same deferral
                        # rule as submit_user_input.
                        self._start_background_event_drain(
                            updated.session_id, transport, handle
                        )
                    else:
                        try:
                            await self._drain_events(updated, transport, handle)
                        except Exception as exc:
                            _log_degrade(
                                "handoff_continue_drain_failed",
                                session_id=updated.session_id,
                                takeover_id=takeover_id,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                return SubmitResult(True, blocked_input_id=blocked_input_id)
            handle = self._handle_for_session(updated)
            await self._send_session_view(
                updated,
                ViewModelFactory.takeover_progress(
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    phase="submitting_blocked_input",
                    summary=summary,
                ),
                idempotency_key=f"takeover_submitting:{takeover_id}",
            )
            try:
                await transport.submit_turn(
                    handle,
                    TurnInput(text=blocked.text, attachments=list(blocked.attachments)),
                    idempotency_key=blocked.idempotency_key,
                )
            except Exception:
                blocked.state = "not_delivered"
                await self._send_session_view(
                    updated,
                    ViewModelFactory.takeover_progress(
                        takeover_id=takeover_id,
                        blocked_input_id=blocked_input_id,
                        phase="failed",
                        summary=summary,
                        reason="submit_failed",
                    ),
                    idempotency_key=f"takeover_submit_failed:{takeover_id}",
                )
                return SubmitResult(False, "submit_failed", blocked_input_id=blocked_input_id)
            # Terminal state for the take-over-and-send path. Without it the
            # thread ends at "sending your message..." and a slow first model
            # response reads as a dead takeover; the reply itself can lag by
            # minutes (client-side request timeout is 10 minutes).
            await self._send_session_view(
                updated,
                ViewModelFactory.takeover_progress(
                    takeover_id=takeover_id,
                    blocked_input_id=blocked_input_id,
                    phase="submitted_blocked_input",
                    summary=summary,
                ),
                idempotency_key=f"takeover_submitted:{takeover_id}",
            )
            if self.defer_event_drain:
                # Serve mode holds the ingress lock here; the session-level
                # listener can now outlive the turn by hours (background
                # subagents, wait ceiling), so a synchronous drain would block
                # every subsequent inbound event. Same deferral rule as
                # submit_user_input.
                self._start_background_event_drain(updated.session_id, transport, handle)
            else:
                await self._drain_events(updated, transport, handle)
            await self.refresh_session_status_card(updated)
            return SubmitResult(True, blocked_input_id=blocked_input_id)
        except TakeoverError as exc:
            return SubmitResult(False, exc.reason, blocked_input_id=blocked_input_id)

    async def _mark_pre_takeover_hitls_stale(
        self,
        session: Session,
        *,
        through_generation: int,
        takeover_id: str,
    ) -> list[HitlRequest]:
        stale_requests = self.hitls.mark_pending_for_session_stale(
            session.session_id,
            through_generation=through_generation,
        )
        self.interactions.clear_awaiting_other_for_session(
            session.session_id,
            through_generation=through_generation,
        )
        for request in stale_requests:
            await self._send_session_view(
                session,
                ViewModelFactory.stale_hitl_after_takeover(request),
                idempotency_key=f"hitl_stale_after_takeover:{takeover_id}:{request.hitl_request_id}",
            )
        return stale_requests

    async def settle_hitls_for_external_claim(
        self,
        session: Session,
        *,
        prior_transport_kind: str,
        prior_handle: TransportHandle | None,
        through_generation: int,
    ) -> list[HitlRequest]:
        """External TUI claimed a structured session (ADR 0051).

        The old worker's pending prompts can never be answered from the
        channel again — the claim bumped the generation, so every stored
        callback token is already dead. Two cleanups the generation bump
        does NOT do by itself:

        - shut the prior structured worker down so its blocked
          ``can_use_tool`` futures resolve now (default deny) instead of
          hanging until the permission timeout;
        - flip the channel-side cards to an explicit stale notice, so the
          user learns the prompt moved to the terminal *before* clicking
          into a stale-generation rejection.

        Order matters: the user-visible sweep runs FIRST — the claim is
        invoked from the ingress-locked hook path, so the worker shutdown
        must never gate card flips (and is bounded below for the same
        reason: a wedged old worker must not block ingress).
        """
        stale_requests = self.hitls.mark_pending_for_session_stale(
            session.session_id,
            through_generation=through_generation,
        )
        self.interactions.clear_awaiting_other_for_session(
            session.session_id,
            through_generation=through_generation,
        )
        for request in stale_requests:
            await self._send_session_view(
                session,
                ViewModelFactory.stale_hitl_after_takeover(
                    request,
                    reason="The session was resumed in a terminal TUI; answer there instead.",
                ),
                idempotency_key=(
                    f"hitl_stale_after_claim:{session.session_id}:"
                    f"{session.generation}:{request.hitl_request_id}"
                ),
            )
        transport = self.transports.get(prior_transport_kind)
        shutdown = getattr(transport, "shutdown", None) if transport is not None else None
        if shutdown is not None and prior_handle is not None and prior_handle.handle_id:
            try:
                result = await asyncio.wait_for(
                    shutdown(prior_handle, "external_tui_claim"),
                    timeout=EXTERNAL_CLAIM_SHUTDOWN_TIMEOUT_SECONDS,
                )
                if result is not None and not getattr(result, "accepted", True):
                    _log_degrade(
                        "external_claim_shutdown_failed",
                        session_id=session.session_id,
                        error=f"rejected: {getattr(result, 'reason', '') or 'unknown'}",
                    )
            except Exception as exc:
                # Best-effort: a worker from a previous runtime is already
                # gone; anything else still resolves via the prompt timeout.
                _log_degrade(
                    "external_claim_shutdown_failed",
                    session_id=session.session_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return stale_requests

    @staticmethod
    async def _rollback_resumed_takeover_handle(transport: AgentTransport, handle: TransportHandle) -> None:
        shutdown = getattr(transport, "shutdown", None)
        if shutdown is None:
            return
        try:
            await shutdown(handle, "takeover_rollback")
        except Exception:
            return

    async def _complete_takeover_as_manual_only(
        self,
        session: Session,
        *,
        takeover_id: str,
        blocked_input_id: str,
        actor: ActorRef,
        summary: str,
        reason: str,
    ) -> SubmitResult:
        try:
            tx = self.sessions.authorize_takeover(
                takeover_id,
                approved_by=actor,
                resume_ref=None,
            )
            tx.reason = reason
        except TakeoverError as exc:
            return SubmitResult(False, exc.reason, blocked_input_id=blocked_input_id)
        await self._send_session_view(
            session,
            ViewModelFactory.manual_only(
                takeover_id=takeover_id,
                blocked_input_id=blocked_input_id,
                summary=summary,
                reason=reason,
                suggested_steps=[],
            ),
            idempotency_key=f"takeover_manual:{takeover_id}",
        )
        return SubmitResult(False, TakeoverPhase.MANUAL_ONLY, blocked_input_id=blocked_input_id)

    async def _send_takeover_failed(
        self,
        session: Session,
        *,
        takeover_id: str,
        blocked_input_id: str,
        summary: str,
        reason: str,
    ) -> None:
        try:
            self.sessions.fail_takeover(takeover_id, reason=reason)
        except TakeoverError:
            pass
        await self._send_session_view(
            session,
            ViewModelFactory.takeover_progress(
                takeover_id=takeover_id,
                blocked_input_id=blocked_input_id,
                phase="failed",
                summary=summary,
                reason=reason,
            ),
            idempotency_key=f"takeover_failed:{takeover_id}",
        )

    @staticmethod
    def _takeover_requires_external_tui_termination(session: Session) -> bool:
        terminate_ref = Orchestrator._takeover_terminate_ref(session) or {}
        if terminate_ref.get("controller_kind") == SHARED_APP_SERVER_CONTROLLER:
            # Both the TUI and WalkCode are clients of the daemon that owns the
            # thread: WalkCode resumes it alongside the TUI, no process to stop.
            return False
        if session.writer_owner is not None and session.writer_owner.kind == "external_tui":
            if session.status == "stopped" or session.lifecycle_state in {
                "EXTERNAL_DETACHED_IMPORTABLE",
                "EXTERNAL_DETACHED_UNIMPORTABLE",
            }:
                return False
            # ADR 0067: the TUI process moved on to another session (/clear,
            # /resume). Stopping it would kill the terminal running THAT
            # session; this one is no longer being written by anyone.
            return not Orchestrator._claude_tui_switched_away(session)
        return False

    @staticmethod
    def _claude_tui_switched_away(session: Session) -> bool:
        """Is the Claude TUI recorded for this session now running a different session?"""
        own = agent_session_id("claude_headless", Orchestrator._takeover_resume_ref(session) or {})
        controller_kind, process_ref = Orchestrator._normalize_takeover_terminate_ref(
            Orchestrator._takeover_terminate_ref(session) or {}
        )
        if not own or controller_kind != "process":
            return False
        try:
            pid = int(process_ref.get("pid", 0) or 0)
        except (TypeError, ValueError):
            return False
        current = claude_tui_current_session(pid, str(process_ref.get("lstart", "") or ""))
        return bool(current) and current != own

    @staticmethod
    def _takeover_resume_ref(session: Session) -> dict[str, Any] | None:
        refs: list[dict[str, Any]] = []
        if isinstance(session.transport_ref, dict):
            refs.append(session.transport_ref)
        if session.writer_owner is not None and isinstance(session.writer_owner.external_ref, dict):
            refs.append(session.writer_owner.external_ref)
        for ref in refs:
            resume_ref = ref.get("resume_ref")
            if isinstance(resume_ref, dict) and resume_ref:
                return dict(resume_ref)
        return None

    @staticmethod
    def _takeover_terminate_ref(session: Session) -> dict[str, Any] | None:
        refs: list[dict[str, Any]] = []
        if isinstance(session.transport_ref, dict):
            refs.append(session.transport_ref)
        if session.writer_owner is not None and isinstance(session.writer_owner.external_ref, dict):
            refs.append(session.writer_owner.external_ref)
        for ref in refs:
            terminate_ref = ref.get("terminate_ref")
            if isinstance(terminate_ref, dict) and terminate_ref:
                return dict(terminate_ref)
        return None

    @staticmethod
    def _normalize_takeover_resume_ref(resume_ref: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        transport_kind = str(resume_ref.get("transport_kind", "") or resume_ref.get("kind", ""))
        raw_transport_ref = resume_ref.get("transport_ref")
        if isinstance(raw_transport_ref, dict):
            transport_ref = dict(raw_transport_ref)
        else:
            transport_ref = {
                key: value
                for key, value in resume_ref.items()
                if key not in {"transport_kind", "kind", "transport_ref"}
            }
        return transport_kind, transport_ref

    @staticmethod
    def _normalize_takeover_terminate_ref(terminate_ref: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        controller_kind = str(terminate_ref.get("controller_kind", "") or terminate_ref.get("kind", ""))
        raw_process_ref = terminate_ref.get("process_ref")
        if isinstance(raw_process_ref, dict):
            process_ref = dict(raw_process_ref)
        else:
            process_ref = {
                key: value
                for key, value in terminate_ref.items()
                if key not in {"controller_kind", "kind", "process_ref"}
            }
        return controller_kind, process_ref

    async def _handle_ask_user_decision(
        self,
        session: Session,
        ctx: InteractionContext,
        decision: DecisionResult,
        *,
        actor: ActorRef,
        idempotency_key: str,
        edit_card: InboundEvent | None = None,
    ) -> None:
        payload = decision.decision or {}
        action = payload.get("action")
        if action == "answers":
            transport = self._interaction_transport(session)
            if transport is None:
                return
            answers = payload.get("answers", {})
            if not isinstance(answers, dict):
                answers = {}
            else:
                answers = dict(answers)
            if session.transport_kind == "codex_app_server":
                answers["_questions"] = [dict(question) for question in ctx.questions]
            await transport.answer_user_question(
                self._handle_for_session(session),
                ctx.transport_request_id or ctx.interaction_id,
                answers,
            )
            if ctx.hitl_request_id:
                self.hitls.mark_decided(ctx.hitl_request_id)
            return
        if action == "incomplete":
            # Submit arrived with unanswered questions: keep the card open and
            # tell the user which ones are missing.
            missing = payload.get("missing", [])
            titles = []
            for index in missing if isinstance(missing, list) else []:
                try:
                    question = ctx.questions[int(index)]
                except (ValueError, IndexError, TypeError):
                    continue
                titles.append(str(question.get("header") or question.get("prompt") or f"第{index}题"))
            note = "、".join(t for t in titles if t) or "部分问题"
            binding = session.channel_binding
            channel = self.channels.get(binding.channel_kind) if binding is not None else None
            if channel is not None and binding is not None:
                await channel.send_view(
                    binding,
                    {"type": "text", "text": f"⚠️ 还有未回答的问题：{note}。请补选后再点「提交全部」。"},
                )
            return
        if action == "awaiting_other":
            # Keep the all-questions card intact; just prompt for the free-text
            # reply that will fill this one question.
            binding = session.channel_binding
            channel = self.channels.get(binding.channel_kind) if binding is not None else None
            if channel is not None and binding is not None:
                await channel.send_view(
                    binding,
                    {"type": "text", "text": "✏️ 请在本话题里直接回复你的自定义答案文本。"},
                )
            return
        if action == "update":
            # set/toggle/free-text mutated a pending answer → re-render the same
            # card in place (edit) so the batch card doesn't spam new copies.
            view = ViewModelFactory(self.interactions).ask_user_question_prompt(ctx)
            binding = session.channel_binding
            channel = self.channels.get(binding.channel_kind) if binding is not None else None
            if (
                edit_card is not None
                and edit_card.message_id
                and channel is not None
                and channel.capabilities().editable_message
                and binding is not None
            ):
                # edit_view reports failure both ways: False return and
                # raised errors. Either one
                # must fall through to sending a fresh card, with a trace.
                try:
                    edited = await channel.edit_view(binding, edit_card.message_id, view)
                except Exception as exc:
                    edited = False
                    _log_degrade(
                        "ask_card_edit_failed",
                        session_id=session.session_id,
                        interaction_id=ctx.interaction_id,
                        message_id=edit_card.message_id,
                        error=exc,
                        fallback="send_new_card",
                    )
                if edited:
                    return
            await self._send_session_view(session, view, idempotency_key=idempotency_key)

    async def _send_session_view(
        self,
        session: Session,
        view: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> None:
        if session.channel_binding is None:
            return
        self.outbox.enqueue(
            channel_binding_key=session.channel_binding.key(),
            view_model=view,
            idempotency_key=f"{session.session_id}:{session.generation}:{idempotency_key}",
        )
        await self._flush_outbox()

    async def notify_tui_conflict(
        self,
        session: Session,
        *,
        kind: str,
        pid: int = 0,
        command: str = "",
        detail: str = "",
        dedupe_key: str = "",
    ) -> None:
        """Post an explicit TUI-ownership notice to the session's channel topic.

        Silent ownership flips were the root of the 2026-07-19 takeover
        incident ("接管完成" but no replies): the fence protected the data but
        the user saw nothing. Every ownership-relevant TUI event now surfaces.
        """
        await self._send_session_view(
            session,
            ViewModelFactory.tui_conflict_notice(
                kind=kind,
                session_id=session.session_id,
                pid=pid,
                command=command,
                detail=detail,
            ),
            idempotency_key=f"tui_conflict:{kind}:{dedupe_key or pid}",
        )

    def settle_timed_out_gate(self, request: HitlRequest) -> None:
        """Close a blocking-gate prompt whose hook returned without a card
        decision (timed out and handed the prompt to the terminal).

        Only a click used to settle a gate card, so a prompt answered in the
        terminal kept live buttons forever. The HITL request goes stale and
        its interaction is recorded as answered in the terminal, so old
        tokens stop working. The card itself is edited separately by
        ``retire_gate_card``, which may need several passes (``card_open``
        stays set until then).
        """
        request.status = "stale"
        if request.interaction_id:
            try:
                ctx = self.interactions.get(request.interaction_id)
            except KeyError:
                ctx = None
            if ctx is not None and ctx.decision is None:
                ctx.decision = {"action": "terminal"}
                ctx.decided_at = self._now()
                ctx.awaiting_other = None

    async def retire_gate_card(self, session_id: str, rid: str, request: HitlRequest) -> str:
        """Edit a settled gate card into its terminal result.

        Returns "done", "gone" (nothing left to edit: session, channel or the
        card's delivery is gone), "queued" (the card is still waiting in the
        outbox, which bounds its own retries) or "retry" (the edit failed —
        the caller bounds these).
        """
        try:
            session = self.sessions.get(session_id)
        except KeyError:
            return "gone"
        binding = session.channel_binding
        channel = self.channels.get(binding.channel_kind) if binding is not None else None
        if channel is None or not channel.capabilities().editable_message:
            return "gone"
        key = f"{session_id}:{request.generation}:gate:{rid}"
        message_id = request.card_message_id or self.outbox.sent_message_id(key)
        if not message_id:
            # Still queued (a retry may deliver the original card later) or
            # dead / compacted. Only a queued card is worth waiting for.
            return "queued" if self.outbox.is_pending(key) else "gone"
        view = ViewModelFactory.decision_result(
            kind=request.prompt_kind,
            action="terminal",
            detail="飞书上没有及时作答，已转到终端。",
        )
        error = "edit_view returned False"
        try:
            if await channel.edit_view(binding, message_id, view):
                return "done"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        _log_degrade(
            "gate_card_retire_edit_failed",
            session_id=session_id,
            rid=rid,
            message_id=message_id,
            error=error,
        )
        return "retry"

    async def post_claude_gate_prompt(self, session_id: str, request: dict[str, Any]) -> bool:
        """Post a permission / AskUserQuestion card for a PreToolUse gate request.

        Reuses the headless event pipeline end to end: the synthesized
        ``AgentEvent`` flows through ``_event_to_view`` (HITL + interaction
        registration with ``transport_request_id = rid``), so the card
        callback resolves through ``_handle_callback_event`` unchanged and the
        decision lands in the gate spool via ``ClaudeGateTransport``.
        """
        try:
            session = self.sessions.get(session_id)
        except KeyError:
            return False
        if session.channel_binding is None:
            return False
        rid = str(request.get("rid", "") or "")
        if not rid:
            return False
        tool_name = str(request.get("tool_name", "") or "")
        tool_input = request.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        # Keep the card decidable for the whole hook wait window: the token
        # default (10 min) is shorter than the gate timeout (30 min), and a
        # click on a valid-looking card must not silently do nothing.
        deadline = float(request.get("deadline", 0) or 0)
        interaction_ttl = max(60.0, deadline - self._now()) if deadline else 0
        if str(request.get("kind", "")) == claude_gate.KIND_ASK_USER:
            event = AgentEvent(
                AgentEventType.ASK_USER_REQUESTED,
                {
                    "rid": rid,
                    "questions": _ClaudePermissionBridge._map_ask_questions(tool_input),
                    "native_method": "pre_tool_use_hook",
                    "interaction_ttl": interaction_ttl,
                },
            )
        else:
            event = AgentEvent(
                AgentEventType.PERMISSION_REQUESTED,
                {
                    "rid": rid,
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "actions": ["allow", "always_allow", "deny"],
                    "high_risk": _claude_tool_is_high_risk(tool_name),
                    "native_method": "pre_tool_use_hook",
                    "interaction_ttl": interaction_ttl,
                },
            )
        view = self._event_to_view(session, event)
        session.last_event_seq += 1
        session.last_progress_at = self._now()
        session.last_progress_event = f"gate.waiting:{tool_name or 'ask_user_question'}"
        await self._send_session_view(session, view, idempotency_key=f"gate:{rid}")
        await self.refresh_session_status_card(session)
        return True

    async def _drain_events(
        self,
        session: Session,
        transport: AgentTransport,
        handle: TransportHandle,
    ) -> None:
        channel = self.channels[session.channel_binding.channel_kind] if session.channel_binding else None
        if channel is None or session.channel_binding is None:
            return
        # Ownership fence (ADR 0051): a handoff (external TUI claim,
        # takeover) bumps the generation and swaps the transport while this
        # drain may still be streaming the OLD worker's events. Processing
        # them would register fresh-generation HITL cards for a writer that
        # no longer owns the session — cards the stale sweep can never
        # catch. Snapshot ownership at entry and stop the moment it moves.
        expected_generation = session.generation
        expected_transport_kind = session.transport_kind
        last_visible_text = ""
        saw_any_event = False
        open_turn = False
        # Title material for the turn in flight. codex app-server's JSON-RPC
        # `turn/completed` carries only threadId + turn — the assistant text
        # arrived earlier as separate `item/agentMessage/delta` events — so a
        # title built from the completion payload alone would always be empty
        # on that transport. Accumulate the deltas and fall back to them.
        # (The event_msg shape's `task_complete` DOES carry
        # last_agent_message, and Claude's completion carries its own text;
        # those keep winning because the payload is checked first.)
        turn_text_parts: list[str] = []
        turn_text_len = 0
        # Did anything from the turn in flight reach the channel? Tracked so a
        # turn that ends having produced nothing at all can say so instead of
        # closing in silence (see EMPTY_TURN_NOTICE).
        turn_produced_output = False
        async for event in self._iter_transport_events(transport, handle):
            if (
                session.generation != expected_generation
                or session.transport_kind != expected_transport_kind
                or session.status == "stopped"
                or str(session.transport_ref.get("handle_id", "")) != handle.handle_id
            ):
                # Ownership fence (generation/transport moved) — plus closed
                # sessions and handle replacement: a shutdown or a resume to a
                # fresh worker mid-listen still delivers the OLD stream's tail
                # events (EOF warning, synthetic completion, pending-lost
                # error), which must not overwrite the successor's lifecycle.
                _log_degrade(
                    "event_drain_ownership_moved",
                    session_id=session.session_id,
                    expected_generation=expected_generation,
                    current_generation=session.generation,
                    session_status=session.status,
                    drain_handle=handle.handle_id,
                )
                return
            saw_any_event = True
            # The stream now spans turns (background subagents re-open them),
            # so "did the stream die mid-turn" must track the LAST turn's
            # boundary, not whether any result was ever seen.
            if event.type == AgentEventType.TURN_COMPLETED:
                open_turn = False
            elif event.type in {
                AgentEventType.TURN_DELTA,
                AgentEventType.TURN_NARRATION,
                AgentEventType.TOOL_STARTED,
                AgentEventType.TOOL_COMPLETED,
                AgentEventType.TOOL_FAILED,
            }:
                open_turn = True
            if event.type == AgentEventType.TURN_DELTA and turn_text_len < SESSION_TITLE_MATERIAL_CHARS:
                # Sliced to the remaining budget, not merely checked before
                # appending: the transport coalesces a whole batch of deltas
                # into ONE event, so a single event can carry an entire long
                # answer and a check-then-append-whole would blow the cap by
                # orders of magnitude. Only the first non-blank line survives
                # into a 40-char title, so a prefix is all this ever needs.
                delta = str(event.payload.get("text", "") or "")[
                    : SESSION_TITLE_MATERIAL_CHARS - turn_text_len
                ]
                if delta:
                    turn_text_parts.append(delta)
                    turn_text_len += len(delta)
            self._record_session_progress(session, event)
            if (
                event.type == AgentEventType.SESSION_ERROR
                and str(event.payload.get("reason", "")) == "pending_turn_lost"
            ):
                # ADR 0058：已接受的提交随 worker 一起没了。能重放就自动
                # 重放（换掉"请重发"文案）；退避用尽则明说，别让用户猜。
                if event.payload.get("traffic_seen"):
                    # 回合已经流出过输出/工具事件——副作用可能已发生，
                    # 重放等于重复执行（删密钥、发消息、部署……）。作废
                    # 暂存、只说实话，把决定权还给人。
                    self._turn_replays.pop(session.session_id, None)
                    _log_degrade(
                        "turn_replay_refused_partial_execution",
                        session_id=session.session_id,
                    )
                    event.payload["message"] = (
                        "⚠️ 代理进程在执行你的消息中途退出了；为避免重复执行"
                        "已完成的操作，不自动重发——请确认现场后再重发。"
                    )
                else:
                    had_replay_entry = session.session_id in self._turn_replays
                    lost_count = int(event.payload.get("pending_lost", 1) or 1)
                    replay_delay = self._maybe_schedule_turn_replay(session)
                    if replay_delay is not None:
                        extra = (
                            f"（共 {lost_count} 条消息丢失，仅自动重发最后一条，"
                            "更早的请自行重发）"
                            if lost_count > 1
                            else ""
                        )
                        event.payload["message"] = (
                            "⚠️ 代理进程在生成回复前退出了；"
                            f"{replay_delay:g} 秒后自动重发你刚才的消息。{extra}"
                        )
                    elif had_replay_entry:
                        event.payload["message"] = (
                            "⚠️ 代理进程在生成回复前退出了，自动重发已尝试 "
                            f"{len(self.turn_replay_delays)} 次仍未成功；请稍后重发一次。"
                        )
            view = self._event_to_view(session, event)
            session.last_event_seq += 1
            silent_turn = False
            if event.type == AgentEventType.TURN_COMPLETED:
                # Turn-end signal for the two structured transports. codex
                # app-server never loads the user-level hooks.json (it only
                # emits hook events for source="plugin"), so unlike the TUI
                # paths there is no Stop hook to hang the title refresh off —
                # this stream is the only place the turn is known to be over.
                #
                # .strip() before the fallback, not a bare `or`: a completion
                # whose message is whitespace is truthy, and would shadow the
                # accumulated deltas with something that cleans down to "".
                completion_text = str(event.payload.get("message", "") or "").strip()
                await self._maybe_refresh_session_title(
                    session,
                    assistant_text=completion_text or "".join(turn_text_parts),
                )
                # Read BEFORE the turn-end reset below, and only for a real
                # completion: a SESSION_ERROR already speaks for itself.
                silent_turn = not turn_produced_output
            if event.type in {
                AgentEventType.TURN_COMPLETED,
                AgentEventType.SESSION_ERROR,
            }:
                # BOTH are turn ends (see the listen loop's result handling: an
                # error result closes the turn too). Resetting only on
                # completion would let a failed turn's text leak into the next
                # turn's title on this long-lived stream.
                turn_text_parts.clear()
                turn_text_len = 0
                # A SESSION_ERROR already told the user what went wrong — and
                # codex still sends turn/completed right after one. Resetting
                # here would make that completion look like a silent turn and
                # post a second, redundant warning for the same failure.
                turn_produced_output = event.type == AgentEventType.SESSION_ERROR
                turn_ended = True
            else:
                turn_ended = False
            await self.refresh_session_status_card(session)
            if view.get("type") in {"tool_progress", "turn_narration"}:
                # Narration joins the rolling burst card as a 💬 line — never
                # a bubble, and it must NOT seal the burst (the tools it
                # narrates land right after it).
                delivered = await self._upsert_tool_progress_view(session, channel, view)
                # A diagnostic line ("upstream errored, retrying") is not the
                # agent answering — a turn whose only trace is retry notes must
                # still close with the no-output warning.
                if delivered and not view.get("diagnostic"):
                    turn_produced_output = True
                continue
            if view.get("type") == "background_tasks":
                # Ledger beat: status card is already refreshed above; it must
                # not seal a tool burst (task_started lands right after the
                # Agent tool_use line) nor produce channel text.
                continue
            # Any non-tool event ends the burst — including an empty
            # turn-completed. Sealing must not depend on the event producing
            # visible text, or the next turn's tools edit last turn's card.
            self._seal_tool_progress_burst(session)
            visible_text = render_view_text(view)
            # .strip(): a whitespace-only delta is not something a human can
            # read, and counting it as output would let a turn whose entire
            # payload was "\n\n" close without the notice below — while also
            # posting a blank bubble.
            if not visible_text.strip():
                if not silent_turn:
                    continue
                # The turn ended having produced nothing the user could see.
                # Say so rather than dropping the completion (EMPTY_TURN_NOTICE).
                _log_degrade(
                    "turn_completed_without_output",
                    session_id=session.session_id,
                    transport_kind=session.transport_kind,
                )
                view = {"type": "turn_completed", "message": EMPTY_TURN_NOTICE}
                visible_text = EMPTY_TURN_NOTICE
            if event.type == AgentEventType.TURN_COMPLETED and visible_text == last_visible_text:
                # The completion is repeating text the deltas already showed.
                last_visible_text = ""
                continue
            # The watermark is per-turn, cleared as the turn ends. Letting it
            # live for the whole drain was silent data loss on this resident
            # cross-turn stream (ADR 0060): whenever two turns in a row
            # answered the same thing, the second reply was dropped — exactly
            # the class of silence the notice above exists to end.
            last_visible_text = "" if turn_ended else visible_text
            if event.type not in {
                AgentEventType.TURN_COMPLETED,
                AgentEventType.SESSION_ERROR,
            }:
                turn_produced_output = True
            self.outbox.enqueue(
                channel_binding_key=session.channel_binding.key(),
                view_model=view,
                idempotency_key=(
                    f"{session.session_id}:{session.generation}:"
                    f"{event.seq or session.last_event_seq}:{event.type}"
                ),
            )
            await self._flush_outbox()
        if saw_any_event and open_turn and session.lifecycle_state == "ACTIVE":
            session.last_progress_at = self._now()
            session.last_progress_event = "turn.event_stream_incomplete"
            session.lifecycle_state = "ERROR_RECOVERABLE"

    async def _upsert_tool_progress_view(
        self,
        session: Session,
        channel: ChannelAdapter,
        view: dict[str, Any],
    ) -> bool:
        binding = session.channel_binding
        if binding is None or self._delivery_is_dead(binding):
            return False
        # Accumulate a burst of consecutive tool events into one card that is
        # patched in place. A tool_result (completed/failed) updates its own
        # started line (matched by tool_id) instead of appending a new one.
        # Narration entries (ADR 0055) interleave chronologically as 💬 lines.
        lines = binding.capabilities.get("tool_progress_lines")
        if not isinstance(lines, list):
            lines = []
        if view.get("type") == "turn_narration":
            text = str(view.get("text", "") or "").strip()
            if not text:
                return False
            lines.append({"kind": "narration", "text": text[:600]})
            binding.capabilities["tool_progress_lines"] = lines
            return await self._patch_tool_progress_card(session, channel, lines)
        entry = {
            "tool_name": str(view.get("tool_name", "") or "tool"),
            "status": str(view.get("status", "") or "running"),
            "summary": str(view.get("summary", "") or ""),
            "tool_id": str(view.get("tool_id", "") or ""),
        }
        merged = False
        if entry["tool_id"]:
            for index, existing in enumerate(lines):
                if isinstance(existing, dict) and existing.get("tool_id") == entry["tool_id"]:
                    # tool_result blocks usually omit the tool name/summary, so
                    # keep the ones the tool_use (started) line already carried.
                    if entry["tool_name"] in ("", "tool") and existing.get("tool_name"):
                        entry["tool_name"] = existing["tool_name"]
                    if not entry["summary"] and existing.get("summary"):
                        entry["summary"] = existing["summary"]
                    lines[index] = entry
                    merged = True
                    break
        if not merged:
            lines.append(entry)
        binding.capabilities["tool_progress_lines"] = lines
        return await self._patch_tool_progress_card(
            session, channel, lines, tool=entry["tool_name"], status=entry["status"]
        )

    async def _patch_tool_progress_card(
        self,
        session: Session,
        channel: ChannelAdapter,
        lines: list[Any],
        *,
        tool: str = "narration",
        status: str = "",
    ) -> bool:
        binding = session.channel_binding
        if binding is None:
            return False
        aggregate = {"type": "tool_progress", "lines": [dict(line) for line in lines if isinstance(line, dict)]}
        message_id = str(binding.capabilities.get("tool_progress_message_id", "") or "")
        if message_id and channel.capabilities().editable_message:
            try:
                edited = await channel.edit_view(binding, message_id, aggregate)
            except Exception as exc:
                edited = False
                _log_degrade(
                    "tool_progress_edit_failed",
                    session_id=session.session_id,
                    message_id=message_id,
                    tool=tool,
                    error=exc,
                    fallback="send_new_card",
                )
            if edited:
                return True
            binding.capabilities.pop("tool_progress_message_id", None)
        try:
            new_message_id = await channel.send_view(binding, aggregate)
        except Exception as exc:
            # Progress cards are ephemeral by design (no outbox retry), but the
            # drop must leave a trace or the missing card is undebuggable.
            self._mark_delivery_dead(binding, exc)
            _log_degrade(
                "tool_progress_send_failed",
                session_id=session.session_id,
                tool=tool,
                status=status,
                error=exc,
                drop=True,
                delivery_dead=bool(self._delivery_is_dead(binding)),
            )
            return False
        if new_message_id:
            binding.capabilities["tool_progress_message_id"] = str(new_message_id)
        return bool(new_message_id)

    @staticmethod
    def _seal_tool_progress_burst(session: Session) -> None:
        # A non-tool message (agent text, turn end, a prompt) breaks the burst:
        # drop the rolling handle so the next run of tools starts a fresh card
        # rather than editing one stranded above newer messages.
        binding = session.channel_binding
        if binding is None:
            return
        binding.capabilities.pop("tool_progress_message_id", None)
        binding.capabilities.pop("tool_progress_lines", None)

    def _record_session_progress(self, session: Session, event: AgentEvent) -> None:
        session.last_progress_at = self._now()
        session.last_progress_event = event.type
        model = str(event.payload.get("model", "") or "")
        if model:
            session.model = model
        if event.type == AgentEventType.TURN_COMPLETED:
            usage = event.payload.get("usage")
            if isinstance(usage, dict) and usage:
                session.last_usage = dict(usage)
        if event.type == AgentEventType.BACKGROUND_TASKS:
            tasks = event.payload.get("tasks")
            session.background_tasks = (
                [dict(task) for task in tasks if isinstance(task, dict)]
                if isinstance(tasks, list)
                else []
            )
            # Ledger beats refresh liveness but never move the lifecycle: the
            # session stays IDLE-with-background-work between turns.
            return
        if session.writer_owner and session.writer_owner.kind == "external_tui":
            return
        if event.type in {
            AgentEventType.TURN_DELTA,
            AgentEventType.TURN_NARRATION,
            AgentEventType.TOOL_STARTED,
            AgentEventType.TOOL_COMPLETED,
            AgentEventType.TOOL_FAILED,
        }:
            session.lifecycle_state = "ACTIVE"
        elif event.type == AgentEventType.PERMISSION_REQUESTED:
            session.lifecycle_state = "WAITING_PERMISSION"
        elif event.type == AgentEventType.ASK_USER_REQUESTED:
            session.lifecycle_state = "WAITING_USER"
        elif event.type == AgentEventType.TURN_COMPLETED:
            session.lifecycle_state = "IDLE"
            self._record_durable_resume_ref(session, event)
        elif event.type == AgentEventType.SESSION_ERROR:
            session.lifecycle_state = "ERROR_RECOVERABLE"

    @staticmethod
    def _record_durable_resume_ref(session: Session, event: AgentEvent) -> None:
        if session.transport_kind == "claude_headless":
            agent_session_id = str(
                event.payload.get("agent_session_id")
                or event.payload.get("session_id")
                or ""
            )
            if agent_session_id:
                session.transport_ref["agent_session_id"] = agent_session_id
        thread_id = str(event.payload.get("thread_id") or "")
        if thread_id:
            session.transport_ref["thread_id"] = thread_id

    async def _iter_transport_events(
        self,
        transport: AgentTransport,
        handle: TransportHandle,
    ):
        raw_events = transport.events(handle)
        events = await _maybe_await(raw_events)
        if hasattr(events, "__aiter__"):
            async for event in events:
                yield event
            return
        for event in events:
            yield event

    def _event_to_view(self, session: Session, event: AgentEvent) -> dict[str, Any]:
        if event.type == AgentEventType.TURN_DELTA:
            return {"type": "turn_delta", "text": str(event.payload.get("text", ""))}
        if event.type == AgentEventType.TURN_NARRATION:
            view = {"type": "turn_narration", "text": str(event.payload.get("text", ""))}
            if event.payload.get("diagnostic"):
                view["diagnostic"] = True
            return view
        if event.type == AgentEventType.TURN_COMPLETED:
            return {"type": "turn_completed", "message": str(event.payload.get("message", ""))}
        if event.type == AgentEventType.BACKGROUND_TASKS:
            # Status-card only; renders as no channel text.
            return {"type": "background_tasks", "count": int(event.payload.get("count", 0) or 0)}
        if event.type in {
            AgentEventType.TOOL_STARTED,
            AgentEventType.TOOL_COMPLETED,
            AgentEventType.TOOL_FAILED,
        }:
            status = {
                AgentEventType.TOOL_STARTED: "running",
                AgentEventType.TOOL_COMPLETED: "completed",
                AgentEventType.TOOL_FAILED: "failed",
            }.get(event.type, "running")
            return {
                "type": "tool_progress",
                "status": status,
                "tool_name": str(event.payload.get("tool_name", "") or "tool"),
                "tool_id": str(event.payload.get("tool_id", "") or ""),
                "summary": _compact_tool_summary(event.payload.get("summary", "")),
            }
        if event.type == AgentEventType.PERMISSION_REQUESTED:
            tool_input = event.payload.get("tool_input", {})
            if not isinstance(tool_input, dict):
                tool_input = {"value": tool_input}
            actions = event.payload.get("actions", ["allow_once", "deny"])
            if not isinstance(actions, list):
                actions = ["allow_once", "deny"]
            transport_request_id = str(
                event.payload.get("rid") or event.payload.get("request_id") or ""
            )
            hitl_request = None
            if transport_request_id:
                native_method = str(
                    event.payload.get("native_method")
                    or tool_input.get("native_method")
                    or "permission.requested"
                )
                hitl_request = self.hitls.register_request(
                    session_id=session.session_id,
                    generation=session.generation,
                    transport_kind=session.transport_kind,
                    transport_request_id=transport_request_id,
                    native_method=native_method,
                    prompt_kind="permission",
                )
            ctx = self.interactions.register_permission(
                session_id=session.session_id,
                generation=session.generation,
                tool_name=str(event.payload.get("tool_name", "")),
                tool_input=tool_input,
                actions=[str(action) for action in actions],
                transport_request_id=transport_request_id,
                high_risk=bool(event.payload.get("high_risk", False)),
                hitl_request_id=hitl_request.hitl_request_id if hitl_request else "",
                ttl=float(event.payload.get("interaction_ttl", 0) or 0) or None,
            )
            if hitl_request is not None:
                self.hitls.attach_interaction(hitl_request.hitl_request_id, ctx.interaction_id)
            return ViewModelFactory(self.interactions).permission_prompt(ctx)
        if event.type == AgentEventType.ASK_USER_REQUESTED:
            questions = event.payload.get("questions", [])
            if not isinstance(questions, list) or not questions:
                questions = [{"prompt": str(event.payload.get("prompt", "")), "options": []}]
            valid_questions = [dict(question) for question in questions if isinstance(question, dict)]
            if not valid_questions:
                valid_questions = [{"prompt": str(event.payload.get("prompt", "")), "options": []}]
            transport_request_id = str(
                event.payload.get("rid") or event.payload.get("request_id") or ""
            )
            hitl_request = None
            if transport_request_id:
                hitl_request = self.hitls.register_request(
                    session_id=session.session_id,
                    generation=session.generation,
                    transport_kind=session.transport_kind,
                    transport_request_id=transport_request_id,
                    native_method=str(event.payload.get("native_method") or "ask_user.requested"),
                    prompt_kind="ask_user_question",
                )
            ctx = self.interactions.register_ask_user_question(
                session_id=session.session_id,
                generation=session.generation,
                questions=valid_questions,
                transport_request_id=transport_request_id,
                hitl_request_id=hitl_request.hitl_request_id if hitl_request else "",
                ttl=float(event.payload.get("interaction_ttl", 0) or 0) or None,
            )
            if hitl_request is not None:
                self.hitls.attach_interaction(hitl_request.hitl_request_id, ctx.interaction_id)
            return ViewModelFactory(self.interactions).ask_user_question_prompt(ctx)
        if event.type == AgentEventType.SESSION_ERROR:
            return {"type": "error", "message": str(event.payload.get("message", ""))}
        return {
            "type": "unknown_event",
            "event_type": event.type,
            "text": f"[{event.type}] {event.payload}",
        }


def _submit_result_completes_inbound_ledger(result: SubmitResult) -> bool:
    if result.accepted:
        return True
    return result.reason in {
        BlockedReason.UNAUTHORIZED,
        BlockedReason.DUPLICATE_INBOUND,
        BlockedReason.INVALID_TOKEN,
        BlockedReason.ALREADY_DECIDED,
        BlockedReason.STALE_GENERATION,
        BlockedReason.NOT_FOUND,
        BlockedReason.EXTERNAL_TUI_READONLY,
        # Terminal rejection that replies a note to the sender: retrying the
        # same event would only re-send the note. (LEASE_EXPIRED is no longer
        # produced by validate_submit — ADR 0059 removed the expiry veto.)
        BlockedReason.SESSION_STOPPED,
        # ADR 0057: a stale (stranded) inbound is terminally refused — a
        # redelivery would only be refused again and re-send the note.
        "stale_inbound",
        "keep_readonly",
    }
