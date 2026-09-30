"""Core channel-native contracts: enums, errors, dataclasses and small shared helpers."""

from __future__ import annotations

import contextlib
import inspect
import os
import re
import sys
import tempfile
import time

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


BindingKey = tuple[str, str, str, str, str]


def attachment_download_dir() -> Path:
    """Stable directory that inbound attachments download into.

    Downloads land here (instead of a random spot under the system temp root)
    so the Claude transport can hand the same directory to ``add_dirs``. That
    makes the agent's ``Read`` of a downloaded file a read inside an allowed
    working directory — no permission prompt for every attachment.

    Honors ``WALKCODE_DOWNLOAD_DIR`` when set (per-instance isolation); else
    defaults to ``<system temp>/walkcode-attachments``.
    """
    raw = os.environ.get("WALKCODE_DOWNLOAD_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path(tempfile.gettempdir()) / "walkcode-attachments"
    with contextlib.suppress(OSError):
        base.mkdir(parents=True, exist_ok=True)
    return base


def _log_degrade(event: str, **fields: Any) -> None:
    """One-line stderr trace for silent-degradation paths.

    These paths deliberately keep the user flow alive (fall back to a new
    card, drop an ephemeral progress update), but without a trace the visible
    symptom ("card didn't update" / "progress vanished") is undebuggable.
    """
    parts = [f"walkcode degrade={event}"]
    for key, value in fields.items():
        if isinstance(value, BaseException):
            value = f"{type(value).__name__}: {value}"
        parts.append(f"{key}={value}")
    print(" ".join(str(p) for p in parts), file=sys.stderr, flush=True)


class AgentEventType:
    TURN_DELTA = "turn.delta"
    # Mid-turn assistant narration (text sharing a message with tool_use
    # blocks, or transcript text drained between TUI hooks). Rendered as a 💬
    # line on the rolling tool-progress card — never as a channel bubble, and
    # never seals the burst (ADR 0055).
    TURN_NARRATION = "turn.narration"
    TURN_COMPLETED = "turn.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    PERMISSION_REQUESTED = "permission.requested"
    ASK_USER_REQUESTED = "ask_user.requested"
    SESSION_ERROR = "session.error"
    # Background subagent ledger beat: payload carries the full list of tasks
    # still running inside the agent process. count == 0 means the ledger just
    # drained. Never rendered as channel text; drives status card + liveness.
    BACKGROUND_TASKS = "background.tasks"


class DeliveryStatus:
    SENT = "sent"
    TRANSIENT_FAILURE = "transient_failure"
    PERMANENT_FAILURE = "permanent_failure"


class BlockedReason:
    ALREADY_DECIDED = "already_decided"
    AMBIGUOUS_SESSION = "ambiguous_session"
    CAPABILITY_DISABLED = "capability_disabled"
    DUPLICATE_INBOUND = "duplicate_inbound"
    EXTERNAL_TUI_READONLY = "external_tui_readonly"
    INVALID_TOKEN = "invalid_token"
    NOT_EXTERNAL_TUI = "not_external_tui"
    NOT_FOUND = "not_found"
    SESSION_RUNNING = "session_running"
    SESSION_STOPPED = "session_stopped"
    STALE_GENERATION = "stale_generation"
    UNAUTHORIZED = "unauthorized"


class TransportUnavailable(RuntimeError):
    """Raised when an optional transport dependency is not available."""


class CapabilityUnsupported(RuntimeError):
    """Raised when a transport method is intentionally capability-gated off."""


class ChannelConfigError(ValueError):
    """Raised when channel-native runtime config is invalid."""


class UnsafeSandboxError(RuntimeError):
    """Raised when a thread would run unsandboxed on an unrestricted channel.

    Deliberately NOT a ChannelConfigError: the lark ingress loop
    re-raises that one to kill the process, and under launchd a fatal error
    thrown per inbound message is a crash loop with nothing visible in chat.
    Refusing the one thread keeps the instance alive and the refusal in the
    logs. The genuinely static half of this check — an explicit
    WALKCODE_CODEX_SANDBOX=danger-full-access with no allowlist — is a
    ChannelConfigError raised at startup instead, where fatal is correct.
    """


class TransientDeliveryError(RuntimeError):
    """Raised by a channel adapter when a delivery should be retried."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentDeliveryError(RuntimeError):
    """Raised by a channel adapter when a delivery should not be retried."""


class TakeoverError(RuntimeError):
    """Raised for invalid takeover transitions."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class TakeoverPhase:
    PROMPTED = "prompted"
    AUTHORIZED = "authorized"
    MANUAL_ONLY = "manual_only"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionRole:
    OWNER = "owner"
    COLLABORATOR = "collaborator"
    REVIEWER = "reviewer"
    ADMIN = "admin"


@dataclass(frozen=True)
class ActorRef:
    channel_kind: str
    actor_id: str
    display_name: str = ""


@dataclass(frozen=True)
class AttachmentRef:
    source_id: str
    mime: str = ""
    local_path: str = ""
    source_message_id: str = ""


@dataclass
class ChannelBinding:
    channel_kind: str
    account_id: str
    chat_id: str
    thread_id: str = ""
    root_message_id: str = ""
    last_message_id: str = ""
    health_message_id: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)

    def key(self) -> BindingKey:
        return (
            self.channel_kind,
            self.account_id,
            self.chat_id,
            self.thread_id,
            self.root_message_id,
        )


