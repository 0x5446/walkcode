"""Clean-slate channel-native core contracts for WalkCode V3.

This module is intentionally independent from the removed pre-V3 runtime.
It starts as a compact contract-tested core; later slices can split it into
smaller modules once the boundaries settle.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import time
import uuid
import inspect
import html
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


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


class AgentEventType:
    TURN_DELTA = "turn.delta"
    TURN_COMPLETED = "turn.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    PERMISSION_REQUESTED = "permission.requested"
    ASK_USER_REQUESTED = "ask_user.requested"
    SESSION_ERROR = "session.error"


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
    LEASE_EXPIRED = "lease_expired"
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
    subscribed: bool = False
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
    thread_context: bool
    editable_message: bool
    interactive_message: bool
    interactive_update: bool
    private_callback_ack: bool
    toast_or_ephemeral_notice: bool
    force_reply: bool
    attachment_download: bool
    forum_or_topic: bool
    max_text_chars: int
    max_callback_payload_bytes: int
    edit_rate_limit_hint: str = ""


@dataclass(frozen=True)
class ChannelEndpointConfig:
    kind: str
    credentials: dict[str, str]
    options: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    legacy: bool = False


@dataclass(frozen=True)
class ChannelNativeConfig:
    channel: ChannelEndpointConfig
    agent: str
    agent_options: dict[str, dict[str, Any]]
    state_path: str
    cwd: str
    profile: str = ""
    workspace_roots: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChannelNativeConfig":
        source = os.environ if env is None else env
        _reject_removed_runtime_env(source)
        channel_kind = _configured_channel_kind(source)
        if not channel_kind:
            raise ChannelConfigError(
                "no channel configured for channel-native runtime; "
                "set WALKCODE_ENV_FILE to bind this command to a runtime instance"
            )

        if channel_kind == "telegram":
            channel = _telegram_config_from_env(source, priority=0)
        elif channel_kind == "lark":
            channel = _lark_config_from_env(source, priority=0)
        else:
            raise ChannelConfigError(f"unknown channel configured: {channel_kind}")

        agent = _configured_agent(source)
        supported_agent_names = ("claude", "codex")
        if agent not in supported_agent_names:
            raise ChannelConfigError(f"unknown agent configured: {agent}")

        profile = _configured_profile(source)
        return cls(
            channel=channel,
            agent=agent,
            agent_options=_configured_agent_options(source),
            state_path=str(
                Path(_configured_state_path(source, channel_kind, agent, profile)).expanduser()
            ),
            cwd=str(Path(source.get("WALKCODE_CWD", "~/.walkcode/workspace")).expanduser()),
            profile=profile,
            workspace_roots=tuple(
                str(Path(item).expanduser())
                for item in str(source.get("WALKCODE_WORKSPACE_ROOTS", "") or "").split(":")
                if item.strip()
            ),
        )

    @property
    def channel_kind(self) -> str:
        return self.channel.kind

    @property
    def agent_transport_kind(self) -> str:
        return _agent_to_transport_kind(self.agent)


@dataclass(frozen=True)
class LegacyEnvConversionReport:
    suggested_env: dict[str, str]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class E2EGateSpec:
    name: str
    flag: str
    required_env: tuple[str, ...]


@dataclass(frozen=True)
class E2EGateResult:
    name: str
    enabled: bool
    missing: tuple[str, ...] = ()
    reason: str = ""


class ChannelNativeE2EGates:
    _SPECS = {
        "telegram": E2EGateSpec(
            name="telegram",
            flag="WALKCODE_E2E_TELEGRAM",
            required_env=("TELEGRAM_BOT_TOKEN", "WALKCODE_E2E_TELEGRAM_CHAT_ID"),
        ),
        "lark": E2EGateSpec(
            name="lark",
            flag="WALKCODE_E2E_LARK",
            required_env=("LARK_APP_ID", "LARK_APP_SECRET", "WALKCODE_E2E_LARK_CHAT_ID"),
        ),
        "claude_headless": E2EGateSpec(
            name="claude_headless",
            flag="WALKCODE_E2E_CLAUDE_HEADLESS",
            required_env=("WALKCODE_E2E_CWD",),
        ),
        "codex_app_server": E2EGateSpec(
            name="codex_app_server",
            flag="WALKCODE_E2E_CODEX_APP_SERVER",
            required_env=("WALKCODE_E2E_CWD",),
        ),
    }

    def __init__(self, env: Any):
        self._env = env

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ChannelNativeE2EGates":
        return cls(os.environ if env is None else env)

    def evaluate(self, name: str) -> E2EGateResult:
        spec = self._SPECS.get(name)
        if spec is None:
            raise ValueError(f"unknown E2E gate: {name}")
        if not _env_bool(self._env.get(spec.flag), default=False):
            return E2EGateResult(
                name=name,
                enabled=False,
                reason=f"set {spec.flag}=1 to enable {name} E2E",
            )
        missing = tuple(key for key in spec.required_env if not self._env.get(key))
        if missing:
            return E2EGateResult(
                name=name,
                enabled=False,
                missing=missing,
                reason=f"missing required env for {name} E2E: {', '.join(missing)}",
            )
        return E2EGateResult(name=name, enabled=True)

    def all(self) -> dict[str, E2EGateResult]:
        return {name: self.evaluate(name) for name in self._SPECS}


class LegacyFeishuEnvConverter:
    _MAPPING = {
        "FEISHU_APP_ID": "LARK_APP_ID",
        "FEISHU_APP_SECRET": "LARK_APP_SECRET",
        "FEISHU_RECEIVE_ID": "LARK_RECEIVE_ID",
        "FEISHU_RECEIVE_ID_TYPE": "LARK_RECEIVE_ID_TYPE",
        "FEISHU_OPENAPI_DOMAIN": "LARK_OPENAPI_DOMAIN",
    }

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> LegacyEnvConversionReport:
        source = os.environ if env is None else env
        suggested = {
            new_key: source[old_key]
            for old_key, new_key in cls._MAPPING.items()
            if source.get(old_key)
        }
        warnings = []
        if suggested:
            warnings.append(
                "FEISHU_* variables are legacy-only; channel-native runtime reads LARK_* instead."
            )
        else:
            warnings.append("no FEISHU_* variables found to convert")
        return LegacyEnvConversionReport(suggested_env=suggested, warnings=warnings)


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _env_bool(raw: str | None, *, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _configured_channel_kind(source: Any) -> str:
    explicit = str(source.get("WALKCODE_CHANNEL", "") or "").strip()
    if explicit:
        if "," in explicit:
            raise ChannelConfigError("WALKCODE_CHANNEL accepts exactly one channel: telegram or lark")
        return explicit
    return ""


def _configured_agent(source: Any) -> str:
    agent = str(source.get("WALKCODE_AGENT") or "").strip()
    if not agent:
        raise ChannelConfigError("missing WALKCODE_AGENT; set WALKCODE_AGENT=claude or WALKCODE_AGENT=codex")
    return _normalize_agent_name(agent)


def _configured_agent_options(source: Any) -> dict[str, dict[str, Any]]:
    claude: dict[str, Any] = {}
    settings = str(source.get("WALKCODE_CLAUDE_SETTINGS") or "").strip()
    if settings:
        claude["settings"] = str(Path(settings).expanduser())
    cli_path = str(source.get("WALKCODE_CLAUDE_CLI_PATH") or "").strip()
    if cli_path:
        claude["cli_path"] = str(Path(cli_path).expanduser())
    claude_config_dir = str(source.get("WALKCODE_CLAUDE_CONFIG_DIR") or "").strip()
    if claude_config_dir:
        claude["config_dir"] = str(Path(claude_config_dir).expanduser())
    claude_permission_mode = str(source.get("WALKCODE_CLAUDE_PERMISSION_MODE") or "").strip()
    if claude_permission_mode:
        allowed_modes = {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
        if claude_permission_mode not in allowed_modes:
            raise ChannelConfigError(
                f"invalid WALKCODE_CLAUDE_PERMISSION_MODE: {claude_permission_mode}; "
                f"use one of {', '.join(sorted(allowed_modes))}"
            )
        claude["permission_mode"] = claude_permission_mode
    codex: dict[str, Any] = {}
    codex_home = str(source.get("WALKCODE_CODEX_HOME") or "").strip()
    if codex_home:
        codex["codex_home"] = str(Path(codex_home).expanduser())
    codex_config = str(source.get("WALKCODE_CODEX_CONFIG") or "").strip()
    if codex_config:
        codex["config"] = str(Path(codex_config).expanduser())
    codex_models_cache = str(source.get("WALKCODE_CODEX_MODELS_CACHE") or "").strip()
    if codex_models_cache:
        codex["models_cache"] = str(Path(codex_models_cache).expanduser())
    codex_app_server_mode = str(source.get("WALKCODE_CODEX_APP_SERVER_MODE") or "").strip()
    if codex_app_server_mode:
        codex["app_server_mode"] = codex_app_server_mode
    codex_app_server_socket = str(source.get("WALKCODE_CODEX_APP_SERVER_SOCKET") or "").strip()
    if codex_app_server_socket:
        codex["app_server_socket"] = str(Path(codex_app_server_socket).expanduser())
    return {
        "claude": claude,
        "codex": codex,
    }


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _configured_profile(source: Any) -> str:
    raw = str(source.get("WALKCODE_PROFILE") or "").strip()
    if not raw:
        return ""
    if not _PROFILE_RE.match(raw):
        raise ChannelConfigError(
            f"invalid WALKCODE_PROFILE: {raw!r}; use lowercase letters, digits, and dashes"
        )
    return raw


def _configured_state_path(source: Any, channel_kind: str, agent: str, profile: str = "") -> str:
    explicit = str(source.get("WALKCODE_STATE_PATH") or "").strip()
    if explicit:
        return explicit
    if profile:
        return f"~/.walkcode/{profile}-{agent}-state.json"
    return f"~/.walkcode/{channel_kind}-{agent}-state.json"


def _reject_removed_runtime_env(source: Any) -> None:
    removed = {
        "WALKCODE_CHANNELS": "use WALKCODE_CHANNEL=telegram or WALKCODE_CHANNEL=lark",
        "WALKCODE_PRIMARY_CHANNEL": "remove it; one runtime instance has exactly one WALKCODE_CHANNEL",
        "WALKCODE_TRANSPORTS": "remove it; AgentTransport wiring is internal",
        "WALKCODE_DEFAULT_TRANSPORT": "use WALKCODE_AGENT=claude|codex to bind this bot to one agent",
        "WALKCODE_DEFAULT_AGENT": "use WALKCODE_AGENT=claude|codex to bind this bot to one agent",
    }
    for key, guidance in removed.items():
        if str(source.get(key, "") or "").strip():
            raise ChannelConfigError(f"{key} is not supported by channel-native V3; {guidance}")


def _normalize_agent_name(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"claude", "claude-code", "claude-headless"}:
        return "claude"
    if normalized in {"codex", "codex-app-server"}:
        return "codex"
    return value.strip()


def _agent_to_transport_kind(agent: str) -> str:
    normalized = _normalize_agent_name(agent)
    if normalized == "claude":
        return "claude_headless"
    if normalized == "codex":
        return "codex_app_server"
    raise ChannelConfigError(f"unknown agent configured: {agent}")


def _telegram_config_from_env(source: Any, *, priority: int) -> ChannelEndpointConfig:
    token = source.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise ChannelConfigError("missing TELEGRAM_BOT_TOKEN for telegram channel")
    webhook_url = source.get("TELEGRAM_WEBHOOK_URL", "")
    allowed_chat_ids = _split_csv(source.get("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    e2e_chat_id = str(source.get("WALKCODE_E2E_TELEGRAM_CHAT_ID", "") or "").strip()
    if not allowed_chat_ids and e2e_chat_id:
        allowed_chat_ids = [e2e_chat_id]
    tui_chat_id = str(source.get("WALKCODE_TELEGRAM_TUI_CHAT_ID", "") or "").strip()
    tui_thread_id = str(source.get("WALKCODE_TELEGRAM_TUI_THREAD_ID", "") or "").strip()
    allowed_actor_ids = _split_csv(
        source.get("TELEGRAM_ALLOWED_ACTOR_IDS", "")
        or source.get("TELEGRAM_ALLOWED_USER_IDS", "")
    )
    return ChannelEndpointConfig(
        kind="telegram",
        credentials={"bot_token": token},
        options={
            "allowed_chat_ids": tuple(allowed_chat_ids),
            "allowed_actor_ids": tuple(allowed_actor_ids),
            "rich_messages": _env_bool(
                source.get("WALKCODE_TELEGRAM_RICH_MESSAGES")
                or source.get("TELEGRAM_RICH_MESSAGES"),
                default=False,
            ),
            "tui_chat_id": tui_chat_id,
            "tui_thread_id": tui_thread_id,
            "webhook_url": webhook_url,
            "polling": _env_bool(source.get("TELEGRAM_POLLING"), default=not bool(webhook_url)),
        },
        priority=priority,
    )


def _lark_config_from_env(source: Any, *, priority: int) -> ChannelEndpointConfig:
    app_id = source.get("LARK_APP_ID", "")
    app_secret = source.get("LARK_APP_SECRET", "")
    missing = [key for key, value in (("LARK_APP_ID", app_id), ("LARK_APP_SECRET", app_secret)) if not value]
    if missing:
        raise ChannelConfigError(f"missing {', '.join(missing)} for lark channel")
    allowed_chat_ids = tuple(_split_csv(source.get("LARK_ALLOWED_CHAT_IDS", "")))
    if not allowed_chat_ids:
        e2e_chat_id = str(source.get("WALKCODE_E2E_LARK_CHAT_ID", "") or "").strip()
        if e2e_chat_id:
            allowed_chat_ids = (e2e_chat_id,)
    return ChannelEndpointConfig(
        kind="lark",
        credentials={"app_id": app_id, "app_secret": app_secret},
        options={
            "receive_id": source.get("LARK_RECEIVE_ID", ""),
            "receive_id_type": source.get("LARK_RECEIVE_ID_TYPE", "open_id"),
            "openapi_domain": source.get("LARK_OPENAPI_DOMAIN", "https://open.feishu.cn").rstrip("/"),
            "allowed_chat_ids": allowed_chat_ids,
            "allowed_open_ids": tuple(_split_csv(source.get("LARK_ALLOWED_OPEN_IDS", ""))),
            "tui_chat_id": str(source.get("WALKCODE_LARK_TUI_CHAT_ID", "") or "").strip(),
        },
        priority=priority,
    )


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
    interrupt: bool
    set_model: bool
    set_permission_mode: bool
    checkpoint_rewind: bool
    resume_after_complete: bool
    resume_active_turn: bool
    multi_client_observe: bool
    multi_client_write: bool
    external_tui_takeover: bool
    requires_single_writer: bool = True


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
class WriterLease:
    lease_id: str
    session_id: str
    generation: int
    owner_kind: str
    holder_ref: dict[str, Any]
    heartbeat_at: float
    expires_at: float

    def expired(self, now: float) -> bool:
        return now >= self.expires_at


@dataclass
class BlockedInput:
    blocked_input_id: str
    session_id: str
    actor: ActorRef
    text: str
    attachments: list[AttachmentRef]
    idempotency_key: str
    state: Literal["blocked", "cancelled", "submitted", "not_delivered", "expired"]
    created_at: float
    expires_at: float
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
    writer_lease: WriterLease | None = None
    generation: int = 0
    last_event_seq: int = 0
    blocked_inputs: dict[str, BlockedInput] = field(default_factory=dict)
    cached_title: str = ""
    title_source: str = ""
    status: Literal["running", "stopped"] = "running"
    stop_reason: str = ""
    interrupt_reason: str = ""
    running_since: float = 0.0
    last_progress_at: float = 0.0
    last_progress_event: str = ""
    archived_at: float = 0.0
    archived_by: str = ""
    archive_reason: str = ""
    created_at: float = field(default_factory=time.time)


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


@dataclass
class PendingBinding:
    pending_key: str
    binding: ChannelBinding
    cwd: str
    created_at: float


class AuthorizationStore:
    def __init__(self, *, now: Callable[[], float] = time.time):
        self._now = now
        self._roles: dict[str, dict[tuple[str, str], str]] = {}
        self._audit: list[dict[str, Any]] = []

    def grant(self, session_id: str, actor: ActorRef, role: str) -> None:
        if role not in {
            SessionRole.OWNER,
            SessionRole.COLLABORATOR,
            SessionRole.REVIEWER,
            SessionRole.ADMIN,
        }:
            raise ValueError(f"unknown session role: {role}")
        self._roles.setdefault(session_id, {})[(actor.channel_kind, actor.actor_id)] = role
        self._audit.append(
            {
                "type": "role_granted",
                "session_id": session_id,
                "actor": actor.actor_id,
                "channel_kind": actor.channel_kind,
                "role": role,
                "ts": self._now(),
            }
        )

    def role_for(self, session_id: str, actor: ActorRef) -> str:
        return self._roles.get(session_id, {}).get((actor.channel_kind, actor.actor_id), "")

    def can_submit(self, session_id: str, actor: ActorRef) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.COLLABORATOR, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_decide_permission(
        self,
        session_id: str,
        actor: ActorRef,
        *,
        high_risk: bool = False,
    ) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if high_risk:
            allowed = role in {SessionRole.OWNER, SessionRole.ADMIN}
        else:
            allowed = role in {SessionRole.OWNER, SessionRole.COLLABORATOR, SessionRole.ADMIN}
        if allowed:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_takeover(self, session_id: str, actor: ActorRef) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def can_control_session(
        self,
        session_id: str,
        actor: ActorRef,
        *,
        action: str,
    ) -> AuthorizationResult:
        role = self.role_for(session_id, actor)
        if role in {SessionRole.OWNER, SessionRole.ADMIN}:
            return AuthorizationResult(True, role=role)
        return AuthorizationResult(False, BlockedReason.UNAUTHORIZED, role=role)

    def audit_events(self) -> list[dict[str, Any]]:
        return list(self._audit)

    def to_dict(self) -> dict[str, Any]:
        grants = []
        for session_id, roles in self._roles.items():
            for (channel_kind, actor_id), role in roles.items():
                grants.append(
                    {
                        "session_id": session_id,
                        "channel_kind": channel_kind,
                        "actor_id": actor_id,
                        "role": role,
                    }
                )
        return {"grants": grants, "audit": list(self._audit)}

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "AuthorizationStore":
        store = cls(now=now)
        for grant in data.get("grants", []):
            if not isinstance(grant, dict):
                continue
            session_id = str(grant.get("session_id", ""))
            channel_kind = str(grant.get("channel_kind", ""))
            actor_id = str(grant.get("actor_id", ""))
            role = str(grant.get("role", ""))
            if session_id and channel_kind and actor_id and role:
                store._roles.setdefault(session_id, {})[(channel_kind, actor_id)] = role
        store._audit = [dict(item) for item in data.get("audit", []) if isinstance(item, dict)]
        return store


def _binding_to_dict(binding: ChannelBinding | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {
        "channel_kind": binding.channel_kind,
        "account_id": binding.account_id,
        "chat_id": binding.chat_id,
        "thread_id": binding.thread_id,
        "root_message_id": binding.root_message_id,
        "last_message_id": binding.last_message_id,
        "health_message_id": binding.health_message_id,
        "subscribed": binding.subscribed,
        "capabilities": dict(binding.capabilities),
    }


def _binding_from_dict(data: dict[str, Any] | None) -> ChannelBinding | None:
    if not data:
        return None
    return ChannelBinding(
        channel_kind=str(data.get("channel_kind", "")),
        account_id=str(data.get("account_id", "")),
        chat_id=str(data.get("chat_id", "")),
        thread_id=str(data.get("thread_id", "")),
        root_message_id=str(data.get("root_message_id", "")),
        last_message_id=str(data.get("last_message_id", "")),
        health_message_id=str(data.get("health_message_id", "")),
        subscribed=bool(data.get("subscribed", False)),
        capabilities=dict(data.get("capabilities", {})),
    )


def _actor_to_dict(actor: ActorRef | None) -> dict[str, Any] | None:
    if actor is None:
        return None
    return {
        "channel_kind": actor.channel_kind,
        "actor_id": actor.actor_id,
        "display_name": actor.display_name,
    }


def _actor_from_dict(data: dict[str, Any] | None) -> ActorRef | None:
    if not data:
        return None
    return ActorRef(
        channel_kind=str(data.get("channel_kind", "")),
        actor_id=str(data.get("actor_id", "")),
        display_name=str(data.get("display_name", "")),
    )


def _attachment_to_dict(attachment: AttachmentRef) -> dict[str, Any]:
    return {
        "source_id": attachment.source_id,
        "mime": attachment.mime,
        "local_path": attachment.local_path,
        "source_message_id": attachment.source_message_id,
    }


def _attachment_from_dict(data: dict[str, Any]) -> AttachmentRef:
    return AttachmentRef(
        source_id=str(data.get("source_id", "")),
        mime=str(data.get("mime", "")),
        local_path=str(data.get("local_path", "")),
        source_message_id=str(data.get("source_message_id", "")),
    )


def _writer_owner_to_dict(owner: WriterOwner | None) -> dict[str, Any] | None:
    if owner is None:
        return None
    return {
        "kind": owner.kind,
        "transport_kind": owner.transport_kind,
        "actor_id": owner.actor_id,
        "external_ref": dict(owner.external_ref),
        "acquired_at": owner.acquired_at,
    }


def _writer_owner_from_dict(data: dict[str, Any] | None) -> WriterOwner | None:
    if not data:
        return None
    return WriterOwner(
        kind=data.get("kind", "none"),
        transport_kind=str(data.get("transport_kind", "")),
        actor_id=str(data.get("actor_id", "")),
        external_ref=dict(data.get("external_ref", {})),
        acquired_at=float(data.get("acquired_at", 0.0)),
    )


def _writer_lease_to_dict(lease: WriterLease | None) -> dict[str, Any] | None:
    if lease is None:
        return None
    return {
        "lease_id": lease.lease_id,
        "session_id": lease.session_id,
        "generation": lease.generation,
        "owner_kind": lease.owner_kind,
        "holder_ref": dict(lease.holder_ref),
        "heartbeat_at": lease.heartbeat_at,
        "expires_at": lease.expires_at,
    }


def _writer_lease_from_dict(data: dict[str, Any] | None) -> WriterLease | None:
    if not data:
        return None
    return WriterLease(
        lease_id=str(data.get("lease_id", "")),
        session_id=str(data.get("session_id", "")),
        generation=int(data.get("generation", 0)),
        owner_kind=str(data.get("owner_kind", "")),
        holder_ref=dict(data.get("holder_ref", {})),
        heartbeat_at=float(data.get("heartbeat_at", 0.0)),
        expires_at=float(data.get("expires_at", 0.0)),
    )


def _blocked_input_to_dict(blocked: BlockedInput) -> dict[str, Any]:
    return {
        "blocked_input_id": blocked.blocked_input_id,
        "session_id": blocked.session_id,
        "actor": _actor_to_dict(blocked.actor),
        "text": blocked.text,
        "attachments": [_attachment_to_dict(item) for item in blocked.attachments],
        "idempotency_key": blocked.idempotency_key,
        "state": blocked.state,
        "created_at": blocked.created_at,
        "expires_at": blocked.expires_at,
        "submit_after_takeover": blocked.submit_after_takeover,
    }


def _blocked_input_from_dict(data: dict[str, Any]) -> BlockedInput:
    actor = _actor_from_dict(data.get("actor")) or ActorRef("", "")
    return BlockedInput(
        blocked_input_id=str(data.get("blocked_input_id", "")),
        session_id=str(data.get("session_id", "")),
        actor=actor,
        text=str(data.get("text", "")),
        attachments=[
            _attachment_from_dict(item) for item in data.get("attachments", []) if isinstance(item, dict)
        ],
        idempotency_key=str(data.get("idempotency_key", "")),
        state=data.get("state", "blocked"),
        created_at=float(data.get("created_at", 0.0)),
        expires_at=float(data.get("expires_at", 0.0)),
        submit_after_takeover=bool(data.get("submit_after_takeover", True)),
    )


def _session_to_dict(session: Session) -> dict[str, Any]:
    return {
        "schema_version": session.schema_version,
        "session_id": session.session_id,
        "transport_kind": session.transport_kind,
        "transport_ref": dict(session.transport_ref),
        "cwd": session.cwd,
        "channel_binding": _binding_to_dict(session.channel_binding),
        "lifecycle_state": session.lifecycle_state,
        "writer_owner": _writer_owner_to_dict(session.writer_owner),
        "writer_lease": _writer_lease_to_dict(session.writer_lease),
        "generation": session.generation,
        "last_event_seq": session.last_event_seq,
        "blocked_inputs": {
            key: _blocked_input_to_dict(value) for key, value in session.blocked_inputs.items()
        },
        "cached_title": session.cached_title,
        "title_source": session.title_source,
        "status": session.status,
        "stop_reason": session.stop_reason,
        "interrupt_reason": session.interrupt_reason,
        "running_since": session.running_since,
        "last_progress_at": session.last_progress_at,
        "last_progress_event": session.last_progress_event,
        "archived_at": session.archived_at,
        "archived_by": session.archived_by,
        "archive_reason": session.archive_reason,
        "created_at": session.created_at,
    }


def _session_from_dict(data: dict[str, Any]) -> Session:
    return Session(
        schema_version=int(data.get("schema_version", 1)),
        session_id=str(data.get("session_id", "")),
        transport_kind=str(data.get("transport_kind", "")),
        transport_ref=dict(data.get("transport_ref", {})),
        cwd=str(data.get("cwd", "")),
        channel_binding=_binding_from_dict(data.get("channel_binding")),
        lifecycle_state=str(data.get("lifecycle_state", "NEW")),
        writer_owner=_writer_owner_from_dict(data.get("writer_owner")),
        writer_lease=_writer_lease_from_dict(data.get("writer_lease")),
        generation=int(data.get("generation", 0)),
        last_event_seq=int(data.get("last_event_seq", 0)),
        blocked_inputs={
            str(key): _blocked_input_from_dict(value)
            for key, value in data.get("blocked_inputs", {}).items()
            if isinstance(value, dict)
        },
        cached_title=str(data.get("cached_title", "")),
        title_source=str(data.get("title_source", "")),
        status=data.get("status", "running"),
        stop_reason=str(data.get("stop_reason", "")),
        interrupt_reason=str(data.get("interrupt_reason", "")),
        running_since=float(data.get("running_since", 0.0)),
        last_progress_at=float(data.get("last_progress_at", 0.0)),
        last_progress_event=str(data.get("last_progress_event", "")),
        archived_at=float(data.get("archived_at", 0.0)),
        archived_by=str(data.get("archived_by", "")),
        archive_reason=str(data.get("archive_reason", "")),
        created_at=float(data.get("created_at", 0.0)),
    )


def _delivery_to_dict(item: DeliveryItem) -> dict[str, Any]:
    return {
        "delivery_id": item.delivery_id,
        "seq": item.seq,
        "channel_binding_key": list(item.channel_binding_key),
        "view_model": dict(item.view_model),
        "idempotency_key": item.idempotency_key,
        "attempt_count": item.attempt_count,
        "created_at": item.created_at,
        "next_attempt_at": item.next_attempt_at,
        "last_error": item.last_error,
        "finished_at": item.finished_at,
        "claim_owner": item.claim_owner,
        "claim_until": item.claim_until,
    }


def _delivery_from_dict(data: dict[str, Any]) -> DeliveryItem:
    return DeliveryItem(
        delivery_id=str(data.get("delivery_id", "")),
        seq=int(data.get("seq", 0)),
        channel_binding_key=tuple(data.get("channel_binding_key", ("", "", "", "", ""))),  # type: ignore[arg-type]
        view_model=dict(data.get("view_model", {})),
        idempotency_key=str(data.get("idempotency_key", "")),
        attempt_count=int(data.get("attempt_count", 0)),
        created_at=float(data.get("created_at", 0.0)),
        next_attempt_at=float(data.get("next_attempt_at", 0.0)),
        last_error=str(data.get("last_error", "")),
        finished_at=float(data.get("finished_at", 0.0)),
        claim_owner=str(data.get("claim_owner", "")),
        claim_until=float(data.get("claim_until", 0.0)),
    )


class SessionRegistry:
    def __init__(self, *, now: Callable[[], float] = time.time, lease_ttl: float = 30.0):
        self._now = now
        self._lease_ttl = lease_ttl
        self._sessions: dict[str, Session] = {}
        self._binding_to_session: dict[BindingKey, str] = {}
        self._pending: dict[str, PendingBinding] = {}
        self._pending_by_binding: dict[BindingKey, str] = {}
        self._takeovers: dict[str, TakeoverTransaction] = {}

    def get(self, session_id: str) -> Session:
        return self._sessions[session_id]

    def resolve_binding(self, key: BindingKey) -> str | None:
        return self._binding_to_session.get(key)

    def resolve_active_binding(self, key: BindingKey) -> BindingResolution:
        exact = self.resolve_binding(key)
        if exact is not None:
            channel_kind, account_id, chat_id, thread_id, root_message_id = key
            session = self._sessions.get(exact)
            if session is not None and session.status == "stopped" and not thread_id and not root_message_id:
                return BindingResolution()
            return BindingResolution(session_id=exact)
        channel_kind, account_id, chat_id, thread_id, root_message_id = key
        if root_message_id and not thread_id:
            return BindingResolution()
        candidates: list[str] = []
        for candidate_key, session_id in self._binding_to_session.items():
            candidate_channel, candidate_account, candidate_chat, candidate_thread, _candidate_root = candidate_key
            if (
                candidate_channel,
                candidate_account,
                candidate_chat,
                candidate_thread,
            ) != (channel_kind, account_id, chat_id, thread_id):
                continue
            session = self._sessions.get(session_id)
            if session is not None and (
                session.status != "stopped"
                or (bool(thread_id) and _session_is_external_tui_takeover_candidate(session))
            ):
                candidates.append(session_id)
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) == 1:
            return BindingResolution(session_id=unique_candidates[0])
        if len(unique_candidates) > 1:
            return BindingResolution(reason=BlockedReason.AMBIGUOUS_SESSION)
        return BindingResolution()

    def update_channel_binding(self, session_id: str, binding: ChannelBinding) -> None:
        session = self._sessions[session_id]
        previous = session.channel_binding
        if previous is not None:
            self._binding_to_session.pop(previous.key(), None)
        session.channel_binding = binding
        self._binding_to_session[binding.key()] = session_id

    def find_by_resume_ref(self, *, transport_kind: str, resume_ref: dict[str, Any]) -> str | None:
        target = self._resume_identity(transport_kind, resume_ref)
        if not target:
            return None
        for session_id, session in self._sessions.items():
            refs = [session.transport_ref]
            if session.writer_owner is not None:
                refs.append(session.writer_owner.external_ref)
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                nested = ref.get("resume_ref")
                if isinstance(nested, dict):
                    if self._resume_identity(transport_kind, nested) == target:
                        return session_id
                if self._resume_identity(transport_kind, ref) == target:
                    return session_id
        return None

    @staticmethod
    def _resume_identity(transport_kind: str, resume_ref: dict[str, Any]) -> str:
        if transport_kind == "claude_headless":
            return str(
                resume_ref.get("agent_session_id")
                or resume_ref.get("claude_session_id")
                or resume_ref.get("session_id")
                or ""
            )
        if transport_kind == "codex_app_server":
            return str(
                resume_ref.get("thread_id")
                or resume_ref.get("codex_thread_id")
                or ""
            )
        return str(resume_ref.get("session_id") or resume_ref.get("handle_id") or "")

    def list_sessions(
        self,
        *,
        channel_kind: str = "",
        account_id: str = "",
        chat_id: str = "",
        thread_id: str = "",
        include_archived: bool = False,
    ) -> list[SessionSummary]:
        summaries: list[SessionSummary] = []
        for session in self._sessions.values():
            if session.archived_at and not include_archived:
                continue
            binding = session.channel_binding
            if binding is None:
                continue
            if channel_kind and binding.channel_kind != channel_kind:
                continue
            if account_id and binding.account_id != account_id:
                continue
            if chat_id and binding.chat_id != chat_id:
                continue
            if thread_id and binding.thread_id != thread_id:
                continue
            summaries.append(
                SessionSummary(
                    session_id=session.session_id,
                    channel_kind=binding.channel_kind,
                    account_id=binding.account_id,
                    chat_id=binding.chat_id,
                    thread_id=binding.thread_id,
                    root_message_id=binding.root_message_id,
                    status=session.status,
                    lifecycle_state=session.lifecycle_state,
                    transport_kind=session.transport_kind,
                    cwd=session.cwd,
                    title=session.cached_title,
                    created_at=session.created_at,
                    archived_at=session.archived_at,
                    archived_by=session.archived_by,
                )
            )
        return sorted(summaries, key=lambda item: (item.created_at, item.session_id))

    def archive_session(self, session_id: str, *, actor: ActorRef, reason: str) -> ControlResult:
        session = self._sessions.get(session_id)
        if session is None:
            return ControlResult(False, BlockedReason.NOT_FOUND)
        if session.status != "stopped":
            return ControlResult(False, BlockedReason.SESSION_RUNNING)
        if not session.archived_at:
            session.archived_at = self._now()
            session.archived_by = actor.actor_id
            session.archive_reason = reason
        return ControlResult(True, state="archived")

    def add_pending_binding(self, *, pending_key: str, binding: ChannelBinding, cwd: str) -> str:
        pending = PendingBinding(
            pending_key=pending_key,
            binding=binding,
            cwd=cwd,
            created_at=self._now(),
        )
        self._pending[pending_key] = pending
        self._pending_by_binding[binding.key()] = pending_key
        return pending_key

    def resolve_pending_by_binding(self, key: BindingKey) -> str | None:
        return self._pending_by_binding.get(key)

    def commit_pending(
        self,
        pending_key: str,
        *,
        session_id: str,
        transport_kind: str,
        transport_ref: dict[str, Any],
        owner: ActorRef,
    ) -> Session:
        pending = self._pending.pop(pending_key)
        self._pending_by_binding.pop(pending.binding.key(), None)
        return self.create_structured_session(
            session_id=session_id,
            binding=pending.binding,
            transport_kind=transport_kind,
            transport_ref=transport_ref,
            cwd=pending.cwd,
            owner=owner,
        )

    def create_structured_session(
        self,
        *,
        session_id: str | None = None,
        binding: ChannelBinding,
        transport_kind: str,
        transport_ref: dict[str, Any],
        cwd: str,
        owner: ActorRef,
    ) -> Session:
        sid = session_id or f"sess-{uuid.uuid4().hex}"
        now = self._now()
        lease = WriterLease(
            lease_id=f"lease-{uuid.uuid4().hex}",
            session_id=sid,
            generation=0,
            owner_kind="orchestrator",
            holder_ref={"transport_kind": transport_kind},
            heartbeat_at=now,
            expires_at=now + self._lease_ttl,
        )
        session = Session(
            schema_version=1,
            session_id=sid,
            transport_kind=transport_kind,
            transport_ref=dict(transport_ref),
            cwd=cwd,
            channel_binding=binding,
            lifecycle_state="ACTIVE",
            writer_owner=WriterOwner(
                kind="orchestrator",
                transport_kind=transport_kind,
                actor_id=owner.actor_id,
                acquired_at=now,
            ),
            writer_lease=lease,
            generation=0,
            running_since=now,
            last_progress_at=now,
            last_progress_event="session.started",
            created_at=now,
        )
        self._sessions[sid] = session
        self._binding_to_session[binding.key()] = sid
        return session

    def create_observed_session(
        self,
        *,
        session_id: str,
        binding: ChannelBinding,
        cwd: str,
        external_ref: dict[str, Any],
        owner: ActorRef,
    ) -> Session:
        now = self._now()
        session = Session(
            schema_version=1,
            session_id=session_id,
            transport_kind="external_tui",
            transport_ref=dict(external_ref),
            cwd=cwd,
            channel_binding=binding,
            lifecycle_state="EXTERNAL_OBSERVED_READONLY",
            writer_owner=WriterOwner(
                kind="external_tui",
                actor_id=owner.actor_id,
                external_ref=dict(external_ref),
                acquired_at=now,
            ),
            writer_lease=None,
            generation=0,
            last_progress_at=now,
            last_progress_event="external_tui.observed",
            created_at=now,
        )
        self._sessions[session_id] = session
        self._binding_to_session[binding.key()] = session_id
        return session

    def validate_submit(self, session_id: str, generation: int) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        if session.writer_owner and session.writer_owner.kind == "external_tui":
            return SubmitResult(False, BlockedReason.EXTERNAL_TUI_READONLY)
        if session.writer_lease is None or session.writer_lease.expired(self._now()):
            return SubmitResult(False, BlockedReason.LEASE_EXPIRED)
        return SubmitResult(True)

    def acquire_structured_writer(
        self,
        session_id: str,
        *,
        transport_kind: str,
        transport_ref: dict[str, Any],
        owner: ActorRef,
    ) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        now = self._now()
        session.transport_kind = transport_kind
        session.transport_ref = dict(transport_ref)
        session.lifecycle_state = "ACTIVE"
        session.writer_owner = WriterOwner(
            kind="orchestrator",
            transport_kind=transport_kind,
            actor_id=owner.actor_id,
            acquired_at=now,
        )
        session.writer_lease = WriterLease(
            lease_id=f"lease-{uuid.uuid4().hex}",
            session_id=session.session_id,
            generation=session.generation,
            owner_kind="orchestrator",
            holder_ref={"transport_kind": transport_kind},
            heartbeat_at=now,
            expires_at=now + self._lease_ttl,
        )
        session.last_progress_at = now
        session.last_progress_event = "writer.reacquired"
        return SubmitResult(True)

    def handoff_to_external_tui(
        self,
        session_id: str,
        *,
        generation: int,
        owner: ActorRef,
        resume_ref: dict[str, Any],
        external_ref: dict[str, Any],
    ) -> SubmitResult:
        session = self._sessions.get(session_id)
        if session is None:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if session.status == "stopped":
            return SubmitResult(False, BlockedReason.SESSION_STOPPED)
        now = self._now()
        ref = dict(external_ref)
        ref["resume_ref"] = dict(resume_ref)
        session.generation += 1
        session.transport_kind = "external_tui"
        session.transport_ref = ref
        session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
        session.writer_owner = WriterOwner(
            kind="external_tui",
            actor_id=owner.actor_id,
            external_ref=ref,
            acquired_at=now,
        )
        session.writer_lease = None
        session.last_progress_at = now
        session.last_progress_event = "external_tui.claimed"
        return SubmitResult(True)

    def block_input(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        turn: TurnInput,
        generation: int,
        ttl: float = 600.0,
    ) -> SubmitResult:
        session = self._sessions[session_id]
        if generation != session.generation:
            return SubmitResult(False, BlockedReason.STALE_GENERATION)
        if not _session_is_external_tui_takeover_candidate(session):
            return SubmitResult(False, BlockedReason.NOT_EXTERNAL_TUI)
        now = self._now()
        blocked_id = f"blocked-{uuid.uuid4().hex}"
        session.blocked_inputs[blocked_id] = BlockedInput(
            blocked_input_id=blocked_id,
            session_id=session_id,
            actor=actor,
            text=turn.text,
            attachments=list(turn.attachments),
            idempotency_key=f"blocked:{blocked_id}",
            state="blocked",
            created_at=now,
            expires_at=now + ttl,
        )
        return SubmitResult(False, BlockedReason.EXTERNAL_TUI_READONLY, blocked_input_id=blocked_id)

    def request_takeover(
        self,
        session_id: str,
        blocked_input_id: str,
        *,
        requested_by: ActorRef,
        generation: int,
    ) -> TakeoverTransaction:
        session = self._require_takeover_session(session_id, generation)
        blocked = session.blocked_inputs.get(blocked_input_id)
        if blocked is None:
            raise TakeoverError(BlockedReason.NOT_FOUND)
        if blocked.state != "blocked":
            raise TakeoverError(f"blocked input is {blocked.state}")
        tx = TakeoverTransaction(
            takeover_id=f"takeover-{uuid.uuid4().hex}",
            session_id=session_id,
            blocked_input_id=blocked_input_id,
            requested_by=requested_by,
            requested_generation=generation,
            phase=TakeoverPhase.PROMPTED,
            created_at=self._now(),
        )
        self._takeovers[tx.takeover_id] = tx
        return tx

    def request_takeover_only(
        self,
        session_id: str,
        *,
        requested_by: ActorRef,
        generation: int,
        ttl: float = 600.0,
    ) -> TakeoverTransaction:
        session = self._require_takeover_session(session_id, generation)
        existing = self._find_takeover_only_transaction(session_id, generation)
        if existing is not None:
            return existing
        now = self._now()
        blocked_id = f"takeover-only-{uuid.uuid4().hex}"
        session.blocked_inputs[blocked_id] = BlockedInput(
            blocked_input_id=blocked_id,
            session_id=session_id,
            actor=requested_by,
            text="",
            attachments=[],
            idempotency_key=f"takeover-only:{blocked_id}",
            state="blocked",
            created_at=now,
            expires_at=now + ttl,
            submit_after_takeover=False,
        )
        tx = TakeoverTransaction(
            takeover_id=f"takeover-{uuid.uuid4().hex}",
            session_id=session_id,
            blocked_input_id=blocked_id,
            requested_by=requested_by,
            requested_generation=generation,
            phase=TakeoverPhase.PROMPTED,
            created_at=now,
        )
        self._takeovers[tx.takeover_id] = tx
        return tx

    def _find_takeover_only_transaction(self, session_id: str, generation: int) -> TakeoverTransaction | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        candidates = [
            tx
            for tx in self._takeovers.values()
            if tx.session_id == session_id and tx.requested_generation == generation
        ]
        candidates.sort(key=lambda item: item.created_at)
        for tx in candidates:
            blocked = session.blocked_inputs.get(tx.blocked_input_id)
            if blocked is None:
                continue
            if blocked.submit_after_takeover:
                continue
            return tx
        return None

    def authorize_takeover(
        self,
        takeover_id: str,
        *,
        approved_by: ActorRef,
        resume_ref: dict[str, Any] | None,
    ) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        self._require_takeover_session(tx.session_id, tx.requested_generation)
        if tx.phase != TakeoverPhase.PROMPTED:
            raise TakeoverError(f"takeover is {tx.phase}")
        tx.approved_by = approved_by
        tx.resume_ref = dict(resume_ref) if resume_ref else None
        tx.authorized_at = self._now()
        if not tx.resume_ref:
            tx.phase = TakeoverPhase.MANUAL_ONLY
            tx.reason = "no structured resume reference available"
        else:
            tx.phase = TakeoverPhase.AUTHORIZED
        return tx

    def fail_takeover(self, takeover_id: str, *, reason: str) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        if tx.phase == TakeoverPhase.COMPLETED:
            raise TakeoverError(f"takeover is {tx.phase}")
        tx.phase = TakeoverPhase.FAILED
        tx.reason = reason
        return tx

    def complete_takeover(
        self,
        takeover_id: str,
        *,
        transport_kind: str,
        transport_ref: dict[str, Any],
    ) -> TakeoverTransaction:
        tx = self._takeovers[takeover_id]
        session = self._require_takeover_session(tx.session_id, tx.requested_generation)
        if tx.phase != TakeoverPhase.AUTHORIZED:
            raise TakeoverError(f"takeover is {tx.phase}")
        blocked = session.blocked_inputs.get(tx.blocked_input_id)
        if blocked is None or blocked.state != "blocked":
            raise TakeoverError("blocked input is not pending")

        now = self._now()
        new_generation = session.generation + 1
        session.transport_kind = transport_kind
        session.transport_ref = dict(transport_ref)
        session.status = "running"
        session.stop_reason = ""
        session.lifecycle_state = "ACTIVE"
        session.writer_owner = WriterOwner(
            kind="orchestrator",
            transport_kind=transport_kind,
            actor_id=(tx.approved_by or tx.requested_by).actor_id,
            acquired_at=now,
        )
        session.writer_lease = WriterLease(
            lease_id=f"lease-{uuid.uuid4().hex}",
            session_id=session.session_id,
            generation=new_generation,
            owner_kind="orchestrator",
            holder_ref={"transport_kind": transport_kind},
            heartbeat_at=now,
            expires_at=now + self._lease_ttl,
        )
        session.generation = new_generation
        session.last_progress_at = now
        session.last_progress_event = "takeover.completed"
        blocked.state = "submitted"

        tx.phase = TakeoverPhase.COMPLETED
        tx.transport_kind = transport_kind
        tx.transport_ref = dict(transport_ref)
        tx.completed_at = now
        return tx

    def _require_takeover_session(self, session_id: str, generation: int) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise TakeoverError(BlockedReason.NOT_FOUND)
        if session.generation != generation:
            raise TakeoverError(BlockedReason.STALE_GENERATION)
        if not _session_is_external_tui_takeover_candidate(session):
            raise TakeoverError("session is not externally owned")
        return session

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_ttl": self._lease_ttl,
            "sessions": {key: _session_to_dict(value) for key, value in self._sessions.items()},
            "binding_to_session": {
                json.dumps(list(key)): value for key, value in self._binding_to_session.items()
            },
            "pending": {
                key: {
                    "pending_key": value.pending_key,
                    "binding": _binding_to_dict(value.binding),
                    "cwd": value.cwd,
                    "created_at": value.created_at,
                }
                for key, value in self._pending.items()
            },
            "takeovers": {key: self._takeover_to_dict(value) for key, value in self._takeovers.items()},
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "SessionRegistry":
        registry = cls(now=now, lease_ttl=float(data.get("lease_ttl", 30.0)))
        registry._sessions = {
            str(key): _session_from_dict(value)
            for key, value in data.get("sessions", {}).items()
            if isinstance(value, dict)
        }
        for raw_key, session_id in data.get("binding_to_session", {}).items():
            try:
                key = tuple(json.loads(raw_key))
            except Exception:
                continue
            if len(key) == 5:
                registry._binding_to_session[key] = str(session_id)  # type: ignore[index]
        if not registry._binding_to_session:
            registry._binding_to_session = {
                session.channel_binding.key(): session_id
                for session_id, session in registry._sessions.items()
                if session.channel_binding is not None
            }
        for key, value in data.get("pending", {}).items():
            if not isinstance(value, dict):
                continue
            binding = _binding_from_dict(value.get("binding"))
            if binding is None:
                continue
            pending = PendingBinding(
                pending_key=str(value.get("pending_key", key)),
                binding=binding,
                cwd=str(value.get("cwd", "")),
                created_at=float(value.get("created_at", 0.0)),
            )
            registry._pending[pending.pending_key] = pending
            registry._pending_by_binding[binding.key()] = pending.pending_key
        registry._takeovers = {
            str(key): cls._takeover_from_dict(value)
            for key, value in data.get("takeovers", {}).items()
            if isinstance(value, dict)
        }
        return registry

    @staticmethod
    def _takeover_to_dict(tx: TakeoverTransaction) -> dict[str, Any]:
        return {
            "takeover_id": tx.takeover_id,
            "session_id": tx.session_id,
            "blocked_input_id": tx.blocked_input_id,
            "requested_by": _actor_to_dict(tx.requested_by),
            "requested_generation": tx.requested_generation,
            "phase": tx.phase,
            "created_at": tx.created_at,
            "approved_by": _actor_to_dict(tx.approved_by),
            "resume_ref": dict(tx.resume_ref) if tx.resume_ref else None,
            "transport_kind": tx.transport_kind,
            "transport_ref": dict(tx.transport_ref),
            "authorized_at": tx.authorized_at,
            "completed_at": tx.completed_at,
            "reason": tx.reason,
        }

    @staticmethod
    def _takeover_from_dict(data: dict[str, Any]) -> TakeoverTransaction:
        requested_by = _actor_from_dict(data.get("requested_by")) or ActorRef("", "")
        return TakeoverTransaction(
            takeover_id=str(data.get("takeover_id", "")),
            session_id=str(data.get("session_id", "")),
            blocked_input_id=str(data.get("blocked_input_id", "")),
            requested_by=requested_by,
            requested_generation=int(data.get("requested_generation", 0)),
            phase=str(data.get("phase", TakeoverPhase.PROMPTED)),
            created_at=float(data.get("created_at", 0.0)),
            approved_by=_actor_from_dict(data.get("approved_by")),
            resume_ref=dict(data["resume_ref"]) if isinstance(data.get("resume_ref"), dict) else None,
            transport_kind=str(data.get("transport_kind", "")),
            transport_ref=dict(data.get("transport_ref", {})),
            authorized_at=data.get("authorized_at"),
            completed_at=data.get("completed_at"),
            reason=str(data.get("reason", "")),
        )


@dataclass
class InteractionContext:
    interaction_id: str
    session_id: str
    generation: int
    created_at: float
    expires_at: float
    tool_name: str
    tool_input: dict[str, Any]
    actions: list[str]
    transport_request_id: str = ""
    high_risk: bool = False
    kind: str = "permission"
    questions: list[dict[str, Any]] = field(default_factory=list)
    current_index: int = 0
    answers: dict[int, Any] = field(default_factory=dict)
    awaiting_other: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    decided_by: ActorRef | None = None
    decided_at: float | None = None
    hitl_request_id: str = ""


@dataclass
class DecisionResult:
    accepted: bool
    reason: str = ""
    decision: dict[str, Any] | None = None


@dataclass
class CallbackToken:
    token: str
    interaction_id: str
    action: str
    generation: int
    expires_at: float


class InteractionStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        token_ttl: float = 600.0,
        decided_retention: float = 86400.0,
    ):
        self._now = now
        self._token_ttl = token_ttl
        self._decided_retention = decided_retention
        self._interactions: dict[str, InteractionContext] = {}
        self._tokens: dict[str, CallbackToken] = {}
        self._awaiting_other_by_binding: dict[BindingKey, str] = {}

    def register_permission(
        self,
        *,
        session_id: str,
        generation: int,
        tool_name: str,
        tool_input: dict[str, Any],
        actions: list[str],
        transport_request_id: str = "",
        high_risk: bool = False,
        hitl_request_id: str = "",
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name=tool_name,
            tool_input=dict(tool_input),
            actions=list(actions),
            transport_request_id=transport_request_id or interaction_id,
            high_risk=high_risk,
            hitl_request_id=hitl_request_id,
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_ask_user_question(
        self,
        *,
        session_id: str,
        generation: int,
        questions: list[dict[str, Any]],
        transport_request_id: str = "",
        hitl_request_id: str = "",
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name="",
            tool_input={},
            actions=[],
            transport_request_id=transport_request_id or interaction_id,
            kind="ask_user_question",
            questions=[dict(question) for question in questions],
            hitl_request_id=hitl_request_id,
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_takeover(
        self,
        *,
        session_id: str,
        generation: int,
        takeover_id: str,
        blocked_input_id: str,
        actions: list[str] | None = None,
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name="",
            tool_input={
                "takeover_id": takeover_id,
                "blocked_input_id": blocked_input_id,
            },
            actions=list(actions or ["takeover_and_send"]),
            kind="takeover",
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def register_model_choice(
        self,
        *,
        session_id: str,
        generation: int,
        models: list[dict[str, Any]],
        current: str = "",
    ) -> InteractionContext:
        interaction_id = f"int-{uuid.uuid4().hex}"
        now = self._now()
        ctx = InteractionContext(
            interaction_id=interaction_id,
            session_id=session_id,
            generation=generation,
            created_at=now,
            expires_at=now + self._token_ttl,
            tool_name="",
            tool_input={"models": [dict(m) for m in models], "current": current},
            actions=[str(m.get("slug", "")) for m in models if m.get("slug")],
            kind="model_choice",
        )
        self._interactions[interaction_id] = ctx
        return ctx

    def get(self, interaction_id: str) -> InteractionContext:
        return self._interactions[interaction_id]

    def create_callback_token(self, interaction_id: str, action: str, *, generation: int) -> str:
        token = uuid.uuid4().hex[:20]
        self._tokens[token] = CallbackToken(
            token=token,
            interaction_id=interaction_id,
            action=action,
            generation=generation,
            expires_at=self._now() + self._token_ttl,
        )
        return token

    def decide_from_token(
        self,
        token: str,
        *,
        actor: ActorRef,
        current_generation: int,
        binding_key: BindingKey | None = None,
    ) -> DecisionResult:
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if token_state.generation != current_generation:
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        ctx = self._interactions.get(token_state.interaction_id)
        if ctx is None:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if ctx.decision is None and ctx.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if ctx.generation != current_generation:
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        if ctx.decision is not None:
            return DecisionResult(False, BlockedReason.ALREADY_DECIDED, ctx.decision)
        if ctx.kind == "ask_user_question":
            return self._decide_ask_user_question(
                ctx,
                token_state.action,
                actor=actor,
                binding_key=binding_key,
            )
        decision = {"action": token_state.action}
        ctx.decision = decision
        ctx.decided_by = actor
        ctx.decided_at = self._now()
        return DecisionResult(True, decision=decision)

    def context_for_token(self, token: str) -> InteractionContext | None:
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return None
        return self._interactions.get(token_state.interaction_id)

    def action_for_token(self, token: str) -> str:
        token_state = self._tokens.get(token)
        if token_state is None or token_state.expires_at <= self._now():
            return ""
        return token_state.action

    def awaiting_context_for_binding(self, binding_key: BindingKey) -> InteractionContext | None:
        interaction_id = self._awaiting_other_by_binding.get(binding_key)
        if interaction_id is None:
            return None
        return self._interactions.get(interaction_id)

    def begin_awaiting_other(
        self,
        interaction_id: str,
        binding_key: BindingKey,
        *,
        question_index: int,
    ) -> None:
        ctx = self._interactions[interaction_id]
        ctx.awaiting_other = {
            "binding_key": binding_key,
            "question_index": question_index,
            "started_at": self._now(),
        }
        self._awaiting_other_by_binding[binding_key] = interaction_id

    def answer_awaiting_other(
        self,
        binding_key: BindingKey,
        *,
        actor: ActorRef,
        text: str,
        current_generation: int,
    ) -> DecisionResult:
        interaction_id = self._awaiting_other_by_binding.get(binding_key)
        if interaction_id is None:
            return DecisionResult(False, BlockedReason.NOT_FOUND)
        ctx = self._interactions[interaction_id]
        if ctx.generation != current_generation:
            return DecisionResult(False, BlockedReason.STALE_GENERATION)
        if ctx.decision is None and ctx.expires_at <= self._now():
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if not ctx.awaiting_other:
            return DecisionResult(False, BlockedReason.NOT_FOUND)
        question_index = int(ctx.awaiting_other["question_index"])
        if question_index < 0 or question_index >= len(ctx.questions):
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        ctx.answers[question_index] = text
        ctx.awaiting_other = None
        self._awaiting_other_by_binding.pop(binding_key, None)
        # Free-text just fills that question; the user still submits the batch.
        return DecisionResult(
            True, decision={"action": "update", "question_index": question_index}
        )

    def interaction_count(self) -> int:
        return len(self._interactions)

    def token_count(self) -> int:
        return len(self._tokens)

    def awaiting_other_count(self) -> int:
        return len(self._awaiting_other_by_binding)

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_interaction_ids: set[str] = set()
        for interaction_id, ctx in list(self._interactions.items()):
            if ctx.decision is not None:
                decided_at = ctx.decided_at if ctx.decided_at is not None else ctx.created_at
                if decided_at + self._decided_retention <= now:
                    removed_interaction_ids.add(interaction_id)
            elif ctx.expires_at <= now:
                removed_interaction_ids.add(interaction_id)

        removed_tokens = 0
        for token, token_state in list(self._tokens.items()):
            if token_state.expires_at <= now or token_state.interaction_id in removed_interaction_ids:
                self._tokens.pop(token, None)
                removed_tokens += 1

        for interaction_id in removed_interaction_ids:
            self._interactions.pop(interaction_id, None)

        removed_awaiting = 0
        for binding_key, interaction_id in list(self._awaiting_other_by_binding.items()):
            ctx = self._interactions.get(interaction_id)
            if ctx is None or ctx.awaiting_other is None:
                self._awaiting_other_by_binding.pop(binding_key, None)
                removed_awaiting += 1

        return {
            "interactions": len(removed_interaction_ids),
            "tokens": removed_tokens,
            "awaiting_other": removed_awaiting,
        }

    def _decide_ask_user_question(
        self,
        ctx: InteractionContext,
        action: str,
        *,
        actor: ActorRef,
        binding_key: BindingKey | None,
    ) -> DecisionResult:
        # Batch model: all questions on one card. set/toggle mutate a pending
        # answer and re-render (no finalize); submit_all commits everything.
        if action == "submit_all":
            return self._finalize_ask_user(ctx, actor)
        parts = action.split(":")
        if len(parts) < 2:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        command = parts[0]
        try:
            question_index = int(parts[1])
        except ValueError:
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        if question_index < 0 or question_index >= len(ctx.questions):
            return DecisionResult(False, BlockedReason.INVALID_TOKEN)
        question = ctx.questions[question_index]

        if command in {"set", "answer"} and len(parts) == 3 and not question.get("allow_multiple"):
            try:
                option_index = int(parts[2])
            except ValueError:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            options = list(question.get("options", []))
            if option_index < 0 or option_index >= len(options):
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            ctx.answers[question_index] = options[option_index]
            # "answer" = single simple question → finalize on click; "set" =
            # batch radio → just update, wait for Submit.
            if command == "answer":
                return self._finalize_ask_user(ctx, actor)
            return DecisionResult(True, decision={"action": "update"})

        if command == "toggle" and len(parts) == 3 and question.get("allow_multiple"):
            try:
                option_index = int(parts[2])
            except ValueError:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            options = list(question.get("options", []))
            if option_index < 0 or option_index >= len(options):
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            selected = list(ctx.answers.get(question_index, []))
            value = options[option_index]
            if value in selected:
                selected.remove(value)
            else:
                selected.append(value)
            ctx.answers[question_index] = selected
            return DecisionResult(True, decision={"action": "update"})

        if command == "other" and question.get("allow_other"):
            if binding_key is None:
                return DecisionResult(False, BlockedReason.INVALID_TOKEN)
            self.begin_awaiting_other(ctx.interaction_id, binding_key, question_index=question_index)
            return DecisionResult(
                True,
                decision={"action": "awaiting_other", "question_index": question_index},
            )

        return DecisionResult(False, BlockedReason.INVALID_TOKEN)

    def _finalize_ask_user(
        self,
        ctx: InteractionContext,
        actor: ActorRef,
    ) -> DecisionResult:
        decision = {"action": "answers", "answers": dict(ctx.answers)}
        ctx.decision = decision
        ctx.decided_by = actor
        ctx.decided_at = self._now()
        return DecisionResult(True, decision=decision)

    def to_dict(self) -> dict[str, Any]:
        return {
            "token_ttl": self._token_ttl,
            "decided_retention": self._decided_retention,
            "interactions": {
                key: self._interaction_to_dict(value) for key, value in self._interactions.items()
            },
            "tokens": {key: self._token_to_dict(value) for key, value in self._tokens.items()},
            "awaiting_other_by_binding": [
                {"binding_key": list(key), "interaction_id": value}
                for key, value in self._awaiting_other_by_binding.items()
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "InteractionStore":
        store = cls(
            now=now,
            token_ttl=float(data.get("token_ttl", 600.0)),
            decided_retention=float(data.get("decided_retention", 86400.0)),
        )
        store._interactions = {
            str(key): cls._interaction_from_dict(value)
            for key, value in data.get("interactions", {}).items()
            if isinstance(value, dict)
        }
        store._tokens = {
            str(key): cls._token_from_dict(value)
            for key, value in data.get("tokens", {}).items()
            if isinstance(value, dict)
        }
        for item in data.get("awaiting_other_by_binding", []):
            if not isinstance(item, dict):
                continue
            raw_key = item.get("binding_key", [])
            if isinstance(raw_key, list) and len(raw_key) == 5:
                store._awaiting_other_by_binding[tuple(raw_key)] = str(item.get("interaction_id", ""))  # type: ignore[index]
        return store

    @staticmethod
    def _interaction_to_dict(ctx: InteractionContext) -> dict[str, Any]:
        return {
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "created_at": ctx.created_at,
            "expires_at": ctx.expires_at,
            "tool_name": ctx.tool_name,
            "tool_input": dict(ctx.tool_input),
            "actions": list(ctx.actions),
            "transport_request_id": ctx.transport_request_id,
            "high_risk": ctx.high_risk,
            "kind": ctx.kind,
            "questions": [dict(question) for question in ctx.questions],
            "current_index": ctx.current_index,
            "answers": {str(key): value for key, value in ctx.answers.items()},
            "awaiting_other": dict(ctx.awaiting_other) if ctx.awaiting_other else None,
            "decision": dict(ctx.decision) if ctx.decision else None,
            "decided_by": _actor_to_dict(ctx.decided_by),
            "decided_at": ctx.decided_at,
            "hitl_request_id": ctx.hitl_request_id,
        }

    @staticmethod
    def _interaction_from_dict(data: dict[str, Any]) -> InteractionContext:
        created_at = float(data.get("created_at", 0.0))
        interaction_id = str(data.get("interaction_id", ""))
        return InteractionContext(
            interaction_id=interaction_id,
            session_id=str(data.get("session_id", "")),
            generation=int(data.get("generation", 0)),
            created_at=created_at,
            expires_at=float(data.get("expires_at", created_at + 600.0)),
            tool_name=str(data.get("tool_name", "")),
            tool_input=dict(data.get("tool_input", {})),
            actions=[str(action) for action in data.get("actions", [])],
            transport_request_id=str(data.get("transport_request_id", "")) or interaction_id,
            high_risk=bool(data.get("high_risk", False)),
            kind=str(data.get("kind", "permission")),
            questions=[dict(question) for question in data.get("questions", []) if isinstance(question, dict)],
            current_index=int(data.get("current_index", 0)),
            answers={int(key): value for key, value in data.get("answers", {}).items()},
            awaiting_other=dict(data["awaiting_other"]) if isinstance(data.get("awaiting_other"), dict) else None,
            decision=dict(data["decision"]) if isinstance(data.get("decision"), dict) else None,
            decided_by=_actor_from_dict(data.get("decided_by")),
            decided_at=data.get("decided_at"),
            hitl_request_id=str(data.get("hitl_request_id", "")),
        )

    @staticmethod
    def _token_to_dict(token: CallbackToken) -> dict[str, Any]:
        return {
            "token": token.token,
            "interaction_id": token.interaction_id,
            "action": token.action,
            "generation": token.generation,
            "expires_at": token.expires_at,
        }

    @staticmethod
    def _token_from_dict(data: dict[str, Any]) -> CallbackToken:
        return CallbackToken(
            token=str(data.get("token", "")),
            interaction_id=str(data.get("interaction_id", "")),
            action=str(data.get("action", "")),
            generation=int(data.get("generation", 0)),
            expires_at=float(data.get("expires_at", 0.0)),
        )


class ViewModelFactory:
    _ACTION_LABELS = {
        "allow": "Allow",
        "allow_once": "Allow once",
        "deny": "Deny",
        "always_allow": "Always allow",
        "accept": "Accept",
        "acceptForSession": "Accept for session",
        "decline": "Decline",
        "cancel": "Cancel",
        "accept_edits": "Accept edits",
        "plan_auto_accept": "Plan auto-accept",
        "plan_manual_approve": "Plan manual approve",
    }

    def __init__(self, interactions: InteractionStore):
        self.interactions = interactions

    def permission_prompt(self, ctx: InteractionContext) -> dict[str, Any]:
        return {
            "type": "permission_prompt",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "tool_name": ctx.tool_name,
            "tool_input": dict(ctx.tool_input),
            "high_risk": ctx.high_risk,
            "actions": [
                {
                    "action": action,
                    "label": self._ACTION_LABELS.get(action, action.replace("_", " ").title()),
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        action,
                        generation=ctx.generation,
                    ),
                }
                for action in ctx.actions
            ],
        }

    def model_choice(self, ctx: InteractionContext) -> dict[str, Any]:
        current = str(ctx.tool_input.get("current", "") or "")
        models = ctx.tool_input.get("models", [])
        actions = []
        for model in models if isinstance(models, list) else []:
            slug = str(model.get("slug", "") or "")
            if not slug:
                continue
            display = str(model.get("display_name", "") or slug)
            actions.append(
                {
                    "action": slug,
                    "label": f"✓ {display}" if slug == current else display,
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        slug,
                        generation=ctx.generation,
                    ),
                }
            )
        return {
            "type": "model_choice",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "current": current,
            "actions": actions,
        }

    def ask_user_question_prompt(self, ctx: InteractionContext) -> dict[str, Any]:
        # All questions live in a single card: each question is its own section
        # with option buttons (single-select = radio, multi-select = toggle),
        # answers are changeable, and one global Submit finalizes everything.
        # (Feishu has no tab widget, so sections are stacked vertically.)
        def tok(action: str) -> str:
            return self.interactions.create_callback_token(
                ctx.interaction_id, action, generation=ctx.generation
            )

        # One simple question (single-select, no free-text) finalizes on a
        # single click — no separate Submit step. Any other shape (multiple
        # questions, multi-select, or free-text) uses the batch card where
        # answers are changeable and one Submit commits them all.
        immediate = (
            len(ctx.questions) == 1
            and not bool(ctx.questions[0].get("allow_multiple"))
            and not bool(ctx.questions[0].get("allow_other"))
        )
        questions: list[dict[str, Any]] = []
        for q_index, question in enumerate(ctx.questions):
            multi = bool(question.get("allow_multiple"))
            answer = ctx.answers.get(q_index)
            selected_set = set(answer) if isinstance(answer, list) else ({answer} if answer else set())
            options = []
            for o_index, option in enumerate(question.get("options", [])):
                if immediate:
                    action = f"answer:{q_index}:{o_index}"
                else:
                    action = f"{'toggle' if multi else 'set'}:{q_index}:{o_index}"
                options.append(
                    {
                        "action": action,
                        "label": str(option),
                        "selected": str(option) in {str(s) for s in selected_set},
                        "token": tok(action),
                    }
                )
            other = None
            if question.get("allow_other"):
                other_action = f"other:{q_index}"
                other = {"action": other_action, "token": tok(other_action)}
            # Answer chosen via free text (not one of the options) is shown too.
            answer_text = ""
            if isinstance(answer, str) and answer and answer not in {str(o["label"]) for o in options}:
                answer_text = answer
            elif isinstance(answer, list):
                answer_text = ", ".join(str(v) for v in answer)
            elif isinstance(answer, str):
                answer_text = answer
            questions.append(
                {
                    "index": q_index,
                    "prompt": str(question.get("prompt", "")),
                    "header": str(question.get("header", "") or ""),
                    "allow_multiple": multi,
                    "options": options,
                    "other": other,
                    "answer_display": answer_text,
                }
            )
        submit = None if immediate else {"action": "submit_all", "label": "Submit", "token": tok("submit_all")}
        # Flattened actions for channels with a generic button renderer
        # (e.g. Telegram inline keyboard); the Lark card renderer uses the
        # structured `questions` layout instead.
        flat_actions: list[dict[str, Any]] = []
        for q in questions:
            for opt in q["options"]:
                flat_actions.append(
                    {"action": opt["action"], "label": opt["label"], "token": opt["token"]}
                )
            if q["other"]:
                flat_actions.append(
                    {"action": q["other"]["action"], "label": "Other", "token": q["other"]["token"]}
                )
        if submit is not None:
            flat_actions.append(submit)
        return {
            "type": "ask_user_question",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "questions": questions,
            "submit": submit,
            "actions": flat_actions,
        }

    def takeover_prompt_for_context(
        self,
        ctx: InteractionContext,
        *,
        recoverability: str,
        summary: str,
    ) -> dict[str, Any]:
        labels = {
            "takeover_and_send": "Take over and send" if str(summary or "").strip() else "Take over",
        }
        actions = [action for action in ctx.actions if action == "takeover_and_send"]
        return {
            "type": "takeover_prompt",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "takeover_id": str(ctx.tool_input.get("takeover_id", "")),
            "blocked_input_id": str(ctx.tool_input.get("blocked_input_id", "")),
            "recoverability": recoverability,
            "summary": summary,
            "actions": [
                {
                    "action": action,
                    "label": labels.get(action, action.replace("_", " ").title()),
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        action,
                        generation=ctx.generation,
                    ),
                }
                for action in actions
            ],
        }

    def takeover_confirmation_for_context(
        self,
        ctx: InteractionContext,
        *,
        recoverability: str,
        summary: str,
    ) -> dict[str, Any]:
        actions = [
            {
                "action": "confirm_takeover",
                "label": "Confirm takeover and send",
                "token": self.interactions.create_callback_token(
                    ctx.interaction_id,
                    "confirm_takeover",
                    generation=ctx.generation,
                ),
            },
            {
                "action": "keep_readonly",
                "label": "Keep read-only",
                "token": self.interactions.create_callback_token(
                    ctx.interaction_id,
                    "keep_readonly",
                    generation=ctx.generation,
                ),
            },
            {
                "action": "manual_instructions",
                "label": "Manual steps",
                "token": self.interactions.create_callback_token(
                    ctx.interaction_id,
                    "manual_instructions",
                    generation=ctx.generation,
                ),
            },
        ]
        return {
            "type": "takeover_confirmation",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "takeover_id": str(ctx.tool_input.get("takeover_id", "")),
            "blocked_input_id": str(ctx.tool_input.get("blocked_input_id", "")),
            "recoverability": recoverability,
            "summary": summary,
            "actions": actions,
        }

    @staticmethod
    def takeover_progress(
        *,
        takeover_id: str,
        blocked_input_id: str,
        phase: str,
        summary: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        return {
            "type": "takeover_progress",
            "takeover_id": takeover_id,
            "blocked_input_id": blocked_input_id,
            "phase": phase,
            "summary": summary,
            "reason": reason,
        }

    @staticmethod
    def manual_only(
        *,
        takeover_id: str,
        blocked_input_id: str,
        summary: str = "",
        reason: str = "no structured resume reference available",
        suggested_steps: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "manual_only",
            "takeover_id": takeover_id,
            "blocked_input_id": blocked_input_id,
            "summary": summary,
            "reason": reason,
            "suggested_steps": list(
                suggested_steps
                or [
                    "Stop or finish the current TUI process.",
                    "Start a new IM-owned agent session if you want to continue from chat.",
                ]
            ),
        }

    @staticmethod
    def stale_hitl_after_takeover(request: HitlRequest) -> dict[str, Any]:
        return {
            "type": "hitl_stale",
            "hitl_request_id": request.hitl_request_id,
            "session_id": request.session_id,
            "generation": request.generation,
            "transport_request_id": request.transport_request_id,
            "native_method": request.native_method,
            "prompt_kind": request.prompt_kind,
            "reason": "The prompt belonged to the read-only TUI writer before takeover.",
        }

    @staticmethod
    def health_view(
        *,
        status: str,
        title: str,
        session_id: str,
        transport: str,
        elapsed: float,
        cwd: str,
        lifecycle_state: str = "",
        writer_owner: str = "",
        last_progress_event: str = "",
        last_event_seq: int = 0,
        readonly: bool = False,
        actions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "health",
            "status": status,
            "title": title,
            "session_id": session_id,
            "transport": transport,
            "elapsed": elapsed,
            "cwd": cwd,
            "lifecycle_state": lifecycle_state,
            "writer_owner": writer_owner,
            "last_progress_event": last_progress_event,
            "last_event_seq": last_event_seq,
            "readonly": readonly,
            "actions": list(actions or []),
        }

    @staticmethod
    def decision_result(
        *, kind: str, tool_name: str = "", action: str = "", detail: str = ""
    ) -> dict[str, Any]:
        # Terminal card shown in place of an interactive prompt once decided, so
        # a settled request no longer shows live buttons (V2 result-card parity).
        return {
            "type": "decision_result",
            "kind": kind,
            "tool_name": tool_name,
            "action": action,
            "detail": detail,
        }

    @staticmethod
    def error_view(*, code: str, message: str, retryable: bool) -> dict[str, Any]:
        return {"type": "error", "code": code, "message": message, "retryable": retryable}

    @staticmethod
    def command_menu(actions: list[dict[str, Any]]) -> dict[str, Any]:
        return {"type": "command_menu", "actions": [dict(action) for action in actions]}

    @staticmethod
    def session_chooser(
        *,
        reason: str,
        sessions: list[SessionSummary],
    ) -> dict[str, Any]:
        return {
            "type": "session_chooser",
            "reason": reason,
            "sessions": [
                {
                    "session_id": item.session_id,
                    "transport_kind": item.transport_kind,
                    "status": item.status,
                    "lifecycle_state": item.lifecycle_state,
                    "title": item.title,
                    "root_message_id": item.root_message_id,
                    "thread_id": item.thread_id,
                    "cwd": item.cwd,
                }
                for item in sessions
            ],
        }

    @staticmethod
    def takeover_prompt(
        *,
        takeover_id: str,
        blocked_input_id: str,
        recoverability: str,
        summary: str,
    ) -> dict[str, Any]:
        return {
            "type": "takeover_prompt",
            "takeover_id": takeover_id,
            "blocked_input_id": blocked_input_id,
            "recoverability": recoverability,
            "summary": summary,
            "actions": [
                {"action": "takeover_and_send", "label": "Take over and send" if str(summary or "").strip() else "Take over"},
            ],
        }


@dataclass
class DeliveryItem:
    delivery_id: str
    seq: int
    channel_binding_key: BindingKey
    view_model: dict[str, Any]
    idempotency_key: str
    attempt_count: int = 0
    created_at: float = 0.0
    next_attempt_at: float = 0.0
    last_error: str = ""
    finished_at: float = 0.0
    claim_owner: str = ""
    claim_until: float = 0.0


class DurableOutbox:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        max_attempts: int = 5,
        base_retry_delay: float = 1.0,
        sent_retention: float = 86400.0,
        dead_retention: float = 604800.0,
    ):
        self._now = now
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._sent_retention = sent_retention
        self._dead_retention = dead_retention
        self._seq = 0
        self._pending: dict[str, DeliveryItem] = {}
        self._dead: dict[str, DeliveryItem] = {}
        self._sent: dict[str, DeliveryItem] = {}

    def enqueue(
        self,
        *,
        channel_binding_key: BindingKey,
        view_model: dict[str, Any],
        idempotency_key: str,
    ) -> DeliveryItem:
        existing = self._find_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        self._seq += 1
        item = DeliveryItem(
            delivery_id=f"del-{uuid.uuid4().hex}",
            seq=self._seq,
            channel_binding_key=channel_binding_key,
            view_model=dict(view_model),
            idempotency_key=idempotency_key,
            created_at=self._now(),
            next_attempt_at=self._now(),
        )
        self._pending[item.delivery_id] = item
        return item

    def _find_by_idempotency_key(self, idempotency_key: str) -> DeliveryItem | None:
        for bucket in (self._pending, self._sent, self._dead):
            for item in bucket.values():
                if item.idempotency_key == idempotency_key:
                    return item
        return None

    def get(self, delivery_id: str) -> DeliveryItem:
        if delivery_id in self._pending:
            return self._pending[delivery_id]
        if delivery_id in self._dead:
            return self._dead[delivery_id]
        return self._sent[delivery_id]

    def pending_count(self) -> int:
        return len(self._pending)

    def dead_count(self) -> int:
        return len(self._dead)

    def sent_count(self) -> int:
        return len(self._sent)

    def pending_items(self) -> list[DeliveryItem]:
        now = self._now()
        return sorted(
            (
                item
                for item in self._pending.values()
                if item.next_attempt_at <= now and self._claim_is_available(item, now=now)
            ),
            key=lambda item: item.seq,
        )

    def claim_ready(
        self,
        *,
        owner: str,
        lease_ttl: float = 60.0,
        limit: int | None = None,
    ) -> list[DeliveryItem]:
        now = self._now()
        items = self.pending_items()
        if limit is not None:
            items = items[: max(0, int(limit))]
        claim_until = now + max(1.0, float(lease_ttl))
        for item in items:
            item.claim_owner = owner
            item.claim_until = claim_until
        return items

    @staticmethod
    def _claim_is_available(item: DeliveryItem, *, now: float) -> bool:
        return not item.claim_owner or item.claim_until <= now

    def record_result(
        self,
        delivery_id: str,
        status: str,
        error: str = "",
        *,
        claim_owner: str = "",
        retry_after: float | None = None,
    ) -> bool:
        item = self._pending.get(delivery_id)
        if item is None:
            return False
        if claim_owner and item.claim_owner and item.claim_owner != claim_owner:
            return False
        item.attempt_count += 1
        item.last_error = error
        if status == DeliveryStatus.SENT:
            item.finished_at = self._now()
            self._sent[delivery_id] = self._pending.pop(delivery_id)
            return True
        elif status == DeliveryStatus.PERMANENT_FAILURE:
            item.finished_at = self._now()
            self._dead[delivery_id] = self._pending.pop(delivery_id)
            return True
        elif status == DeliveryStatus.TRANSIENT_FAILURE:
            item.claim_owner = ""
            item.claim_until = 0.0
            if item.attempt_count >= self._max_attempts:
                item.finished_at = self._now()
                self._dead[delivery_id] = self._pending.pop(delivery_id)
                return True
            delay = self._base_retry_delay * (2 ** max(item.attempt_count - 1, 0))
            if retry_after is not None:
                delay = max(delay, max(0.0, float(retry_after)))
            item.next_attempt_at = self._now() + delay
            return True
        else:
            raise ValueError(f"unknown delivery status: {status}")

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_sent = self._compact_bucket(self._sent, now=now, retention=self._sent_retention)
        removed_dead = self._compact_bucket(self._dead, now=now, retention=self._dead_retention)
        return {"sent": removed_sent, "dead": removed_dead}

    @staticmethod
    def _compact_bucket(bucket: dict[str, DeliveryItem], *, now: float, retention: float) -> int:
        expired = []
        for delivery_id, item in bucket.items():
            finished_at = item.finished_at or item.created_at
            if finished_at + retention <= now:
                expired.append(delivery_id)
        for delivery_id in expired:
            bucket.pop(delivery_id, None)
        return len(expired)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self._seq,
            "max_attempts": self._max_attempts,
            "base_retry_delay": self._base_retry_delay,
            "sent_retention": self._sent_retention,
            "dead_retention": self._dead_retention,
            "pending": {key: _delivery_to_dict(value) for key, value in self._pending.items()},
            "dead": {key: _delivery_to_dict(value) for key, value in self._dead.items()},
            "sent": {key: _delivery_to_dict(value) for key, value in self._sent.items()},
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "DurableOutbox":
        outbox = cls(
            now=now,
            max_attempts=int(data.get("max_attempts", 5)),
            base_retry_delay=float(data.get("base_retry_delay", 1.0)),
            sent_retention=float(data.get("sent_retention", 86400.0)),
            dead_retention=float(data.get("dead_retention", 604800.0)),
        )
        outbox._seq = int(data.get("seq", 0))
        outbox._pending = {
            str(key): _delivery_from_dict(value) for key, value in data.get("pending", {}).items()
        }
        outbox._dead = {str(key): _delivery_from_dict(value) for key, value in data.get("dead", {}).items()}
        outbox._sent = {str(key): _delivery_from_dict(value) for key, value in data.get("sent", {}).items()}
        return outbox


class InboundLedger:
    def __init__(self, *, now: Callable[[], float] = time.time, ttl: float = 3600.0):
        self._now = now
        self._ttl = ttl
        self._completed: dict[str, float] = {}
        self._in_progress: dict[str, float] = {}

    def record(self, event_id: str) -> bool:
        if not self.start(event_id):
            return False
        self.complete(event_id)
        return True

    def start(self, event_id: str) -> bool:
        now = self._now()
        self._expire(now)
        if event_id in self._completed or event_id in self._in_progress:
            return False
        self._in_progress[event_id] = now + self._ttl
        return True

    def complete(self, event_id: str) -> None:
        self._in_progress.pop(event_id, None)
        self._completed[event_id] = self._now() + self._ttl

    def fail(self, event_id: str) -> None:
        self._in_progress.pop(event_id, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ttl": self._ttl,
            "completed": dict(self._completed),
            "in_progress": dict(self._in_progress),
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "InboundLedger":
        ledger = cls(now=now, ttl=float(data.get("ttl", 3600.0)))
        ledger._completed = {str(key): float(value) for key, value in data.get("completed", {}).items()}
        ledger._in_progress = {str(key): float(value) for key, value in data.get("in_progress", {}).items()}
        return ledger

    def _expire(self, now: float) -> None:
        expired = [event_id for event_id, expires_at in self._completed.items() if expires_at <= now]
        for event_id in expired:
            self._completed.pop(event_id, None)
        expired_in_progress = [
            event_id for event_id, expires_at in self._in_progress.items() if expires_at <= now
        ]
        for event_id in expired_in_progress:
            self._in_progress.pop(event_id, None)


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

    async def interrupt(self, handle: TransportHandle, reason: str) -> ControlResult: ...

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult: ...

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult: ...

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult: ...

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult: ...

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
            await channel.send_view(binding, item.view_model)
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
            )
        self._notify_state_changed()

    def _notify_state_changed(self) -> None:
        if self.on_state_changed is None:
            return
        try:
            self.on_state_changed()
        except Exception:
            return


class FakeChannelAdapter:
    def __init__(self, kind: str, capabilities: ChannelCapabilities):
        self.kind = kind
        self._capabilities = capabilities
        self.sent_views: list[dict[str, Any]] = []
        self.downloaded_attachments: list[str] = []
        self.acknowledged_callbacks: list[str] = []
        self.deleted_messages: list[dict[str, Any]] = []

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str:
        self.sent_views.append({"binding": binding.key(), "view": dict(view_model)})
        return f"msg-{len(self.sent_views)}"

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool:
        self.sent_views.append(
            {
                "binding": binding.key(),
                "message_id": str(message_id),
                "view": dict(view_model),
                "edited": True,
            }
        )
        return True

    async def ack_callback(self, inbound: InboundEvent) -> None:
        self.acknowledged_callbacks.append(inbound.event_id)

    async def delete_message(self, binding: ChannelBinding, message_id: str) -> bool:
        self.deleted_messages.append({"binding": binding.key(), "message_id": str(message_id)})
        return True

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        self.downloaded_attachments.append(attachment.source_id)
        if attachment.local_path:
            return attachment
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=attachment.mime,
            local_path=f"/tmp/walkcode-fake-downloads/{attachment.source_id}",
            source_message_id=attachment.source_message_id,
        )

    def rendered_text(self) -> str:
        parts: list[str] = []
        for item in self.sent_views:
            view = item["view"]
            parts.append(render_view_text(view))
        return "\n".join(parts)


class FakeExternalTuiController:
    def __init__(self, kind: str, *, accepted: bool = True, reason: str = ""):
        self.kind = kind
        self.accepted = accepted
        self.reason = reason
        self.terminate_calls: list[dict[str, Any]] = []

    async def terminate(self, ref: dict[str, Any], reason: str) -> ControlResult:
        self.terminate_calls.append({"ref": dict(ref), "reason": reason})
        if not self.accepted:
            return ControlResult(False, reason=self.reason or "external_tui_termination_failed")
        return ControlResult(True, state="terminated")


class LocalProcessController:
    kind = "process"

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
        kill_after_timeout: bool = True,
    ):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.kill_after_timeout = kill_after_timeout

    async def terminate(self, ref: dict[str, Any], reason: str) -> ControlResult:
        return await asyncio.to_thread(self._terminate_sync, ref, reason)

    def _terminate_sync(self, ref: dict[str, Any], _reason: str) -> ControlResult:
        try:
            pid = int(ref.get("pid", 0) or 0)
        except (TypeError, ValueError):
            return ControlResult(False, "invalid_pid")
        if pid <= 1 or pid == os.getpid():
            return ControlResult(False, "invalid_pid")
        if not bool(ref.get("allow_terminate")):
            return ControlResult(False, "termination_not_authorized")
        if not self._pid_running(pid):
            return ControlResult(True, state="already_exited")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ControlResult(True, state="already_exited")
        except PermissionError:
            return ControlResult(False, "permission_denied")
        except OSError as exc:
            return ControlResult(False, str(exc))
        if self._wait_exited(pid):
            return ControlResult(True, state="terminated")
        if not self.kill_after_timeout:
            return ControlResult(False, "process_still_running")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return ControlResult(True, state="terminated")
        except PermissionError:
            return ControlResult(False, "permission_denied")
        except OSError as exc:
            return ControlResult(False, str(exc))
        if self._wait_exited(pid):
            return ControlResult(True, state="killed")
        return ControlResult(False, "process_still_running")

    def _wait_exited(self, pid: int) -> bool:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if not self._pid_running(pid):
                return True
            time.sleep(self.poll_interval)
        return False

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            result = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except Exception:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        if result.returncode != 0:
            return False
        stat = result.stdout.strip()
        return bool(stat) and not stat.startswith("Z")


class FakeAgentTransport:
    def __init__(
        self,
        kind: str,
        capabilities: TransportCapabilities,
        *,
        scripted_events: list[AgentEvent] | None = None,
    ):
        self.kind = kind
        self._capabilities = capabilities
        self._scripted_events = list(scripted_events or [])
        self.submitted_turns: list[TurnInput] = []
        self.handles: list[TransportHandle] = []
        self.resume_specs: list[ResumeSpec] = []
        self.call_log: list[str] = []
        self.interrupt_calls: list[str] = []
        self.shutdown_calls: list[str] = []
        self.model_calls: list[str] = []
        self.permission_mode_calls: list[str] = []
        self.rewind_calls: list[str] = []
        self.permission_approval_calls: list[tuple[str, dict[str, Any]]] = []
        self.question_answer_calls: list[tuple[str, dict[str, Any]]] = []

    def capabilities(self) -> TransportCapabilities:
        return self._capabilities

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        handle = TransportHandle(
            handle_id=f"handle-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"session_id": spec.session_id, "cwd": spec.cwd},
        )
        self.handles.append(handle)
        return handle

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        self.resume_specs.append(spec)
        self.call_log.append("resume")
        ref = dict(spec.resume_ref)
        handle = TransportHandle(
            handle_id=str(ref.get("handle_id", "")) or f"handle-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={key: value for key, value in ref.items() if key not in {"transport_kind", "kind"}},
        )
        self.handles.append(handle)
        return handle

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        self.call_log.append("submit_turn")
        self.submitted_turns.append(turn)

    async def interrupt(self, handle: TransportHandle, reason: str) -> ControlResult:
        self.interrupt_calls.append(reason)
        return ControlResult(True, state="interrupted")

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        self.permission_approval_calls.append((rid, dict(decision)))

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        self.question_answer_calls.append((rid, dict(answers)))

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        self.shutdown_calls.append(mode)
        return ControlResult(True, state="stopped")

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        self.model_calls.append(model)
        return ControlResult(True, state="model_set")

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult:
        self.permission_mode_calls.append(mode)
        return ControlResult(True, state="permission_mode_set")

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult:
        self.rewind_calls.append(checkpoint_id)
        return ControlResult(True, state="checkpoint_rewound")

    async def events(self, handle: TransportHandle) -> list[AgentEvent]:
        events = self._scripted_events
        self._scripted_events = []
        return events


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def _title_from_text(text: str, *, limit: int = 80) -> str:
    collapsed = " ".join(str(text or "").strip().split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."


def _is_takeover_command(text: str) -> bool:
    first = str(text or "").strip().split(maxsplit=1)[0].lower() if str(text or "").strip() else ""
    command = first.split("@", 1)[0]
    return command in {"/takeover", "/take_over"}


def _telegram_should_render_markdown(view_model: dict[str, Any], text: str) -> bool:
    if view_model.get("type") not in {"turn_delta", "turn_completed", "text"}:
        return False
    return bool(
        re.search(
            r"(^|\n)\s{0,3}#{1,6}\s+"
            r"|(^|\n)\s*\|.+\|\s*($|\n)"
            r"|```"
            r"|`[^`\n]+`"
            r"|\*\*[^*\n][\s\S]*?\*\*"
            r"|\[[^\]\n]+\]\(https?://[^)\s]+\)",
            text,
        )
    )


def _telegram_html_from_markdown(text: str) -> str:
    parts: list[str] = []
    pos = 0
    fence_pattern = re.compile(r"```[ \t]*([A-Za-z0-9_+-]+)?[ \t]*\n?([\s\S]*?)```")
    for match in fence_pattern.finditer(text):
        parts.append(_telegram_html_from_markdown_segment(text[pos:match.start()]))
        code = html.escape(match.group(2).strip("\n"), quote=False)
        parts.append(f"<pre>{code}</pre>")
        pos = match.end()
    parts.append(_telegram_html_from_markdown_segment(text[pos:]))
    return "".join(parts)


def _telegram_html_from_markdown_segment(text: str) -> str:
    lines = text.splitlines()
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if _looks_like_markdown_table_row(stripped):
            table_lines = []
            while index < len(lines) and _looks_like_markdown_table_row(lines[index].strip()):
                table_lines.append(lines[index])
                index += 1
            rendered.append(f"<pre>{html.escape(chr(10).join(table_lines), quote=False)}</pre>")
            continue
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            rendered.append(f"<b>{_telegram_inline_markdown_to_html(heading.group(1))}</b>")
        else:
            rendered.append(_telegram_inline_markdown_to_html(line))
        index += 1
    if not lines:
        return ""
    suffix = "\n" if text.endswith("\n") else ""
    return "\n".join(rendered) + suffix


def _looks_like_markdown_table_row(line: str) -> bool:
    return line.startswith("|") and line.endswith("|") and line.count("|") >= 2


def _telegram_inline_markdown_to_html(text: str) -> str:
    code_spans: list[str] = []

    def keep_code(match: re.Match[str]) -> str:
        code_spans.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        return f"\x00CODE{len(code_spans) - 1}\x00"

    protected = re.sub(r"`([^`\n]+)`", keep_code, text)
    escaped = html.escape(protected, quote=False)
    escaped = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        lambda match: (
            f'<a href="{html.escape(match.group(2), quote=True)}">'
            f"{match.group(1)}</a>"
        ),
        escaped,
    )
    escaped = re.sub(r"\*\*([^*\n]+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"__([^_\n]+?)__", r"<u>\1</u>", escaped)
    escaped = re.sub(r"~~([^~\n]+?)~~", r"<s>\1</s>", escaped)
    for index, code in enumerate(code_spans):
        escaped = escaped.replace(f"\x00CODE{index}\x00", code)
    return escaped


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


def render_view_text(view_model: dict[str, Any]) -> str:
    if "text" in view_model:
        return str(view_model["text"])
    if "message" in view_model:
        return str(view_model["message"])
    view_type = view_model.get("type")
    if view_type == "permission_prompt":
        return f"Permission requested: {view_model.get('tool_name', '')}"
    if view_type == "ask_user_question":
        questions = view_model.get("questions")
        if isinstance(questions, list) and questions:
            titles = [
                str(q.get("header") or q.get("prompt") or "")
                for q in questions
                if isinstance(q, dict)
            ]
            return "请选择：" + " / ".join(t for t in titles if t)
        return str(view_model.get("prompt", "请选择"))
    if view_type == "tui_user_input":
        value = str(view_model.get("input", "") or "").strip()
        return f"TUI input\n\n{value}" if value else "TUI input"
    if view_type == "tool_progress":
        def _status_label(value: str) -> str:
            return {
                "running": "RUNNING",
                "completed": "COMPLETED",
                "failed": "FAILED",
            }.get(value, value.upper())

        lines = view_model.get("lines")
        entries = (
            [e for e in lines if isinstance(e, dict)]
            if isinstance(lines, list) and lines
            else [view_model]
        )
        # One tool keeps the original single-block layout; a coalesced burst
        # lists each tool on its own line.
        if len(entries) == 1:
            entry = entries[0]
            rows = [
                "Agent activity",
                f"Status: {_status_label(str(entry.get('status', '') or 'running'))}",
                f"Tool: {entry.get('tool_name', '') or 'tool'}",
            ]
            summary = str(entry.get("summary", "") or "").strip()
            if summary:
                rows.append(f"Summary: {summary}")
            return "\n".join(rows)
        rows = ["Agent activity"]
        for entry in entries:
            label = _status_label(str(entry.get("status", "") or "running"))
            row = f"Status: {label} — {entry.get('tool_name', '') or 'tool'}"
            summary = str(entry.get("summary", "") or "").strip()
            if summary:
                row += f" — {summary}"
            rows.append(row)
        return "\n".join(rows)
    if view_type == "health":
        elapsed = float(view_model.get("elapsed", 0.0) or 0.0)
        rows = [
            f"WalkCode session: {view_model.get('title', '')}".strip(),
            f"Status: {view_model.get('status', '')}",
            f"Agent: {view_model.get('transport', '')}",
            f"Session: {view_model.get('session_id', '')}",
            f"State: {view_model.get('lifecycle_state', '') or '-'}",
            f"Writer: {view_model.get('writer_owner', '') or '-'}",
            f"Duration: {int(elapsed)}s",
            f"Progress: {view_model.get('last_progress_event', '') or '-'}",
            f"Seq: {view_model.get('last_event_seq', 0)}",
            f"Cwd: {view_model.get('cwd', '')}",
        ]
        if view_model.get("readonly"):
            rows.append("Input: read-only until takeover")
        reason = str(view_model.get("reason", "") or "")
        if reason:
            rows.append(f"Reason: {reason}")
        return "\n".join(rows)
    if view_type == "error":
        return f"{view_model.get('code', 'error')}: {view_model.get('message', '')}"
    if view_type == "command_menu":
        return "Commands"
    if view_type == "model_choice":
        return "Choose a model"
    if view_type == "decision_result":
        return f"{view_model.get('action', 'decided')}: {view_model.get('tool_name', '')}".strip()
    if view_type == "session_chooser":
        rows = [
            "Multiple active sessions match this chat.",
            "Reply inside the target session topic/thread, or start a new task from the agent bot's root chat.",
        ]
        for item in view_model.get("sessions", [])[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("session_id") or "session")
            transport = str(item.get("transport_kind", "") or "agent")
            lifecycle = str(item.get("lifecycle_state", "") or item.get("status", ""))
            root = str(item.get("root_message_id", "") or "")
            suffix = f" root={root}" if root else ""
            rows.append(f"- {title} [{transport} {lifecycle}]{suffix}")
        return "\n".join(rows)
    if view_type == "takeover_prompt":
        return f"Takeover required: {view_model.get('summary', '')}"
    if view_type == "takeover_confirmation":
        return f"Confirm takeover: {view_model.get('summary', '')}"
    if view_type == "takeover_progress":
        phase = str(view_model.get("phase", ""))
        reason = str(view_model.get("reason", ""))
        labels = {
            "terminating_external_tui": "Stopping the TUI session...",
            "resuming_structured": "Taking over the session...",
            "submitting_blocked_input": "Sending your message...",
            "failed": "Takeover failed",
        }
        if phase == "completed":
            return "Takeover completed. You can now send messages in this topic."
        label = labels.get(phase, "Takeover in progress")
        if reason:
            return f"{label}: {reason}"
        return label
    if view_type == "manual_only":
        return f"Cannot take over automatically: {view_model.get('reason', '')}"
    if view_type == "hitl_stale":
        return (
            "Previous human input request is no longer answerable after takeover.\n"
            f"Type: {view_model.get('prompt_kind', '')}\n"
            f"Reason: {view_model.get('reason', '')}"
        )
    if view_model.get("type") == "unknown_event":
        return f"[{view_model.get('event_type')}] {view_model}"
    return str(view_model)


def _compact_tool_summary(value: Any, *, limit: int = 160) -> str:
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


def _sdk_block_field(content: Any, key: str) -> Any:
    if isinstance(content, dict):
        return content.get(key, "")
    return getattr(content, key, "")


def _telegram_http_error_details(exc: urllib.error.HTTPError) -> tuple[str, float | None]:
    body = ""
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""
    retry_after: float | None = None
    description = ""
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            description = str(payload.get("description", "") or "")
            parameters = payload.get("parameters", {})
            if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                try:
                    retry_after = float(parameters["retry_after"])
                except (TypeError, ValueError):
                    retry_after = None
    reason = description or getattr(exc, "reason", "") or str(exc)
    return f"HTTP Error {exc.code}: {reason}", retry_after


class TelegramBotApi:
    def __init__(self, token: str, caller: Callable[[str, dict[str, Any]], Any] | None = None):
        self.token = token
        self._caller = caller

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._caller is not None:
            result = self._caller(method, payload)
            return await _maybe_await(result)
        return await asyncio.to_thread(self._call_sync, method, payload)

    async def download_file(self, file_path: str) -> bytes:
        if self._caller is not None:
            result = self._caller("downloadFile", {"file_path": file_path})
            result = await _maybe_await(result)
            if isinstance(result, bytes):
                return result
            if isinstance(result, str):
                return result.encode("utf-8")
            if isinstance(result, dict):
                content = result.get("content", b"")
                if isinstance(content, bytes):
                    return content
                return str(content).encode("utf-8")
            return bytes(result)
        return await asyncio.to_thread(self._download_file_sync, file_path)

    def _call_sync(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._http_timeout(method, payload)) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            message, retry_after = _telegram_http_error_details(exc)
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise TransientDeliveryError(message, retry_after=retry_after) from exc
            raise
        except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
            raise TransientDeliveryError(str(exc)) from exc

    def _download_file_sync(self, file_path: str) -> bytes:
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read()

    @staticmethod
    def _http_timeout(method: str, payload: dict[str, Any]) -> int:
        if method != "getUpdates":
            return 30
        try:
            poll_timeout = int(payload.get("timeout", 0) or 0)
        except (TypeError, ValueError):
            poll_timeout = 0
        return max(30, poll_timeout + 10)


class TelegramChannelAdapter:
    def __init__(
        self,
        api: TelegramBotApi,
        *,
        max_text_chars: int = 4096,
        use_rich_messages: bool = False,
    ):
        self.kind = "telegram"
        self.api = api
        self.use_rich_messages = use_rich_messages
        self._capabilities = ChannelCapabilities(
            thread_context=True,
            editable_message=True,
            interactive_message=True,
            interactive_update=True,
            private_callback_ack=True,
            toast_or_ephemeral_notice=False,
            force_reply=True,
            attachment_download=True,
            forum_or_topic=True,
            max_text_chars=max_text_chars,
            max_callback_payload_bytes=64,
            edit_rate_limit_hint="coalesce streaming edits",
        )
        self.sent_texts: list[str] = []

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    def parse_update(self, update: dict[str, Any]) -> InboundEvent:
        update_id = str(update.get("update_id", ""))
        if "callback_query" in update:
            query = update["callback_query"]
            message = query.get("message", {})
            sender = query.get("from", {})
            data = str(query.get("data", ""))
            token = data[3:] if data.startswith("cb:") else data
            chat = message.get("chat", {})
            return InboundEvent(
                event_id=f"telegram:{update_id}",
                channel_kind="telegram",
                account_id="bot",
                chat_id=str(chat.get("id", "")),
                thread_id=str(message.get("message_thread_id", "") or ""),
                message_id=str(message.get("message_id", "")),
                root_message_id=str(message.get("reply_to_message", {}).get("message_id", "") or ""),
                sender_id=str(sender.get("id", "")),
                sender_display=self._display_name(sender),
                text=data,
                callback={"callback_query_id": str(query.get("id", "")), "data": data, "token": token},
                raw=update,
            )
        message = update.get("message", {})
        sender = message.get("from", {})
        chat = message.get("chat", {})
        return InboundEvent(
            event_id=f"telegram:{update_id}",
            channel_kind="telegram",
            account_id="bot",
            chat_id=str(chat.get("id", "")),
            thread_id=str(message.get("message_thread_id", "") or ""),
            message_id=str(message.get("message_id", "")),
            root_message_id=str(message.get("reply_to_message", {}).get("message_id", "") or ""),
            sender_id=str(sender.get("id", "")),
            sender_display=self._display_name(sender),
            text=str(message.get("text", "") or message.get("caption", "") or ""),
            attachments=self._attachments_from_message(message),
            raw=update,
        )

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str:
        text = self._text_from_view(view_model)
        reply_markup = self._reply_markup_from_view(view_model)
        if _telegram_should_render_markdown(view_model, text):
            if self.use_rich_messages:
                rich_payload: dict[str, Any] = {
                    "chat_id": binding.chat_id,
                    "rich_message": {"markdown": text},
                }
                if binding.thread_id:
                    rich_payload["message_thread_id"] = binding.thread_id
                if reply_markup:
                    rich_payload["reply_markup"] = reply_markup
                try:
                    result = await self.api.call("sendRichMessage", rich_payload)
                    self.sent_texts.append(text)
                    return str(result.get("result", {}).get("message_id", ""))
                except (TransientDeliveryError, PermanentDeliveryError):
                    raise
                except Exception:
                    pass
            html_text = _telegram_html_from_markdown(text)
            if len(html_text) <= self._capabilities.max_text_chars:
                try:
                    result = await self._send_text_once(
                        binding,
                        html_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                    self.sent_texts.append(html_text)
                    return str(result.get("result", {}).get("message_id", ""))
                except (TransientDeliveryError, PermanentDeliveryError):
                    raise
                except Exception:
                    pass
        chunks = [{"text": chunk} for chunk in self._split_text(text)]
        last_message_id = ""
        for chunk in chunks:
            result = await self._send_text_once(binding, chunk["text"], reply_markup=reply_markup)
            self.sent_texts.append(chunk["text"])
            last_message_id = str(result.get("result", {}).get("message_id", ""))
        return last_message_id

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool:
        text = self._text_from_view(view_model)
        reply_markup = self._reply_markup_from_view(view_model)
        if _telegram_should_render_markdown(view_model, text):
            if self.use_rich_messages:
                payload: dict[str, Any] = {
                    "chat_id": binding.chat_id,
                    "message_id": message_id,
                    "rich_message": {"markdown": text},
                }
                if reply_markup:
                    payload["reply_markup"] = reply_markup
                try:
                    result = await self.api.call("editMessageText", payload)
                    return bool(result.get("ok", True))
                except (TransientDeliveryError, PermanentDeliveryError):
                    raise
                except Exception:
                    pass
            html_text = _telegram_html_from_markdown(text)
            if len(html_text) <= self._capabilities.max_text_chars:
                try:
                    return await self._edit_text_once(
                        binding,
                        message_id,
                        html_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                except (TransientDeliveryError, PermanentDeliveryError):
                    raise
                except Exception:
                    pass
        chunks = self._split_text(text)
        if len(chunks) != 1:
            return False
        return await self._edit_text_once(binding, message_id, chunks[0], reply_markup=reply_markup)

    async def _send_text_once(
        self,
        binding: ChannelBinding,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": binding.chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if binding.thread_id:
            payload["message_thread_id"] = binding.thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.api.call("sendMessage", payload)

    async def _edit_text_once(
        self,
        binding: ChannelBinding,
        message_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "",
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": binding.chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self.api.call("editMessageText", payload)
        return bool(result.get("ok", True))

    async def pin_message(self, binding: ChannelBinding, message_id: str) -> bool:
        payload: dict[str, Any] = {
            "chat_id": binding.chat_id,
            "message_id": message_id,
            "disable_notification": True,
        }
        result = await self.api.call("pinChatMessage", payload)
        return bool(result.get("ok", True))

    async def close_topic(self, binding: ChannelBinding) -> bool:
        if not binding.thread_id:
            return False
        result = await self.api.call(
            "closeForumTopic",
            {
                "chat_id": binding.chat_id,
                "message_thread_id": int(binding.thread_id),
            },
        )
        return bool(result.get("ok", True))

    async def reopen_topic(self, binding: ChannelBinding) -> bool:
        if not binding.thread_id:
            return False
        result = await self.api.call(
            "reopenForumTopic",
            {
                "chat_id": binding.chat_id,
                "message_thread_id": int(binding.thread_id),
            },
        )
        return bool(result.get("ok", True))

    async def send_action(self, binding: ChannelBinding, action: str = "typing") -> bool:
        payload: dict[str, Any] = {"chat_id": binding.chat_id, "action": action}
        if binding.thread_id:
            payload["message_thread_id"] = binding.thread_id
        result = await self.api.call("sendChatAction", payload)
        return bool(result.get("ok", True))

    async def react_to_message(self, binding: ChannelBinding, message_id: str, emoji: str = "✅") -> bool:
        if not message_id:
            return False
        result = await self.api.call(
            "setMessageReaction",
            {
                "chat_id": binding.chat_id,
                "message_id": int(message_id),
                "reaction": [{"type": "emoji", "emoji": emoji}],
            },
        )
        return bool(result.get("ok", True))

    async def set_bot_commands(self, commands: list[dict[str, str]]) -> bool:
        payload = {
            "commands": [
                {
                    "command": str(command.get("command", "")).lstrip("/").lower(),
                    "description": str(command.get("description", ""))[:256],
                }
                for command in commands
                if str(command.get("command", "")).strip()
            ],
        }
        result = await self.api.call("setMyCommands", payload)
        return bool(result.get("ok", True))

    async def delete_message(self, binding: ChannelBinding, message_id: str) -> bool:
        if not message_id:
            return False
        result = await self.api.call(
            "deleteMessage",
            {
                "chat_id": binding.chat_id,
                "message_id": int(message_id),
            },
        )
        return bool(result.get("ok", True))

    async def ack_callback(self, inbound: InboundEvent) -> None:
        callback_query_id = str((inbound.callback or {}).get("callback_query_id", ""))
        if callback_query_id:
            await self.api.call("answerCallbackQuery", {"callback_query_id": callback_query_id})

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        result = await self.api.call("getFile", {"file_id": attachment.source_id})
        file_path = str(result.get("result", {}).get("file_path", ""))
        if not file_path:
            raise PermanentDeliveryError(f"Telegram file path missing for {attachment.source_id}")
        content = await self.api.download_file(file_path)
        suffix = Path(file_path).suffix
        with tempfile.NamedTemporaryFile(
            "wb",
            prefix="walkcode-telegram-",
            suffix=suffix,
            dir=attachment_download_dir(),
            delete=False,
        ) as tmp:
            tmp.write(content)
            local_path = tmp.name
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=attachment.mime,
            local_path=local_path,
            source_message_id=attachment.source_message_id,
        )

    def rendered_text(self) -> str:
        return "\n".join(self.sent_texts)

    def _split_text(self, text: str) -> list[str]:
        if not text:
            return [""]
        limit = self._capabilities.max_text_chars
        return [text[i:i + limit] for i in range(0, len(text), limit)]

    @staticmethod
    def _display_name(sender: dict[str, Any]) -> str:
        first = str(sender.get("first_name", "") or "")
        last = str(sender.get("last_name", "") or "")
        username = str(sender.get("username", "") or "")
        return " ".join(x for x in (first, last) if x) or username

    @staticmethod
    def _attachments_from_message(message: dict[str, Any]) -> list[AttachmentRef]:
        attachments: list[AttachmentRef] = []
        source_message_id = str(message.get("message_id", ""))
        photos = message.get("photo", [])
        if isinstance(photos, list) and photos:
            def photo_weight(item: Any) -> int:
                if not isinstance(item, dict):
                    return 0
                if item.get("file_size") is not None:
                    return int(item.get("file_size", 0) or 0)
                return int(item.get("width", 0) or 0) * int(item.get("height", 0) or 0)

            best = max((item for item in photos if isinstance(item, dict)), key=photo_weight, default={})
            if best.get("file_id"):
                attachments.append(
                    AttachmentRef(
                        source_id=str(best["file_id"]),
                        mime="image/jpeg",
                        source_message_id=source_message_id,
                    )
                )
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            attachments.append(
                AttachmentRef(
                    source_id=str(document["file_id"]),
                    mime=str(document.get("mime_type", "") or ""),
                    source_message_id=source_message_id,
                )
            )
        return attachments

    @staticmethod
    def _text_from_view(view_model: dict[str, Any]) -> str:
        return render_view_text(view_model)

    @staticmethod
    def _reply_markup_from_view(view_model: dict[str, Any]) -> dict[str, Any] | None:
        actions = view_model.get("actions")
        if not isinstance(actions, list) or not actions:
            return None
        keyboard = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            token = str(action.get("token", "") or "")
            callback_data = f"cb:{token}" if token else str(action.get("action", ""))
            keyboard.append(
                [
                    {
                        "text": str(action.get("label", "") or action.get("action", "")),
                        "callback_data": callback_data,
                    }
                ]
            )
        return {"inline_keyboard": keyboard} if keyboard else None


class LarkBotApi:
    def __init__(self, caller: Callable[[str, dict[str, Any]], Any] | None = None):
        self._caller = caller

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._caller is None:
            raise RuntimeError("LarkBotApi requires a caller in channel-native core tests")
        result = self._caller(method, payload)
        return await _maybe_await(result)


class LarkChannelAdapter:
    def __init__(self, api: LarkBotApi):
        self.kind = "lark"
        self.api = api
        self._capabilities = ChannelCapabilities(
            thread_context=True,
            editable_message=True,
            interactive_message=True,
            interactive_update=True,
            private_callback_ack=True,
            toast_or_ephemeral_notice=True,
            force_reply=False,
            attachment_download=True,
            forum_or_topic=True,
            max_text_chars=30000,
            max_callback_payload_bytes=2048,
            edit_rate_limit_hint="patch cards asynchronously",
        )
        self.sent_texts: list[str] = []

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    def binding_for(self, chat_id: str, root_id: str = "") -> ChannelBinding:
        return ChannelBinding(
            channel_kind="lark",
            account_id="bot",
            chat_id=chat_id,
            thread_id=root_id,
            root_message_id=root_id,
        )

    def parse_event(self, payload: dict[str, Any]) -> InboundEvent:
        event_id = str(payload.get("event_id", ""))
        event = payload.get("event", {})
        if "message" in event:
            message = event.get("message", {})
            sender = event.get("sender", {})
            sender_id = sender.get("sender_id", {})
            root_id = str(message.get("root_id", "") or "")
            message_id = str(message.get("message_id", "") or "")
            root = root_id or message_id
            content = self._decode_content(message.get("content", ""))
            return InboundEvent(
                event_id=f"lark:{event_id}",
                channel_kind="lark",
                account_id="bot",
                chat_id=str(message.get("chat_id", "")),
                thread_id=root,
                message_id=message_id,
                root_message_id=root,
                sender_id=str(sender_id.get("open_id", "") or event.get("open_id", "")),
                sender_display=str(sender.get("sender_type", "") or ""),
                text=self._parse_message_text(content),
                attachments=self._attachments_from_message(message, content),
                raw=payload,
            )
        action = event.get("action", {})
        value = action.get("value", {}) if isinstance(action, dict) else {}
        token = str(value.get("token", "") or value.get("callback_token", ""))
        action_name = str(value.get("action", ""))
        root_id = str(event.get("root_id", "") or "")
        message_id = str(event.get("message_id", "") or "")
        root = root_id or message_id
        return InboundEvent(
            event_id=f"lark:{event_id}",
            channel_kind="lark",
            account_id="bot",
            chat_id=str(event.get("chat_id", "")),
            thread_id=root,
            message_id=message_id,
            root_message_id=root,
            sender_id=str(event.get("open_id", "") or event.get("operator", {}).get("open_id", "")),
            sender_display="",
            text=token,
            # "data" mirrors Telegram's callback_data: tokenless buttons (e.g.
            # the status card's request_takeover) are routed by action name.
            callback={
                "token": token,
                "action": action_name,
                "data": token or action_name,
                "value": value,
            },
            raw=payload,
        )

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str:
        text = render_view_text(view_model)
        method = "sendCard" if self._is_interactive(view_model) else "sendMessage"
        payload = {
            "chat_id": binding.chat_id,
            "root_id": binding.root_message_id,
            "text": text,
            "view": dict(view_model),
        }
        result = await self.api.call(method, payload)
        self.sent_texts.append(text)
        return str(result.get("data", {}).get("message_id", ""))

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool:
        payload = {
            "chat_id": binding.chat_id,
            "message_id": message_id,
            "root_id": binding.root_message_id,
            "text": render_view_text(view_model),
            "view": dict(view_model),
        }
        result = await self.api.call("editCard", payload)
        return bool(result.get("ok", True))

    async def ack_callback(self, inbound: InboundEvent) -> None:
        await self.api.call(
            "ackCallback",
            {
                "event_id": inbound.event_id,
                "message_id": inbound.message_id,
                "token": str((inbound.callback or {}).get("token", "")),
            },
        )

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        resource_type = "image" if attachment.mime.startswith("image/") else "file"
        result = await self.api.call(
            "downloadResource",
            {
                "message_id": attachment.source_message_id,
                "file_key": attachment.source_id,
                "type": resource_type,
            },
        )
        content = self._download_content_bytes(result)
        suffix = self._download_suffix(result, attachment.mime)
        with tempfile.NamedTemporaryFile(
            "wb",
            prefix="walkcode-lark-",
            suffix=suffix,
            dir=attachment_download_dir(),
            delete=False,
        ) as tmp:
            tmp.write(content)
            local_path = tmp.name
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=attachment.mime,
            local_path=local_path,
            source_message_id=attachment.source_message_id,
        )

    def rendered_text(self) -> str:
        return "\n".join(self.sent_texts)

    @staticmethod
    def _is_interactive(view_model: dict[str, Any]) -> bool:
        return str(view_model.get("type", "")) in {
            "permission_prompt",
            "ask_user_question",
            "health",
            "status",
            "takeover_prompt",
            "takeover_confirmation",
            "takeover_progress",
            "manual_only",
            "model_choice",
        }

    @staticmethod
    def _decode_content(content: Any) -> Any:
        if not content:
            return {}
        if isinstance(content, dict):
            return content
        try:
            return json.loads(str(content))
        except json.JSONDecodeError:
            return content

    @staticmethod
    def _parse_message_text(content: Any) -> str:
        if isinstance(content, dict):
            if "text" in content:
                return str(content["text"])
            if "title" in content:
                return str(content["title"])
            return ""
        return str(content or "")

    @staticmethod
    def _attachments_from_message(message: dict[str, Any], content: Any) -> list[AttachmentRef]:
        if not isinstance(content, dict):
            return []
        message_id = str(message.get("message_id", ""))
        message_type = str(message.get("message_type", "") or message.get("msg_type", ""))
        attachments: list[AttachmentRef] = []
        image_key = str(content.get("image_key", "") or "")
        if image_key:
            attachments.append(
                AttachmentRef(
                    source_id=image_key,
                    mime="image/*",
                    source_message_id=message_id,
                )
            )
        file_key = str(content.get("file_key", "") or "")
        if file_key or message_type == "file":
            source_id = file_key or str(content.get("key", "") or "")
            if source_id:
                attachments.append(
                    AttachmentRef(
                        source_id=source_id,
                        mime=str(content.get("mime_type", "") or ""),
                        source_message_id=message_id,
                    )
                )
        return attachments

    @staticmethod
    def _download_content_bytes(result: Any) -> bytes:
        if isinstance(result, bytes):
            return result
        if isinstance(result, str):
            return result.encode("utf-8")
        if isinstance(result, dict):
            content = result.get("content", b"")
            if isinstance(content, bytes):
                return content
            return str(content).encode("utf-8")
        return bytes(result)

    @staticmethod
    def _download_suffix(result: Any, mime: str) -> str:
        if isinstance(result, dict):
            file_name = str(result.get("file_name", "") or "")
            suffix = Path(file_name).suffix
            if suffix:
                return suffix
        if mime == "application/pdf":
            return ".pdf"
        if mime.startswith("image/"):
            return ".img"
        return ""


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

    def __init__(self, *, sdk: Any, timeout: float = 1800.0, on_always_allow: Callable[[str], None] | None = None):
        self._sdk = sdk
        self._timeout = timeout
        self._on_always_allow = on_always_allow
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
            updates = self._always_allow_updates(entry)
            if self._on_always_allow is not None:
                with contextlib.suppress(Exception):
                    self._on_always_allow(str(entry.get("tool_name", "") or ""))
            return allow_cls(updated_permissions=updates or None)
        return self._deny_result(str(decision.get("reason", "") or "Denied via WalkCode"))

    def _deny_result(self, message: str) -> Any:
        deny_cls = getattr(self._sdk, "PermissionResultDeny", None)
        if deny_cls is None:
            raise CapabilityUnsupported("Claude Agent SDK PermissionResultDeny is unavailable")
        return deny_cls(message=message or "Denied via WalkCode", interrupt=False)

    def _build_ask_updated_input(self, entry: dict[str, Any], answers: dict[Any, Any]) -> dict[str, Any]:
        questions = entry.get("tool_input", {}).get("questions", [])
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


class ClaudeHeadlessTransport:
    kind = "claude_headless"

    def __init__(
        self,
        *,
        client_factory: Callable[[LaunchSpec], Any] | None = None,
        sdk_loader: Callable[[], Any] | None = None,
        settings: str | None = None,
        cli_path: str | None = None,
        config_dir: str | None = None,
        permission_mode: str | None = None,
        permission_timeout: float = 1800.0,
    ):
        self._client_factory = client_factory
        self._sdk_loader = sdk_loader or self._default_sdk_loader
        self.settings = settings
        self.cli_path = cli_path
        self.config_dir = config_dir
        self.permission_mode = permission_mode
        self.permission_timeout = permission_timeout
        self._clients: dict[str, Any] = {}
        self._bridges: dict[str, _ClaudePermissionBridge] = {}

    def capabilities(self) -> TransportCapabilities:
        available = self._available()
        return TransportCapabilities(
            structured_input=available,
            structured_output=available,
            permission_callback=available,
            ask_user_question=available,
            interrupt=available,
            set_model=available,
            set_permission_mode=available,
            checkpoint_rewind=available,
            resume_after_complete=available,
            resume_active_turn=False,
            multi_client_observe=False,
            multi_client_write=False,
            external_tui_takeover=available,
        )

    async def launch_session(self, *, cwd: str, session_id: str) -> TransportHandle:
        return await self.launch(LaunchSpec(cwd=cwd, session_id=session_id))

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        if not self._available():
            raise TransportUnavailable("claude_agent_sdk is not installed or no client factory is configured")
        client, bridge = self._create_client(spec)
        await self._connect_client(client)
        handle = TransportHandle(
            handle_id=f"claude-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"session_id": spec.session_id, "cwd": spec.cwd},
        )
        self._clients[handle.handle_id] = client
        if bridge is not None:
            self._bridges[handle.handle_id] = bridge
        return handle

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        if not self._available():
            raise TransportUnavailable("claude_agent_sdk is not installed or no client factory is configured")
        resume_id = str(
            spec.resume_ref.get("agent_session_id")
            or spec.resume_ref.get("resume")
            or spec.resume_ref.get("session_id")
            or ""
        )
        if not resume_id:
            raise CapabilityUnsupported("Claude headless resume requires an agent session id")
        client, bridge = self._create_client(
            LaunchSpec(cwd=spec.cwd, session_id=spec.session_id),
            resume_id=resume_id,
        )
        resume = getattr(client, "resume", None)
        if resume is not None:
            await _maybe_await(resume(dict(spec.resume_ref)))
        await self._connect_client(client)
        resumed_session_id = resume_id or spec.session_id
        handle = TransportHandle(
            handle_id=f"claude-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"session_id": resumed_session_id, "agent_session_id": resumed_session_id, "cwd": spec.cwd},
        )
        self._clients[handle.handle_id] = client
        if bridge is not None:
            self._bridges[handle.handle_id] = bridge
        return handle

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        client = self._clients[handle.handle_id]
        submit = getattr(client, "submit", None)
        if submit is not None:
            await _maybe_await(submit(turn))
            return

        query = getattr(client, "query", None)
        if query is None:
            raise CapabilityUnsupported("Claude headless turn submission is not available")
        # query(text) has no attachment channel, so downloaded files are named
        # by absolute path in the prompt for Claude to open with Read. Without
        # this an attachment-only message reaches Claude as empty text.
        text = self._compose_turn_text(turn)
        try:
            await _maybe_await(query(text, session_id="default"))
        except TypeError:
            await _maybe_await(query(text))

    @staticmethod
    def _compose_turn_text(turn: TurnInput) -> str:
        paths = [
            str(a.local_path)
            for a in (turn.attachments or [])
            if getattr(a, "local_path", "")
        ]
        text = turn.text or ""
        if not paths:
            return text
        refs = "\n".join(f"- {p}" for p in paths)
        note = f"[用户发送了附件，已下载到本地，可用 Read 工具查看]\n{refs}"
        return f"{text}\n\n{note}" if text.strip() else note

    async def events(self, handle: TransportHandle):
        client = self._clients[handle.handle_id]
        bridge = self._bridges.get(handle.handle_id)
        if bridge is not None:
            # can_use_tool bridging is active: stream events so mid-turn
            # permission / AskUserQuestion cards float before the turn ends. The
            # returned async iterator is consumed by the orchestrator's single
            # drain pass, which also picks up events emitted after the human's
            # decision unblocks the SDK.
            return self._bridged_event_stream(handle, client, bridge)

        events = getattr(client, "events", None)
        if events is not None:
            raw_events = await self._collect_client_items(events())
            return list(raw_events)

        receiver = getattr(client, "receive_response", None)
        if receiver is None:
            receiver = getattr(client, "receive_messages", None)
        if receiver is None:
            raise CapabilityUnsupported("Claude headless event stream is not available")

        sdk_messages = await self._collect_client_items(receiver())
        converted: list[AgentEvent] = []
        for message in sdk_messages:
            converted.extend(self._convert_sdk_message_to_events(message))
        return converted

    async def _bridged_event_stream(self, handle: TransportHandle, client: Any, bridge: _ClaudePermissionBridge):
        """Yield SDK events while concurrently floating bridge permission events.

        The SDK message stream and the bridge's permission queue are awaited
        together with ``FIRST_COMPLETED`` so neither starves the other: while
        ``can_use_tool`` is blocked inside the SDK (waiting on a card decision),
        the message stream is naturally idle, yet the floated permission event
        still surfaces immediately. Once the human decides, ``approve_permission``
        resolves the Future, the SDK resumes, and the message stream yields the
        tool result and the turn's completion within this same pass.
        """
        receiver = getattr(client, "receive_response", None)
        if receiver is None:
            receiver = getattr(client, "receive_messages", None)
        if receiver is None:
            raise CapabilityUnsupported("Claude headless event stream is not available")
        stream_iter = receiver().__aiter__()
        msg_task: asyncio.Future | None = asyncio.ensure_future(stream_iter.__anext__())
        queue_task: asyncio.Future = asyncio.ensure_future(bridge.next_event())
        try:
            while True:
                if msg_task is None:
                    # SDK stream is exhausted (turn finished). Flush any residual
                    # floated permission events, then stop.
                    for event in bridge.drain_ready_events():
                        yield event
                    break
                done, _pending = await asyncio.wait(
                    {msg_task, queue_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_task in done:
                    floated = queue_task.result()
                    queue_task = asyncio.ensure_future(bridge.next_event())
                    if floated is not None:
                        yield floated
                    continue
                try:
                    message = msg_task.result()
                except StopAsyncIteration:
                    msg_task = None
                    continue
                msg_task = asyncio.ensure_future(stream_iter.__anext__())
                for event in self._convert_sdk_message_to_events(message):
                    yield event
        finally:
            for task in (msg_task, queue_task):
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            # Fail-safe: unblock any callback still awaiting a decision so the
            # SDK's spawned task returns (deny) instead of leaking.
            bridge.fail_pending_default_deny()

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
        client = self._clients[handle.handle_id]
        approve = getattr(client, "approve_permission", None)
        if approve is None:
            raise CapabilityUnsupported("Claude headless permission approval is not available")
        await _maybe_await(approve(rid, decision))

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
        client = self._clients[handle.handle_id]
        answer = getattr(client, "answer_user_question", None)
        if answer is None:
            raise CapabilityUnsupported("Claude headless AskUserQuestion answers are not available")
        await _maybe_await(answer(rid, answers))

    async def interrupt(self, handle: TransportHandle, reason: str) -> ControlResult:
        bridge = self._bridges.get(handle.handle_id)
        if bridge is not None:
            # Release callbacks awaiting a decision so the interrupted turn's
            # blocked can_use_tool returns (deny) instead of hanging.
            bridge.fail_pending_default_deny(reason="interrupted")
        return await self._call_client_control(handle, "interrupt", reason, state="interrupted")

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        bridge = self._bridges.pop(handle.handle_id, None)
        if bridge is not None:
            bridge.fail_pending_default_deny(reason="shutdown")
        return await self._call_client_control(handle, "shutdown", mode, state="stopped")

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        return await self._call_client_control(handle, "set_model", model, state="model_set")

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult:
        return await self._call_client_control(
            handle,
            "set_permission_mode",
            mode,
            state="permission_mode_set",
        )

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult:
        return await self._call_client_control(
            handle,
            "rewind_checkpoint",
            checkpoint_id,
            state="checkpoint_rewound",
        )

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
        await _maybe_await(method(*args))
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
        if self.settings:
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
        bridge: _ClaudePermissionBridge | None = None
        if options_cls is not None and self._permission_bridging_supported(sdk):
            bridge = _ClaudePermissionBridge(
                sdk=sdk,
                timeout=self.permission_timeout,
                on_always_allow=self._write_always_allow_rule,
            )
            option_kwargs["can_use_tool"] = bridge.can_use_tool
        try:
            if options_cls is not None:
                return client_cls(options=options_cls(**option_kwargs)), bridge
            return client_cls(), None
        except TypeError as exc:
            raise TransportUnavailable("claude_agent_sdk.ClaudeSDKClient cannot be constructed") from exc

    def _permission_bridging_supported(self, sdk: Any) -> bool:
        # Only wire can_use_tool when the SDK exposes the PermissionResult types
        # the bridge returns, and only when the mode isn't blanket-bypass. A
        # bypass mode auto-approves everything, so there is nothing to card.
        if str(self.permission_mode or "") == "bypassPermissions":
            return False
        return (
            getattr(sdk, "PermissionResultAllow", None) is not None
            and getattr(sdk, "PermissionResultDeny", None) is not None
        )

    def _write_always_allow_rule(self, tool_name: str) -> None:
        """Persist an always-allow rule into the profile's settings.json.

        Mirrors V2's ``_add_permission_rule``: append the tool to
        ``permissions.allow`` in the profile settings file. Best-effort — any
        failure (missing file, unwritable, malformed JSON) is silently skipped so
        a persistence hiccup never blocks the live decision.
        """
        tool_name = str(tool_name or "").strip()
        if not tool_name:
            return
        try:
            if self.config_dir:
                settings_path = Path(self.config_dir).expanduser() / "settings.json"
            else:
                settings_path = Path.home() / ".claude" / "settings.json"
            if not settings_path.exists():
                return
            settings = json.loads(settings_path.read_text())
            if not isinstance(settings, dict):
                return
            permissions = settings.setdefault("permissions", {})
            if not isinstance(permissions, dict):
                return
            allow = permissions.setdefault("allow", [])
            if not isinstance(allow, list):
                return
            if tool_name not in allow:
                allow.append(tool_name)
                settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        except Exception:
            return

    @staticmethod
    async def _connect_client(client: Any) -> None:
        connect = getattr(client, "connect", None)
        if connect is not None:
            try:
                await _maybe_await(connect(prompt=None))
            except TypeError:
                await _maybe_await(connect())
            return
        start = getattr(client, "start", None)
        if start is not None:
            await _maybe_await(start())

    @staticmethod
    async def _collect_client_items(source: Any) -> list[Any]:
        items = await _maybe_await(source)
        if items is None:
            return []
        if hasattr(items, "__aiter__"):
            collected = []
            async for item in items:
                collected.append(item)
            return collected
        if isinstance(items, list):
            return items
        if isinstance(items, tuple):
            return list(items)
        return [items]

    @classmethod
    def _convert_sdk_message(cls, message: Any) -> AgentEvent | list[AgentEvent] | None:
        if isinstance(message, AgentEvent):
            return message
        if isinstance(message, dict):
            return cls._convert_sdk_dict_message(message)

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
        if text:
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

        return events or None

    @classmethod
    def _convert_sdk_dict_message(cls, message: dict[str, Any]) -> AgentEvent | list[AgentEvent] | None:
        if bool(message.get("is_error")) or message.get("error") is not None:
            return AgentEvent(
                AgentEventType.SESSION_ERROR,
                {"message": str(message.get("error") or message.get("result") or "Claude SDK reported an error")},
            )
        events = cls._extract_sdk_tool_events(message)
        tool_block_message = bool(events)
        if "content" in message and not tool_block_message:
            events = cls._extract_sdk_tool_events(message.get("content"))
        text = "" if tool_block_message else cls._extract_sdk_text(message.get("content"))
        if text:
            events.append(AgentEvent(AgentEventType.TURN_DELTA, {"text": text}))
        if "result" in message or message.get("type") == "result":
            payload: dict[str, Any] = {"message": str(message.get("result", ""))}
            if message.get("session_id"):
                payload["session_id"] = str(message["session_id"])
            if "usage" in message:
                payload["usage"] = message["usage"]
            events.append(AgentEvent(AgentEventType.TURN_COMPLETED, payload))
        return events or None

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
            return [
                AgentEvent(
                    AgentEventType.TOOL_STARTED,
                    {
                        "tool_id": _sdk_block_field(content, "id"),
                        "tool_name": _sdk_block_field(content, "name") or _sdk_block_field(content, "tool_name"),
                        "summary": _compact_tool_summary(tool_input),
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


class CodexAppServerTransport:
    kind = "codex_app_server"
    _HITL_SERVER_REQUEST_METHODS = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/permissions/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
    }

    def __init__(
        self,
        *,
        client: Any,
        approval_policy: str = "never",
        sandbox: str = "read-only",
        ephemeral: bool = False,
    ):
        self.client = client
        self.approval_policy = approval_policy
        self.sandbox = sandbox
        self.ephemeral = ephemeral
        self._pending_server_requests: dict[str, dict[str, Any]] = {}

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            structured_input=True,
            structured_output=True,
            permission_callback=True,
            ask_user_question=True,
            interrupt=False,
            set_model=False,
            set_permission_mode=False,
            checkpoint_rewind=False,
            resume_after_complete=True,
            resume_active_turn=False,
            multi_client_observe=False,
            multi_client_write=False,
            external_tui_takeover=True,
        )

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        result = await self.client.request(
            "thread/start",
            {
                "cwd": spec.cwd,
                "approvalPolicy": self.approval_policy,
                "sandbox": self.sandbox,
                "ephemeral": self.ephemeral,
            },
        )
        thread_id = _codex_thread_id(result)
        return TransportHandle(
            handle_id=f"codex-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"thread_id": thread_id, "cwd": spec.cwd},
        )

    async def resume_thread(self, thread_id: str, *, cwd: str) -> TransportHandle:
        if not thread_id:
            raise ValueError("thread_id is required for Codex resume")
        result = await self.client.request("thread/resume", {"threadId": thread_id, "cwd": cwd})
        resumed = _codex_thread_id(result) or thread_id
        return TransportHandle(
            handle_id=f"codex-{uuid.uuid4().hex}",
            transport_kind=self.kind,
            ref={"thread_id": resumed, "cwd": cwd},
        )

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        thread_id = str(spec.resume_ref.get("thread_id", ""))
        return await self.resume_thread(thread_id, cwd=spec.cwd)

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        await self.client.request(
            "turn/start",
            {
                "threadId": handle.ref["thread_id"],
                "input": [
                    {"type": "text", "text": turn.text, "text_elements": []},
                ],
                "approvalPolicy": self.approval_policy,
                "idempotencyKey": idempotency_key,
            },
        )

    async def events(self, handle: TransportHandle) -> list[AgentEvent]:
        raw_events = await self.client.events(handle.ref["thread_id"])
        events: list[AgentEvent] = []
        delta_parts: list[str] = []
        for event in (self._convert_event(raw_event) for raw_event in raw_events):
            if event is None:
                continue
            if event.type == AgentEventType.TURN_DELTA:
                delta_parts.append(str(event.payload.get("text", "")))
                continue
            if delta_parts:
                events.append(AgentEvent(AgentEventType.TURN_DELTA, {"text": "".join(delta_parts)}))
                delta_parts = []
            events.append(event)
        if delta_parts:
            events.append(AgentEvent(AgentEventType.TURN_DELTA, {"text": "".join(delta_parts)}))
        return events

    async def approve_permission(self, handle: TransportHandle | None, rid: str, decision: dict[str, Any]) -> None:
        responder = getattr(self.client, "answer_request", None)
        if responder is None:
            raise CapabilityUnsupported("Codex app-server request responses are not available")
        await responder(rid, self._approval_response_for_request(rid, decision))

    async def answer_user_question(
        self,
        handle: TransportHandle | None,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        responder = getattr(self.client, "answer_request", None)
        if responder is None:
            raise CapabilityUnsupported("Codex app-server request responses are not available")
        await responder(rid, self._question_response_for_request(rid, answers))

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        raise CapabilityUnsupported("Codex app-server model switching is not verified")

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult:
        raise CapabilityUnsupported("Codex app-server permission-mode switching is not verified")

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult:
        raise CapabilityUnsupported("Codex app-server checkpoint rewind is not verified")

    def _convert_event(self, event: dict[str, Any]) -> AgentEvent | None:
        event_type = str(event.get("type", "") or event.get("method", ""))
        if self._is_hitl_server_request(event):
            rid = str(event.get("id", "") or "")
            if rid:
                self._pending_server_requests[rid] = dict(event)
            payload = event.get("params", {})
            if not isinstance(payload, dict):
                payload = {}
            if event_type in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "item/permissions/requestApproval",
            }:
                return self._permission_event_from_server_request(rid, event_type, payload)
            if event_type in {
                "item/tool/requestUserInput",
                "mcpServer/elicitation/request",
            }:
                return self._ask_user_event_from_server_request(rid, event_type, payload)
            return None
        if event_type == "event_msg":
            event_payload = event.get("payload", {})
            if not isinstance(event_payload, dict):
                return None
            codex_event_type = str(event_payload.get("type", "") or "")
            if codex_event_type == "agent_message":
                message = str(event_payload.get("message", "") or "")
                if not message:
                    return None
                return AgentEvent(AgentEventType.TURN_DELTA, {"text": message})
            if codex_event_type == "task_complete":
                return AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {
                        "message": str(event_payload.get("last_agent_message", "") or ""),
                        "usage": event_payload.get("usage", {}),
                        "status": "completed",
                        "thread_id": str(event_payload.get("threadId", "") or event_payload.get("thread_id", "") or ""),
                        "turn_id": str(event_payload.get("turn_id", "") or event_payload.get("turnId", "") or ""),
                    },
                )
            tool_event = _codex_tool_event(codex_event_type, event_payload)
            if tool_event is not None:
                return tool_event
            return None
        payload = event.get("params", event)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        if event_type == "item/agentMessage/delta":
            return AgentEvent(AgentEventType.TURN_DELTA, {"text": str(payload.get("delta", ""))})
        tool_event = _codex_tool_event(event_type, payload)
        if tool_event is not None:
            return tool_event
        if event_type == "turn/completed":
            return AgentEvent(
                AgentEventType.TURN_COMPLETED,
                {
                    "message": str(payload.get("message", "")),
                    "usage": payload.get("usage", {}),
                    "status": payload.get("status", "completed"),
                },
            )
        return None

    @classmethod
    def _is_hitl_server_request(cls, event: dict[str, Any]) -> bool:
        return (
            "id" in event
            and "method" in event
            and "result" not in event
            and "error" not in event
            and str(event.get("method", "")) in cls._HITL_SERVER_REQUEST_METHODS
        )

    @staticmethod
    def _permission_event_from_server_request(
        rid: str,
        method: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        if method == "item/commandExecution/requestApproval":
            tool_input = {
                "native_method": method,
                "thread_id": str(payload.get("threadId", "") or ""),
                "turn_id": str(payload.get("turnId", "") or ""),
                "item_id": str(payload.get("itemId", "") or ""),
                "command": str(payload.get("command", "") or ""),
                "cwd": str(payload.get("cwd", "") or ""),
                "reason": str(payload.get("reason", "") or ""),
            }
            return AgentEvent(
                AgentEventType.PERMISSION_REQUESTED,
                {
                    "rid": rid,
                    "tool_name": "Command",
                    "tool_input": tool_input,
                    "actions": _codex_approval_actions(
                        payload.get("availableDecisions"),
                        default=["accept", "acceptForSession", "decline", "cancel"],
                    ),
                    "high_risk": True,
                },
            )
        if method == "item/fileChange/requestApproval":
            tool_input = {
                "native_method": method,
                "thread_id": str(payload.get("threadId", "") or ""),
                "turn_id": str(payload.get("turnId", "") or ""),
                "item_id": str(payload.get("itemId", "") or ""),
                "grant_root": str(payload.get("grantRoot", "") or ""),
                "reason": str(payload.get("reason", "") or ""),
            }
            return AgentEvent(
                AgentEventType.PERMISSION_REQUESTED,
                {
                    "rid": rid,
                    "tool_name": "File change",
                    "tool_input": tool_input,
                    "actions": ["accept", "acceptForSession", "decline", "cancel"],
                    "high_risk": True,
                },
            )
        permissions = payload.get("permissions", {})
        if not isinstance(permissions, dict):
            permissions = {}
        tool_input = {
            "native_method": method,
            "thread_id": str(payload.get("threadId", "") or ""),
            "turn_id": str(payload.get("turnId", "") or ""),
            "item_id": str(payload.get("itemId", "") or ""),
            "cwd": str(payload.get("cwd", "") or ""),
            "reason": str(payload.get("reason", "") or ""),
            "permissions": permissions,
        }
        return AgentEvent(
            AgentEventType.PERMISSION_REQUESTED,
            {
                "rid": rid,
                "tool_name": "Permission profile",
                "tool_input": tool_input,
                "actions": ["accept", "acceptForSession", "decline"],
                "high_risk": True,
            },
        )

    @staticmethod
    def _ask_user_event_from_server_request(
        rid: str,
        method: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        if method == "item/tool/requestUserInput":
            raw_questions = payload.get("questions", [])
            questions: list[dict[str, Any]] = []
            if isinstance(raw_questions, list):
                for question in raw_questions:
                    if not isinstance(question, dict):
                        continue
                    options: list[str] = []
                    raw_options = question.get("options", [])
                    if isinstance(raw_options, list):
                        for option in raw_options:
                            if isinstance(option, dict):
                                options.append(str(option.get("label", "") or ""))
                            else:
                                options.append(str(option))
                    questions.append(
                        {
                            "id": str(question.get("id", "") or ""),
                            "header": str(question.get("header", "") or ""),
                            "prompt": str(question.get("question", "") or question.get("header", "") or ""),
                            "options": [option for option in options if option],
                            "allow_other": bool(question.get("isOther", False)),
                            "is_secret": bool(question.get("isSecret", False)),
                        }
                    )
            if not questions:
                questions = [{"prompt": "Input required", "options": [], "id": ""}]
            return AgentEvent(
                AgentEventType.ASK_USER_REQUESTED,
                {
                    "rid": rid,
                    "native_method": method,
                    "questions": questions,
                },
            )
        return AgentEvent(
            AgentEventType.ASK_USER_REQUESTED,
            {
                "rid": rid,
                "native_method": method,
                "questions": _codex_mcp_elicitation_questions(payload),
            },
        )

    def _approval_response_for_request(self, rid: str, decision: dict[str, Any]) -> dict[str, Any]:
        request = self._pending_server_requests.get(rid, {})
        method = str(request.get("method", "") or "")
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        tool_input = decision.get("_tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        if not method:
            method = str(tool_input.get("native_method", "") or "")
        action = _codex_normalize_approval_action(str(decision.get("action", "") or ""))
        if method == "item/permissions/requestApproval":
            requested = params.get("permissions", {})
            if not isinstance(requested, dict) or not requested:
                requested = tool_input.get("permissions", {})
            if not isinstance(requested, dict):
                requested = {}
            if action in {"accept", "acceptForSession"}:
                permissions = {
                    key: value
                    for key, value in {
                        "network": requested.get("network"),
                        "fileSystem": requested.get("fileSystem"),
                    }.items()
                    if value is not None
                }
                return {
                    "permissions": permissions,
                    "scope": "session" if action == "acceptForSession" else "turn",
                }
            return {"permissions": {}, "scope": "turn", "strictAutoReview": True}
        if method == "item/fileChange/requestApproval":
            return {"decision": action if action in {"accept", "acceptForSession", "decline", "cancel"} else "decline"}
        return {
            "decision": _codex_native_decision_for_action(
                action,
                params.get("availableDecisions"),
            )
        }

    def _question_response_for_request(self, rid: str, answers: dict[str, Any]) -> dict[str, Any]:
        request = self._pending_server_requests.get(rid, {})
        method = str(request.get("method", "") or "")
        params = request.get("params", {})
        if not isinstance(params, dict):
            params = {}
        questions_from_answers = answers.get("_questions", [])
        if not method and isinstance(questions_from_answers, list):
            method = "item/tool/requestUserInput"
        if method == "mcpServer/elicitation/request":
            questions = answers.get("_questions", [])
            if not isinstance(questions, list) or not questions:
                questions = _codex_mcp_elicitation_questions(params)
            action = "accept"
            content: dict[str, Any] = {}
            for index, question in enumerate(questions):
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("id", "") or index)
                value = answers.get(index, answers.get(str(index)))
                if question_id == "mcp_elicitation_action":
                    action = str(_codex_first_answer_value({0: value}) or "accept")
                    continue
                content[question_id] = _codex_mcp_answer_value(value, question)
            if action not in {"accept", "decline", "cancel"}:
                content = {"answer": action}
                action = "accept"
            return {
                "action": action,
                "content": None if action != "accept" else content,
                "_meta": params.get("_meta"),
            }
        raw_questions = params.get("questions", [])
        if not isinstance(raw_questions, list) or not raw_questions:
            raw_questions = questions_from_answers
        native_answers: dict[str, dict[str, list[str]]] = {}
        if isinstance(raw_questions, list):
            for index, question in enumerate(raw_questions):
                if not isinstance(question, dict):
                    continue
                question_id = str(question.get("id", "") or index)
                value = answers.get(index, answers.get(str(index)))
                values = value if isinstance(value, list) else [value]
                native_answers[question_id] = {
                    "answers": [str(item) for item in values if item is not None]
                }
        return {"answers": native_answers}


def _codex_tool_event(event_type: str, payload: dict[str, Any]) -> AgentEvent | None:
    normalized = event_type.lower()
    normalized_compact = re.sub(r"[^a-z0-9]+", "", normalized)
    item = payload.get("item")
    item_type = ""
    if isinstance(item, dict):
        item_type = str(item.get("type", "") or "")
    item_type_compact = re.sub(r"[^a-z0-9]+", "", item_type.lower())
    event_is_tool_like = _codex_tool_like_name(normalized_compact)
    item_is_tool_like = _codex_tool_like_name(item_type_compact)
    if not event_is_tool_like and not item_is_tool_like:
        return None
    if isinstance(item, dict):
        payload = {**payload, **item}
        normalized = f"{normalized}/{item_type.lower()}"
        normalized_compact = re.sub(r"[^a-z0-9]+", "", normalized)
    tool_name = str(
        payload.get("toolName")
        or payload.get("tool_name")
        or payload.get("name")
        or payload.get("commandName")
        or payload.get("command_name")
        or ("command" if payload.get("command") else "")
        or "tool"
    )
    tool_id = str(
        payload.get("toolCallId")
        or payload.get("tool_call_id")
        or payload.get("itemId")
        or payload.get("id")
        or ""
    )
    status = str(payload.get("status", "") or "").lower()
    if (
        any(token in normalized_compact for token in ("failed", "error", "errored"))
        or status in {"failed", "error", "errored"}
        or payload.get("error") is not None
    ):
        return AgentEvent(
            AgentEventType.TOOL_FAILED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "summary": _compact_tool_summary(payload.get("error") or payload.get("reason") or "Tool failed"),
            },
        )
    if (
        any(token in normalized_compact for token in ("completed", "succeeded", "success", "done", "result", "end"))
        or status in {"completed", "succeeded", "success", "done"}
    ):
        return AgentEvent(
            AgentEventType.TOOL_COMPLETED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "summary": _compact_tool_summary(payload.get("summary") or "Tool completed"),
            },
        )
    if (
        any(token in normalized_compact for token in ("started", "start", "call", "created", "begin", "running"))
        or status in {"running", "inprogress", "in_progress"}
    ):
        return AgentEvent(
            AgentEventType.TOOL_STARTED,
            {
                "tool_id": tool_id,
                "tool_name": tool_name,
                "summary": _compact_tool_summary(
                    payload.get("arguments")
                    or payload.get("args")
                    or payload.get("input")
                    or payload.get("command")
                    or payload.get("summary")
                    or ""
                ),
            },
        )
    return None


def _codex_approval_actions(value: Any, *, default: list[str]) -> list[str]:
    if not isinstance(value, list):
        return list(default)
    actions: list[str] = []
    for item in value:
        if isinstance(item, str):
            actions.append(item)
        elif isinstance(item, dict) and len(item) == 1:
            actions.append(str(next(iter(item.keys()))))
    return actions or list(default)


def _codex_normalize_approval_action(action: str) -> str:
    mapping = {
        "allow": "accept",
        "allow_once": "accept",
        "always_allow": "acceptForSession",
        "deny": "decline",
        "reject": "decline",
    }
    return mapping.get(action, action)


def _codex_native_decision_for_action(action: str, available: Any) -> Any:
    if isinstance(available, list):
        for item in available:
            if isinstance(item, str) and item == action:
                return item
            if isinstance(item, dict) and action in item:
                return item
    if action in {"accept", "acceptForSession", "decline", "cancel"}:
        return action
    return "decline"


def _codex_first_answer_value(answers: dict[str, Any]) -> Any:
    if not answers:
        return ""
    for key in (0, "0"):
        if key in answers:
            value = answers[key]
            if isinstance(value, list):
                return value[0] if value else ""
            return value
    first = next(iter(answers.values()))
    if isinstance(first, list):
        return first[0] if first else ""
    return first


def _codex_mcp_elicitation_questions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    message = str(payload.get("message", "") or "MCP input required")
    mode = str(payload.get("mode", "") or "")
    schema = payload.get("requestedSchema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if mode in {"form", "openai/form"} and isinstance(properties, dict) and properties:
        required = schema.get("required", []) if isinstance(schema, dict) else []
        required_ids = {str(item) for item in required} if isinstance(required, list) else set()
        questions: list[dict[str, Any]] = []
        for field_id, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            options, allow_multiple = _codex_mcp_schema_options(definition)
            title = str(definition.get("title", "") or field_id)
            description = str(definition.get("description", "") or "").strip()
            prompt = title if not description else f"{title}\n{description}"
            questions.append(
                {
                    "id": str(field_id),
                    "prompt": prompt,
                    "options": options,
                    "allow_other": not options,
                    "allow_multiple": allow_multiple,
                    "is_secret": bool(definition.get("format") == "password"),
                    "required": str(field_id) in required_ids,
                    "value_type": _codex_mcp_schema_value_type(definition),
                }
            )
        if questions:
            return questions
    return [
        {
            "id": "mcp_elicitation_action",
            "prompt": message,
            "options": ["accept", "decline", "cancel"],
            "allow_other": mode in {"form", "openai/form"},
        }
    ]


def _codex_mcp_schema_options(definition: dict[str, Any]) -> tuple[list[str], bool]:
    schema = definition
    allow_multiple = False
    if str(schema.get("type", "")) == "array" and isinstance(schema.get("items"), dict):
        allow_multiple = True
        schema = schema["items"]
    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        return [str(item) for item in enum_values], allow_multiple
    any_of = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(any_of, list):
        const_values = [
            item.get("const")
            for item in any_of
            if isinstance(item, dict) and item.get("const") is not None
        ]
        if const_values:
            return [str(item) for item in const_values], allow_multiple
    if str(schema.get("type", "")) == "boolean":
        return ["true", "false"], False
    return [], allow_multiple


def _codex_mcp_schema_value_type(definition: dict[str, Any]) -> str:
    value_type = definition.get("type")
    if isinstance(value_type, str):
        if value_type == "array" and isinstance(definition.get("items"), dict):
            item_type = definition["items"].get("type")
            return f"array:{item_type}" if isinstance(item_type, str) else "array"
        return value_type
    return ""


def _codex_mcp_answer_value(value: Any, question: dict[str, Any]) -> Any:
    value_type = str(question.get("value_type", "") or "")
    allow_multiple = bool(question.get("allow_multiple", False))
    if isinstance(value, list):
        raw_value: Any = value if allow_multiple else (value[0] if value else "")
    else:
        raw_value = value
    if allow_multiple:
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        return [_codex_mcp_scalar_answer(item, value_type.removeprefix("array:")) for item in values]
    return _codex_mcp_scalar_answer(raw_value, value_type)


def _codex_mcp_scalar_answer(value: Any, value_type: str) -> Any:
    if value is None:
        return ""
    if value_type == "boolean":
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
    if value_type == "integer":
        try:
            return int(str(value).strip())
        except ValueError:
            return value
    if value_type == "number":
        try:
            return float(str(value).strip())
        except ValueError:
            return value
    return value


def _codex_tool_like_name(value: str) -> bool:
    if not value:
        return False
    if value in {"usermessage", "agentmessage", "reasoning"}:
        return False
    return any(token in value for token in ("tool", "function", "command", "exec", "shell", "bash"))


def _codex_thread_id(result: dict[str, Any]) -> str:
    thread_id = str(result.get("threadId", "") or "")
    if thread_id:
        return thread_id
    thread = result.get("thread", {})
    if isinstance(thread, dict):
        return str(thread.get("id", "") or "")
    return ""


@dataclass
class HitlRequest:
    hitl_request_id: str
    session_id: str
    generation: int
    transport_kind: str
    transport_request_id: str
    native_method: str
    native_params: dict[str, Any]
    prompt_kind: str
    created_at: float
    expires_at: float
    status: str = "pending"
    channel_binding_key: BindingKey | None = None
    interaction_id: str = ""


@dataclass
class HitlDecision:
    hitl_request_id: str
    actor: ActorRef
    action: str
    native_response: dict[str, Any]
    decided_at: float
    delivery_status: str


class HitlStore:
    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        request_ttl: float = 3600.0,
        decided_retention: float = 86400.0,
    ):
        self._now = now
        self._request_ttl = request_ttl
        self._decided_retention = decided_retention
        self._requests: dict[str, HitlRequest] = {}
        self._by_transport: dict[tuple[str, str, str], str] = {}
        self._decisions: dict[str, HitlDecision] = {}

    def register_request(
        self,
        *,
        session_id: str,
        generation: int,
        transport_kind: str,
        transport_request_id: str,
        native_method: str,
        native_params: dict[str, Any],
        prompt_kind: str,
        channel_binding_key: BindingKey | None = None,
    ) -> HitlRequest:
        key = (session_id, transport_kind, transport_request_id)
        existing_id = self._by_transport.get(key)
        if existing_id:
            existing = self._requests.get(existing_id)
            if existing is not None and existing.status == "pending":
                return existing
        now = self._now()
        request = HitlRequest(
            hitl_request_id=f"hitl-{uuid.uuid4().hex}",
            session_id=session_id,
            generation=generation,
            transport_kind=transport_kind,
            transport_request_id=transport_request_id,
            native_method=native_method,
            native_params=dict(native_params),
            prompt_kind=prompt_kind,
            created_at=now,
            expires_at=now + self._request_ttl,
            channel_binding_key=channel_binding_key,
        )
        self._requests[request.hitl_request_id] = request
        self._by_transport[key] = request.hitl_request_id
        return request

    def attach_interaction(self, hitl_request_id: str, interaction_id: str) -> None:
        request = self._requests.get(hitl_request_id)
        if request is not None:
            request.interaction_id = interaction_id

    def get(self, hitl_request_id: str) -> HitlRequest:
        return self._requests[hitl_request_id]

    def pending_for_session(self, session_id: str) -> list[HitlRequest]:
        now = self._now()
        return [
            request
            for request in self._requests.values()
            if request.session_id == session_id
            and request.status == "pending"
            and request.expires_at > now
        ]

    def request_for_transport(
        self,
        *,
        session_id: str,
        transport_kind: str,
        transport_request_id: str,
    ) -> HitlRequest | None:
        hitl_id = self._by_transport.get((session_id, transport_kind, transport_request_id))
        if not hitl_id:
            return None
        return self._requests.get(hitl_id)

    def mark_decided(
        self,
        hitl_request_id: str,
        *,
        actor: ActorRef,
        action: str,
        native_response: dict[str, Any],
        delivery_status: str,
    ) -> HitlDecision:
        request = self._requests[hitl_request_id]
        request.status = "decided"
        decision = HitlDecision(
            hitl_request_id=hitl_request_id,
            actor=actor,
            action=action,
            native_response=dict(native_response),
            decided_at=self._now(),
            delivery_status=delivery_status,
        )
        self._decisions[hitl_request_id] = decision
        return decision

    def mark_stale(self, hitl_request_id: str) -> None:
        request = self._requests.get(hitl_request_id)
        if request is not None and request.status == "pending":
            request.status = "stale"

    def mark_pending_for_session_stale(
        self,
        session_id: str,
        *,
        through_generation: int | None = None,
    ) -> list[HitlRequest]:
        stale: list[HitlRequest] = []
        for request in self.pending_for_session(session_id):
            if through_generation is not None and request.generation > through_generation:
                continue
            request.status = "stale"
            stale.append(request)
        return stale

    def decision_for(self, hitl_request_id: str) -> HitlDecision | None:
        return self._decisions.get(hitl_request_id)

    def request_count(self) -> int:
        return len(self._requests)

    def decision_count(self) -> int:
        return len(self._decisions)

    def compact(self) -> dict[str, int]:
        now = self._now()
        removed_requests = 0
        for hitl_id, request in list(self._requests.items()):
            decided = self._decisions.get(hitl_id)
            if request.status == "pending" and request.expires_at <= now:
                request.status = "expired"
            if request.status in {"decided", "stale", "expired"}:
                reference_time = decided.decided_at if decided is not None else request.expires_at
                if reference_time + self._decided_retention <= now:
                    self._requests.pop(hitl_id, None)
                    self._decisions.pop(hitl_id, None)
                    self._by_transport.pop(
                        (request.session_id, request.transport_kind, request.transport_request_id),
                        None,
                    )
                    removed_requests += 1
        return {"requests": removed_requests}

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_ttl": self._request_ttl,
            "decided_retention": self._decided_retention,
            "requests": {
                hitl_id: self._request_to_dict(request)
                for hitl_id, request in self._requests.items()
            },
            "decisions": {
                hitl_id: self._decision_to_dict(decision)
                for hitl_id, decision in self._decisions.items()
            },
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
    ) -> "HitlStore":
        store = cls(
            now=now,
            request_ttl=float(data.get("request_ttl", 3600.0)),
            decided_retention=float(data.get("decided_retention", 86400.0)),
        )
        store._requests = {
            str(hitl_id): cls._request_from_dict(value)
            for hitl_id, value in data.get("requests", {}).items()
            if isinstance(value, dict)
        }
        store._decisions = {
            str(hitl_id): cls._decision_from_dict(value)
            for hitl_id, value in data.get("decisions", {}).items()
            if isinstance(value, dict)
        }
        for hitl_id, request in store._requests.items():
            store._by_transport[
                (request.session_id, request.transport_kind, request.transport_request_id)
            ] = hitl_id
        return store

    @staticmethod
    def _request_to_dict(request: HitlRequest) -> dict[str, Any]:
        return {
            "hitl_request_id": request.hitl_request_id,
            "session_id": request.session_id,
            "generation": request.generation,
            "transport_kind": request.transport_kind,
            "transport_request_id": request.transport_request_id,
            "native_method": request.native_method,
            "native_params": dict(request.native_params),
            "prompt_kind": request.prompt_kind,
            "created_at": request.created_at,
            "expires_at": request.expires_at,
            "status": request.status,
            "channel_binding_key": list(request.channel_binding_key) if request.channel_binding_key else None,
            "interaction_id": request.interaction_id,
        }

    @staticmethod
    def _request_from_dict(data: dict[str, Any]) -> HitlRequest:
        raw_binding = data.get("channel_binding_key")
        binding_key: BindingKey | None = None
        if isinstance(raw_binding, list) and len(raw_binding) == 5:
            binding_key = tuple(str(part) for part in raw_binding)  # type: ignore[assignment]
        return HitlRequest(
            hitl_request_id=str(data.get("hitl_request_id", "")),
            session_id=str(data.get("session_id", "")),
            generation=int(data.get("generation", 0)),
            transport_kind=str(data.get("transport_kind", "")),
            transport_request_id=str(data.get("transport_request_id", "")),
            native_method=str(data.get("native_method", "")),
            native_params=dict(data.get("native_params", {})),
            prompt_kind=str(data.get("prompt_kind", "")),
            created_at=float(data.get("created_at", 0.0)),
            expires_at=float(data.get("expires_at", 0.0)),
            status=str(data.get("status", "pending")),
            channel_binding_key=binding_key,
            interaction_id=str(data.get("interaction_id", "")),
        )

    @staticmethod
    def _decision_to_dict(decision: HitlDecision) -> dict[str, Any]:
        return {
            "hitl_request_id": decision.hitl_request_id,
            "actor": _actor_to_dict(decision.actor),
            "action": decision.action,
            "native_response": dict(decision.native_response),
            "decided_at": decision.decided_at,
            "delivery_status": decision.delivery_status,
        }

    @staticmethod
    def _decision_from_dict(data: dict[str, Any]) -> HitlDecision:
        return HitlDecision(
            hitl_request_id=str(data.get("hitl_request_id", "")),
            actor=_actor_from_dict(data.get("actor")) or ActorRef("", ""),
            action=str(data.get("action", "")),
            native_response=dict(data.get("native_response", {})),
            decided_at=float(data.get("decided_at", 0.0)),
            delivery_status=str(data.get("delivery_status", "")),
        )


@dataclass
class StateSnapshot:
    sessions: SessionRegistry
    interactions: InteractionStore
    outbox: DurableOutbox
    authz: AuthorizationStore
    inbound_ledger: InboundLedger
    hitls: HitlStore = field(default_factory=HitlStore)


class JsonFileStateStore:
    def __init__(self, path: str | Path, *, now: Callable[[], float] = time.time):
        self.path = Path(path).expanduser()
        self._now = now

    def save(
        self,
        snapshot: StateSnapshot | None = None,
        *,
        sessions: SessionRegistry | None = None,
        interactions: InteractionStore | None = None,
        outbox: DurableOutbox | None = None,
        authz: AuthorizationStore | None = None,
        inbound_ledger: InboundLedger | None = None,
        hitls: HitlStore | None = None,
    ) -> None:
        if snapshot is not None:
            sessions = snapshot.sessions
            interactions = snapshot.interactions
            outbox = snapshot.outbox
            authz = snapshot.authz
            inbound_ledger = snapshot.inbound_ledger
            hitls = snapshot.hitls
        if (
            sessions is None
            or interactions is None
            or outbox is None
            or authz is None
            or inbound_ledger is None
        ):
            raise ValueError("state snapshot or all state components are required")
        if hitls is None:
            hitls = HitlStore(now=self._now)
        payload = {
            "schema_version": 1,
            "sessions": sessions.to_dict(),
            "interactions": interactions.to_dict(),
            "outbox": outbox.to_dict(),
            "authz": authz.to_dict(),
            "inbound_ledger": inbound_ledger.to_dict(),
            "hitls": hitls.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.path.parent),
            delete=False,
        ) as tmp:
            json.dump(payload, tmp, sort_keys=True)
            tmp.write("\n")
            tmp_name = tmp.name
        os.replace(tmp_name, self.path)

    def load(self) -> StateSnapshot:
        with self.path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return StateSnapshot(
            sessions=SessionRegistry.from_dict(payload.get("sessions", {}), now=self._now),
            interactions=InteractionStore.from_dict(payload.get("interactions", {}), now=self._now),
            outbox=DurableOutbox.from_dict(payload.get("outbox", {}), now=self._now),
            authz=AuthorizationStore.from_dict(payload.get("authz", {}), now=self._now),
            inbound_ledger=InboundLedger.from_dict(payload.get("inbound_ledger", {}), now=self._now),
            hitls=HitlStore.from_dict(payload.get("hitls", {}), now=self._now),
        )


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
        self._background_event_drains: set[asyncio.Task] = set()
        self._now = now

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

    async def submit_user_input(
        self,
        session_id: str,
        turn: TurnInput,
        *,
        actor: ActorRef,
        generation: int,
    ) -> SubmitResult:
        session = self.sessions.get(session_id)
        if self.authz is not None:
            authz_result = self.authz.can_submit(session_id, actor)
            if not authz_result.allowed:
                return SubmitResult(False, authz_result.reason)
        transport = None
        if session.lifecycle_state in {"IDLE", "ERROR_RECOVERABLE"}:
            transport = self.transports.get(session.transport_kind)
            if transport is None:
                return SubmitResult(False, "transport_not_wired")
            ready = await self._ensure_writer_ready_for_submit(session, transport, actor)
            if not ready.accepted:
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
        self._record_turn_submitted(session)
        try:
            await transport.submit_turn(handle, turn, idempotency_key=f"{session_id}:{generation}:{turn.text}")
        except Exception:
            session.last_progress_at = self._now()
            session.last_progress_event = "turn.submit_failed"
            session.lifecycle_state = "ERROR_RECOVERABLE"
            await self.refresh_session_status_card(session)
            raise
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
        async def runner() -> None:
            session = self.sessions.get(session_id)
            try:
                await self._drain_events(session, transport, handle)
                await self.refresh_session_status_card(session)
            except Exception as exc:
                session.last_progress_at = self._now()
                session.last_progress_event = "turn.event_drain_failed"
                session.lifecycle_state = "ERROR_RECOVERABLE"
                session.writer_lease = None
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
        task.add_done_callback(self._background_event_drains.discard)

    def _notify_state_changed(self) -> None:
        if self.on_state_changed is None:
            return
        try:
            self.on_state_changed()
        except Exception:
            return

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
        except Exception:
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
        ref = dict(session.transport_ref)
        if session.transport_kind == "claude_headless":
            agent_session_id = str(
                ref.get("agent_session_id")
                or ref.get("claude_session_id")
                or ""
            )
            if not agent_session_id:
                return {}
            ref["agent_session_id"] = agent_session_id
            return ref
        if session.transport_kind == "codex_app_server" and not ref.get("thread_id"):
            return {}
        return ref

    async def prepare_turn_from_inbound(self, inbound: InboundEvent) -> TurnInput | SubmitResult:
        if not inbound.attachments:
            return TurnInput(text=inbound.text)
        channel = self.channels.get(inbound.channel_kind)
        if channel is None or not channel.capabilities().attachment_download:
            return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        attachments = [await channel.download_attachment(attachment) for attachment in inbound.attachments]
        return TurnInput(text=inbound.text, attachments=attachments)

    async def interrupt_session(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        reason: str,
    ) -> ControlResult:
        session = self.sessions.get(session_id)
        authz_result = self._authorize_session_control(session_id, actor, action="interrupt")
        if not authz_result.allowed:
            return ControlResult(False, authz_result.reason)
        if session.status == "stopped":
            return ControlResult(False, BlockedReason.SESSION_STOPPED)
        transport = self.transports[session.transport_kind]
        if not transport.capabilities().interrupt:
            return ControlResult(False, BlockedReason.CAPABILITY_DISABLED)
        result = await transport.interrupt(self._handle_for_session(session), reason)
        if result.accepted:
            session.lifecycle_state = "INTERRUPTED"
            session.interrupt_reason = reason
            await self.refresh_session_status_card(session)
        return result

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
        transport = self.transports[session.transport_kind]
        shutdown = getattr(transport, "shutdown", None)
        if shutdown is not None:
            result = await shutdown(self._handle_for_session(session), mode)
            if not result.accepted:
                return result
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.stop_reason = reason
        session.writer_lease = None
        session.writer_owner = WriterOwner(kind="none")
        await self.refresh_session_status_card(session)
        return ControlResult(True, state="stopped")

    async def set_session_model(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        model: str,
    ) -> ControlResult:
        return await self._run_transport_control(
            session_id,
            actor=actor,
            action="set_model",
            capability="set_model",
            invoke=lambda transport, handle: transport.set_model(handle, model),
        )

    async def set_session_permission_mode(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        mode: str,
    ) -> ControlResult:
        return await self._run_transport_control(
            session_id,
            actor=actor,
            action="set_permission_mode",
            capability="set_permission_mode",
            invoke=lambda transport, handle: transport.set_permission_mode(handle, mode),
        )

    async def rewind_session_checkpoint(
        self,
        session_id: str,
        *,
        actor: ActorRef,
        checkpoint_id: str,
    ) -> ControlResult:
        return await self._run_transport_control(
            session_id,
            actor=actor,
            action="rewind_checkpoint",
            capability="checkpoint_rewind",
            invoke=lambda transport, handle: transport.rewind_checkpoint(handle, checkpoint_id),
        )

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

    def command_menu_for_session(self, session_id: str, *, actor: ActorRef) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        authz_result = self._authorize_session_control(session_id, actor, action="command_menu")
        if not authz_result.allowed:
            return ViewModelFactory(self.interactions).command_menu([])
        if session.status == "stopped":
            if session.archived_at:
                return ViewModelFactory(self.interactions).command_menu([])
            return ViewModelFactory(self.interactions).command_menu(
                [{"action": "archive", "label": "Archive"}]
            )
        transport = self.transports[session.transport_kind]
        caps = transport.capabilities()
        actions: list[dict[str, Any]] = []
        if caps.interrupt:
            actions.append({"action": "interrupt", "label": "Interrupt"})
        actions.append({"action": "close", "label": "Close"})
        return ViewModelFactory(self.interactions).command_menu(actions)

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
            transport=session.transport_kind,
            elapsed=elapsed,
            cwd=session.cwd,
            lifecycle_state=session.lifecycle_state,
            writer_owner=session.writer_owner.kind if session.writer_owner is not None else "",
            last_progress_event=session.last_progress_event,
            last_event_seq=session.last_event_seq,
            readonly=bool(session.writer_owner and session.writer_owner.kind == "external_tui"),
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

    async def refresh_session_status_card(self, session: Session) -> None:
        binding = session.channel_binding
        if binding is None or not bool(binding.capabilities.get("status_card")):
            return
        channel = self.channels.get(binding.channel_kind)
        if channel is None:
            return
        health = self.check_session_health(session.session_id, progress_timeout=0)
        view = dict(health.view_model)
        view["actions"] = self._status_card_actions(session)
        message_id = str(binding.health_message_id or "")
        if message_id and bool(binding.capabilities.get("static_status_card")):
            return
        if message_id and channel.capabilities().editable_message:
            try:
                edited = await channel.edit_view(binding, message_id, view)
            except Exception:
                edited = False
            if edited:
                await self._sync_readonly_topic_state(session)
                return
            binding.health_message_id = ""
        try:
            new_message_id = await channel.send_view(binding, view)
        except Exception:
            return
        if new_message_id:
            binding.health_message_id = str(new_message_id)
            binding.last_message_id = str(new_message_id)
        await self._pin_status_card_if_requested(channel, binding)
        await self._sync_readonly_topic_state(session)

    @staticmethod
    def _status_card_actions(session: Session) -> list[dict[str, Any]]:
        if _session_is_external_tui_takeover_candidate(session):
            return [{"action": "request_takeover", "label": "Take over"}]
        return []

    async def _pin_status_card_if_requested(self, channel: ChannelAdapter, binding: ChannelBinding) -> None:
        if not binding.health_message_id or not bool(binding.capabilities.get("pin_status_card")):
            return
        pin = getattr(channel, "pin_message", None)
        if pin is None:
            return
        try:
            await pin(binding, binding.health_message_id)
        except Exception:
            return

    async def _sync_readonly_topic_state(self, session: Session) -> None:
        return

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
                        session = self.sessions.get(awaiting.session_id)
                        result = None
                        if self.authz is not None:
                            authz_result = self.authz.can_submit(session.session_id, actor)
                            if not authz_result.allowed:
                                result = SubmitResult(False, authz_result.reason)
                        if result is None:
                            transport = self.transports[session.transport_kind]
                        if result is None and not transport.capabilities().ask_user_question:
                            result = SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
                        if result is None:
                            decision = self.interactions.answer_awaiting_other(
                                key,
                                actor=actor,
                                text=inbound.text,
                                current_generation=session.generation,
                            )
                            if decision.accepted:
                                await self._handle_ask_user_decision(
                                    session,
                                    awaiting,
                                    decision,
                                    actor=actor,
                                    idempotency_key=f"{inbound.event_id}:ask_user_answer",
                                )
                            result = SubmitResult(decision.accepted, decision.reason)
                    else:
                        resolution = self.sessions.resolve_active_binding(key)
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
                                )
                                await self._delete_blocked_readonly_input_if_possible(inbound, session, result)
                        else:
                            session = self.sessions.get(resolution.session_id)
                            turn = await self.prepare_turn_from_inbound(inbound)
                            if isinstance(turn, SubmitResult):
                                result = turn
                            else:
                                result = await self.submit_user_input(
                                    session.session_id,
                                    turn,
                                    actor=actor,
                                    generation=session.generation,
                                )
                                await self._delete_blocked_readonly_input_if_possible(inbound, session, result)
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

    async def _delete_blocked_readonly_input_if_possible(
        self,
        inbound: InboundEvent,
        session: Session,
        result: SubmitResult,
    ) -> None:
        return

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
        if inbound.channel_kind in {"telegram", "lark"} and inbound.thread_id:
            capabilities["status_card"] = True
            capabilities["native_topic"] = True
            capabilities["pin_status_card"] = inbound.channel_kind == "telegram"
            capabilities["static_status_card"] = inbound.channel_kind == "telegram"
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
        try:
            await channel.edit_view(binding, inbound.message_id, view)
        except Exception:
            return

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
            transport = self.transports[session.transport_kind]
            if self.authz is not None:
                authz_result = self.authz.can_decide_permission(
                    ctx.session_id,
                    actor,
                    high_risk=ctx.high_risk,
                )
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if not transport.capabilities().permission_callback:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        elif ctx.kind == "ask_user_question":
            transport = self.transports[session.transport_kind]
            if self.authz is not None:
                authz_result = self.authz.can_submit(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if not transport.capabilities().ask_user_question:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        elif ctx.kind == "takeover":
            if self.authz is not None:
                authz_result = self.authz.can_takeover(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if ctx.generation != session.generation:
                return SubmitResult(False, BlockedReason.STALE_GENERATION)
        elif ctx.kind == "model_choice":
            transport = self.transports[session.transport_kind]
            if self.authz is not None:
                authz_result = self.authz.can_submit(ctx.session_id, actor)
                if not authz_result.allowed:
                    return SubmitResult(False, authz_result.reason)
            if not transport.capabilities().set_model:
                return SubmitResult(False, BlockedReason.CAPABILITY_DISABLED)
        decision = self.interactions.decide_from_token(
            token,
            actor=actor,
            current_generation=session.generation,
            binding_key=inbound.binding_key(),
        )
        if decision.accepted and ctx.kind == "permission":
            transport = self.transports[session.transport_kind]
            approval_decision = dict(decision.decision or {})
            if session.transport_kind == "codex_app_server":
                approval_decision["_tool_input"] = dict(ctx.tool_input)
            await transport.approve_permission(
                self._handle_for_session(session),
                ctx.transport_request_id or ctx.interaction_id,
                approval_decision,
            )
            if ctx.hitl_request_id:
                self.hitls.mark_decided(
                    ctx.hitl_request_id,
                    actor=actor,
                    action=str(approval_decision.get("action", "")),
                    native_response=approval_decision,
                    delivery_status=DeliveryStatus.SENT,
                )
            await self._flip_decided_card(
                inbound,
                kind="permission",
                tool_name=ctx.tool_name,
                action=str(approval_decision.get("action", "")),
            )
        if decision.accepted and ctx.kind == "ask_user_question":
            await self._handle_ask_user_decision(
                session,
                ctx,
                decision,
                actor=actor,
                idempotency_key=f"{inbound.event_id}:ask_user_view",
                edit_card=inbound,
            )
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
            return await self._handle_takeover_decision(
                session,
                ctx,
                decision,
                actor=actor,
            )
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
            return SubmitResult(result.accepted, result.reason)
        return SubmitResult(decision.accepted, decision.reason)

    async def _handle_takeover_request_callback(self, inbound: InboundEvent) -> SubmitResult:
        resolution = self.sessions.resolve_active_binding(inbound.binding_key())
        if resolution.reason:
            return SubmitResult(False, resolution.reason)
        if not resolution.session_id:
            return SubmitResult(False, BlockedReason.NOT_FOUND)
        session = self.sessions.get(resolution.session_id)
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
            except Exception:
                self.sessions.fail_takeover(takeover_id, reason="resume_failed")
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
                termination = await controller.terminate(
                    process_ref,
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
            await self._mark_pre_takeover_hitls_stale(
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
        for request in stale_requests:
            await self._send_session_view(
                session,
                ViewModelFactory.stale_hitl_after_takeover(request),
                idempotency_key=f"hitl_stale_after_takeover:{takeover_id}:{request.hitl_request_id}",
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
        if session.writer_owner is not None and session.writer_owner.kind == "external_tui":
            return session.status != "stopped" and session.lifecycle_state not in {
                "EXTERNAL_DETACHED_IMPORTABLE",
                "EXTERNAL_DETACHED_UNIMPORTABLE",
            }
        return False

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

    async def _submit_prepared_inbound(
        self,
        session: Session,
        inbound: InboundEvent,
        actor: ActorRef,
    ) -> SubmitResult:
        turn = await self.prepare_turn_from_inbound(inbound)
        if isinstance(turn, SubmitResult):
            return turn
        return await self.submit_user_input(
            session.session_id,
            turn,
            actor=actor,
            generation=session.generation,
        )

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
            transport = self.transports[session.transport_kind]
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
                self.hitls.mark_decided(
                    ctx.hitl_request_id,
                    actor=actor,
                    action="answers",
                    native_response=answers,
                    delivery_status=DeliveryStatus.SENT,
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
                try:
                    await channel.edit_view(binding, edit_card.message_id, view)
                    return
                except Exception:
                    pass
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

    async def _drain_events(
        self,
        session: Session,
        transport: AgentTransport,
        handle: TransportHandle,
    ) -> None:
        channel = self.channels[session.channel_binding.channel_kind] if session.channel_binding else None
        if channel is None or session.channel_binding is None:
            return
        last_visible_text = ""
        saw_any_event = False
        saw_turn_completed = False
        async for event in self._iter_transport_events(transport, handle):
            saw_any_event = True
            if event.type == AgentEventType.TURN_COMPLETED:
                saw_turn_completed = True
            self._record_session_progress(session, event)
            view = self._event_to_view(session, event)
            session.last_event_seq += 1
            await self.refresh_session_status_card(session)
            if view.get("type") == "tool_progress":
                await self._upsert_tool_progress_view(session, channel, view)
                continue
            visible_text = render_view_text(view)
            if not visible_text:
                continue
            if event.type == AgentEventType.TURN_COMPLETED and visible_text == last_visible_text:
                continue
            last_visible_text = visible_text
            # This message breaks any in-flight tool burst; seal it so the next
            # tools open a new progress card instead of editing a stale one.
            self._seal_tool_progress_burst(session)
            self.outbox.enqueue(
                channel_binding_key=session.channel_binding.key(),
                view_model=view,
                idempotency_key=(
                    f"{session.session_id}:{session.generation}:"
                    f"{event.seq or session.last_event_seq}:{event.type}"
                ),
            )
            await self._flush_outbox()
        if saw_any_event and not saw_turn_completed and session.lifecycle_state == "ACTIVE":
            session.last_progress_at = self._now()
            session.last_progress_event = "turn.event_stream_incomplete"
            session.lifecycle_state = "ERROR_RECOVERABLE"
            session.writer_lease = None

    async def _upsert_tool_progress_view(
        self,
        session: Session,
        channel: ChannelAdapter,
        view: dict[str, Any],
    ) -> None:
        binding = session.channel_binding
        if binding is None:
            return
        # Accumulate a burst of consecutive tool events into one card that is
        # patched in place. A tool_result (completed/failed) updates its own
        # started line (matched by tool_id) instead of appending a new one.
        lines = binding.capabilities.get("tool_progress_lines")
        if not isinstance(lines, list):
            lines = []
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
        aggregate = {"type": "tool_progress", "lines": [dict(line) for line in lines]}

        message_id = str(binding.capabilities.get("tool_progress_message_id", "") or "")
        if message_id and channel.capabilities().editable_message:
            try:
                edited = await channel.edit_view(binding, message_id, aggregate)
            except Exception:
                edited = False
            if edited:
                return
            binding.capabilities.pop("tool_progress_message_id", None)
        try:
            new_message_id = await channel.send_view(binding, aggregate)
        except Exception:
            return
        if new_message_id:
            binding.capabilities["tool_progress_message_id"] = str(new_message_id)

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
        if session.writer_owner and session.writer_owner.kind == "external_tui":
            return
        if event.type in {
            AgentEventType.TURN_DELTA,
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
            session.writer_lease = None
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
        if event.type == AgentEventType.TURN_COMPLETED:
            return {"type": "turn_completed", "message": str(event.payload.get("message", ""))}
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
                native_params = dict(event.payload)
                native_params["tool_input"] = dict(tool_input)
                hitl_request = self.hitls.register_request(
                    session_id=session.session_id,
                    generation=session.generation,
                    transport_kind=session.transport_kind,
                    transport_request_id=transport_request_id,
                    native_method=native_method,
                    native_params=native_params,
                    prompt_kind="permission",
                    channel_binding_key=session.channel_binding.key() if session.channel_binding else None,
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
                native_params = dict(event.payload)
                native_params["questions"] = [dict(question) for question in valid_questions]
                hitl_request = self.hitls.register_request(
                    session_id=session.session_id,
                    generation=session.generation,
                    transport_kind=session.transport_kind,
                    transport_request_id=transport_request_id,
                    native_method=str(event.payload.get("native_method") or "ask_user.requested"),
                    native_params=native_params,
                    prompt_kind="ask_user_question",
                    channel_binding_key=session.channel_binding.key() if session.channel_binding else None,
                )
            ctx = self.interactions.register_ask_user_question(
                session_id=session.session_id,
                generation=session.generation,
                questions=valid_questions,
                transport_request_id=transport_request_id,
                hitl_request_id=hitl_request.hitl_request_id if hitl_request else "",
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
        "keep_readonly",
    }


__all__ = [
    "ActorRef",
    "AgentEvent",
    "AgentEventType",
    "AgentTransport",
    "AttachmentRef",
    "AuthorizationResult",
    "AuthorizationStore",
    "BlockedInput",
    "BlockedReason",
    "BindingResolution",
    "CallbackToken",
    "CapabilityUnsupported",
    "ChannelAdapter",
    "ChannelBinding",
    "ChannelConfigError",
    "ChannelEndpointConfig",
    "ChannelCapabilities",
    "ChannelNativeConfig",
    "ChannelNativeE2EGates",
    "ClaudeHeadlessTransport",
    "CodexAppServerTransport",
    "ControlResult",
    "DecisionResult",
    "DeliveryItem",
    "DeliveryStatus",
    "DurableOutbox",
    "E2EGateResult",
    "E2EGateSpec",
    "ExternalTuiController",
    "FakeAgentTransport",
    "FakeChannelAdapter",
    "FakeExternalTuiController",
    "HitlDecision",
    "HitlRequest",
    "HitlStore",
    "InboundEvent",
    "InboundLedger",
    "InteractionContext",
    "InteractionStore",
    "JsonFileStateStore",
    "LarkBotApi",
    "LarkChannelAdapter",
    "LaunchSpec",
    "LegacyEnvConversionReport",
    "LegacyFeishuEnvConverter",
    "LocalProcessController",
    "Orchestrator",
    "OutboxDispatcher",
    "PermanentDeliveryError",
    "ResumeSpec",
    "Session",
    "SessionHealth",
    "SessionRegistry",
    "SessionRole",
    "SessionSummary",
    "StateSnapshot",
    "SubmitResult",
    "TakeoverError",
    "TakeoverPhase",
    "TakeoverTransaction",
    "TelegramBotApi",
    "TelegramChannelAdapter",
    "TransportCapabilities",
    "TransportHandle",
    "TransportUnavailable",
    "TransientDeliveryError",
    "TurnInput",
    "ViewModelFactory",
    "WriterLease",
    "WriterOwner",
    "render_view_text",
]