@dataclass(frozen=True)
class ChannelCapabilities:
    editable_message: bool
    private_callback_ack: bool
    attachment_download: bool


@dataclass
class InboundEvent:
    event_id: str
    channel_kind: str
    account_id: str
    chat_id: str
    thread_id: str
    message_id: str
    root_message_id: str
    sender_id: str
    sender_display: str
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    callback: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # 渠道侧消息产生时刻（秒，飞书服务端时钟）；0=未知（回调等无此概念）。
    created_at: float = 0.0

    def binding_key(self) -> BindingKey:
        return (
            self.channel_kind,
            self.account_id,
            self.chat_id,
            self.thread_id,
            self.root_message_id,
        )


@dataclass(frozen=True)
class TransportCapabilities:
    structured_input: bool
    structured_output: bool
    permission_callback: bool
    ask_user_question: bool
    set_model: bool
    resume_after_complete: bool
    external_tui_takeover: bool


@dataclass
class LaunchSpec:
    cwd: str
    session_id: str


@dataclass
class ResumeSpec:
    cwd: str
    session_id: str
    resume_ref: dict[str, Any]


@dataclass
class TransportHandle:
    handle_id: str
    transport_kind: str
    ref: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnInput:
    text: str
    attachments: list[AttachmentRef] = field(default_factory=list)
    # 消息在来源渠道的产生时刻（飞书 create_time，秒）；0=未知。
    created_at: float = 0.0


@dataclass
class AgentEvent:
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int = 0


@dataclass
class WriterOwner:
    kind: Literal["orchestrator", "external_tui", "none"]
    transport_kind: str = ""
    actor_id: str = ""
    external_ref: dict[str, Any] = field(default_factory=dict)
    acquired_at: float = 0.0


@dataclass
class BlockedInput:
    blocked_input_id: str
    session_id: str
    actor: ActorRef
    text: str
    attachments: list[AttachmentRef]
    idempotency_key: str
    state: Literal["blocked", "cancelled", "submitted", "not_delivered"]
    created_at: float
    submit_after_takeover: bool = True


@dataclass
class TakeoverTransaction:
    takeover_id: str
    session_id: str
    blocked_input_id: str
    requested_by: ActorRef
    requested_generation: int
    phase: str
    created_at: float
    approved_by: ActorRef | None = None
    resume_ref: dict[str, Any] | None = None
    transport_kind: str = ""
    transport_ref: dict[str, Any] = field(default_factory=dict)
    authorized_at: float | None = None
    completed_at: float | None = None
    reason: str = ""


@dataclass
class Session:
    schema_version: int
    session_id: str
    transport_kind: str
    transport_ref: dict[str, Any]
    cwd: str
    channel_binding: ChannelBinding | None = None
    lifecycle_state: str = "NEW"
    writer_owner: WriterOwner | None = None
    generation: int = 0
    last_event_seq: int = 0
    blocked_inputs: dict[str, BlockedInput] = field(default_factory=dict)
    cached_title: str = ""
    title_source: str = ""
    # Throttle watermark for same-rank title refreshes. Lives on the session,
    # not the transport: one codex thread hops between TUI hooks and the
    # app-server event stream (takeover/handback), and a transport-scoped
    # watermark would reset on every hop and let the title churn.
    title_refreshed_at: float = 0.0
    status: Literal["running", "stopped"] = "running"
    stop_reason: str = ""
    running_since: float = 0.0
    last_progress_at: float = 0.0
    last_progress_event: str = ""
    # 会话最近一次"被人说话"的时刻（ADR 0057）：频道消息记其渠道产生时刻
    # （同源同钟），终端输入记 hook 捕获时刻。滞留消息时效守卫的比较基准。
    last_user_input_at: float = 0.0
    archived_at: float = 0.0
    archived_by: str = ""
    archive_reason: str = ""
    model: str = ""
    last_usage: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # Background subagents still running inside the agent process
    # ([{task_id, description}, ...]); the session can be IDLE between turns
    # while these keep working and re-open turns when they finish.
    background_tasks: list[dict[str, Any]] = field(default_factory=list)


def _session_is_external_tui_takeover_candidate(session: Session) -> bool:
    if session.transport_kind == "external_tui":
        return True
    if session.writer_owner is not None and session.writer_owner.kind == "external_tui":
        return True
    refs: list[dict[str, Any]] = []
    if isinstance(session.transport_ref, dict):
        refs.append(session.transport_ref)
    if session.writer_owner is not None and isinstance(session.writer_owner.external_ref, dict):
        refs.append(session.writer_owner.external_ref)
    return any(str(ref.get("source", "")) == "native_tui_hook" for ref in refs)


_STRUCTURED_TRANSPORT_KINDS = frozenset({"claude_headless", "codex_app_server"})
# Stops that a later channel message is allowed to undo (ADR 0054). All three
# are INVOLUNTARY from the conversation's point of view: the runtime went away,
# a revival attempt did not land, or /reload deliberately cycled the backend
# under a session the user wants to keep talking to.
_CHANNEL_REVIVAL_STOP_REASONS = frozenset(
    {"runtime_restart", "revive_failed", "backend_reload", "idle_expired"}
)


# Where each agent's own session id may sit in a transport / resume ref, most
# specific first. One table for every reader: they used to disagree (one copy
# accepted `session_id`, another did not), so a ref could be "resumable" to one
# path and "not durable" to the next.
_AGENT_SESSION_ID_KEYS = {
    "claude_headless": ("agent_session_id", "claude_session_id", "resume", "session_id"),
    "codex_app_server": ("thread_id", "codex_thread_id", "conversation_id", "session_id"),
}
# WalkCode's own ledger ids (Orchestrator `sess-<uuid4 hex>`, TUI-observed
# `tui-<agent>-<12 hex>`). Claude refs carry `session_id: sess-...` next to the
# agent id; no agent has ever heard of it.
_WALKCODE_SESSION_ID_RE = re.compile(r"sess-[0-9a-f]{32}|tui-(?:claude|codex)-[0-9a-f]{12}")


def agent_session_id(transport_kind: str, ref: dict[str, Any]) -> str:
    """The agent's own session id in ``ref`` ("" when absent).

    For transports without an agent id (fakes, external refs) this is the
    generic ``session_id`` / ``handle_id``.
    """
    keys = _AGENT_SESSION_ID_KEYS.get(transport_kind)
    if keys is None:
        return str(ref.get("session_id") or ref.get("handle_id") or "")
    for key in keys:
        value = ref.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if key == "session_id" and _WALKCODE_SESSION_ID_RE.fullmatch(value):
            continue
        return value
    return ""


def _durable_resume_ref(session: Session) -> dict[str, Any]:
    """The transport ref if it carries enough identity to resume, else {}.

    The id is copied to the key the transport's resume reads.
    """
    ref = dict(session.transport_ref)
    if session.transport_kind not in _AGENT_SESSION_ID_KEYS:
        return ref
    identity = agent_session_id(session.transport_kind, ref)
    if not identity:
        return {}
    ref["agent_session_id" if session.transport_kind == "claude_headless" else "thread_id"] = identity
    return ref


def _agent_session_identity(session: Session) -> str:
    """The AGENT's own session id — what ``codex resume`` / ``claude --resume`` take.

    ``session.session_id`` is WalkCode's ledger key (``sess-<uuid4 hex>``); no
    agent has ever heard of it. /status used to print only that, so anyone who
    copied it into ``codex resume`` got "no such session" — the real id lives
    in the transport ref (``thread_id`` for codex, ``agent_session_id`` for
    claude), one level down inside ``resume_ref`` for TUI-observed sessions.
    """
    ref = session.transport_ref if isinstance(session.transport_ref, dict) else {}
    kind = str(session.transport_kind or "")
    nested = ref.get("resume_ref")
    if isinstance(nested, dict) and nested:
        # TUI-observed sessions (transport_kind "external_tui") keep the
        # agent-native ref nested, tagged with its own discriminator.
        kind = str(nested.get("transport_kind", "") or kind)
        ref = nested
    return agent_session_id(kind, ref) if kind in _AGENT_SESSION_ID_KEYS else ""


def _session_is_channel_revival_candidate(session: Session) -> bool:
    """ADR 0054 preconditions, shared by binding resolution and submit.

    Only INVOLUNTARY stops revive — an explicit close keeps its "blocks
    future submits" contract, an archived session stays archived, and
    external-TUI takeover candidates keep the consent-based takeover prompt.
    """
    if session.status != "stopped":
        return False
    if session.stop_reason not in _CHANNEL_REVIVAL_STOP_REASONS:
        return False
    if session.archived_at:
        return False
    if session.transport_kind not in _STRUCTURED_TRANSPORT_KINDS:
        return False
    if _session_is_external_tui_takeover_candidate(session):
        return False
    return bool(_durable_resume_ref(session))


def _session_has_durable_resume_ref(session: Any) -> bool:
    ref = getattr(session, "transport_ref", {}) or {}
    transport_kind = str(getattr(session, "transport_kind", ""))
    if transport_kind == "external_tui":
        nested = ref.get("resume_ref") if isinstance(ref, dict) else None
        if not isinstance(nested, dict):
            return False
        nested_kind = str(nested.get("transport_kind", "") or nested.get("kind", ""))
        return _resume_ref_is_durable(nested_kind, nested)
    return _resume_ref_is_durable(transport_kind, ref)


def _resume_ref_is_durable(transport_kind: str, ref: dict[str, Any]) -> bool:
    if transport_kind in _STRUCTURED_TRANSPORT_KINDS:
        return bool(agent_session_id(transport_kind, ref))
    return bool(ref)


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    channel_kind: str
    account_id: str
    chat_id: str
    thread_id: str
    root_message_id: str
    status: str
    lifecycle_state: str
    transport_kind: str
    cwd: str
    title: str
    created_at: float
    archived_at: float = 0.0
    archived_by: str = ""


@dataclass
class SubmitResult:
    accepted: bool
    reason: str = ""
    blocked_input_id: str = ""


@dataclass(frozen=True)
class BindingResolution:
    session_id: str = ""
    reason: str = ""


@dataclass(frozen=True)
class SessionHealth:
    session_id: str
    status: str
    reason: str
    stale: bool
    elapsed: float
    last_progress_at: float
    last_progress_event: str
    last_event_seq: int
    view_model: dict[str, Any]


@dataclass
class ControlResult:
    accepted: bool
    reason: str = ""
    state: str = ""


@dataclass(frozen=True)
class AuthorizationResult:
    allowed: bool
    reason: str = ""
    role: str = ""


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _humanize_seconds(seconds: float) -> str:
    """Render a timeout as a human span ("1 小时"), not a raw second count.

    "3600 秒" reads as a machine constant and forces the reader to divide;
    every one of these strings lands in a channel message a human skims once.
    """
    total = int(max(0.0, seconds))
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes} 分钟" if not rest else f"{minutes} 分 {rest} 秒"
    hours, rest = divmod(total, 3600)
    minutes = rest // 60
    return f"{hours} 小时" if not minutes else f"{hours} 小时 {minutes} 分钟"


# Characters a tool card's summary may occupy. Shared so callers that
# pre-render a summary (e.g. _codex_file_change_summary) budget against the
# same number that finally truncates it.
_TOOL_SUMMARY_LIMIT = 160


def _compact_tool_summary(value: Any, *, limit: int = _TOOL_SUMMARY_LIMIT) -> str:
    if isinstance(value, dict):
        raw = ", ".join(f"{key}={value[key]!r}" for key in sorted(value)[:4])
    elif isinstance(value, list):
        raw = f"{len(value)} item(s)"
    else:
        raw = str(value or "")
    collapsed = " ".join(raw.replace("\n", " ").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."
