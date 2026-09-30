"""Clean-slate channel-native core contracts for WalkCode V3.

This package is intentionally independent from the removed pre-V3 runtime.
The implementation lives in submodules; this package re-exports every name
so ``from walkcode.channel_native import X`` keeps working:

- ``models``: enums, errors, core dataclasses and small shared helpers
- ``config``: env parsing, channel endpoints, E2E gates
- ``stores``: authorization, sessions, interactions, outbox, inbound
  ledger, HITL and the JSON state store
- ``views``: view models and plain-text rendering
- ``process_control``: process probing and local TUI/worker control
- ``lark_adapter``: the Lark/Feishu channel adapter
- ``claude_headless``: Claude Agent SDK transport, permission bridge and the
  empty-turn contract (``_compose_turn_text`` / ``EMPTY_TURN_NOTICE``)
- ``codex_transport``: codex app-server transport and event mapping
  (``_CODEX_TOOL_ITEM_SPECS``)
- ``codex_app_server``: codex app-server JSON-RPC clients
- ``orchestrator``: channel/agent protocols, outbox dispatcher, orchestrator
- ``fakes``: in-memory fakes for tests

Patch module-level names where the code using them lives (for example
``walkcode.channel_native.stores._atomic_write_json``); rebinding the name on
this package does not reach the submodules.
"""

from __future__ import annotations

# The pre-split module imported these; keep them reachable as package
# attributes (tests use e.g. ``channel_native.subprocess`` and ``.signal``).
import asyncio
import calendar
import contextlib
import signal
import subprocess
import time
import uuid
import inspect
import json
import math
import os
import re
import shlex
import sys
import tempfile
import urllib.parse
import random

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from . import claude_gate

from .models import (
    ActorRef,
    agent_session_id,
    _AGENT_SESSION_ID_KEYS,
    _agent_session_identity,
    AgentEvent,
    AgentEventType,
    attachment_download_dir,
    AttachmentRef,
    AuthorizationResult,
    BindingKey,
    BindingResolution,
    BlockedInput,
    BlockedReason,
    CapabilityUnsupported,
    _CHANNEL_REVIVAL_STOP_REASONS,
    ChannelBinding,
    ChannelCapabilities,
    ChannelConfigError,
    _compact_tool_summary,
    ControlResult,
    DeliveryStatus,
    _durable_resume_ref,
    _humanize_seconds,
    InboundEvent,
    LaunchSpec,
    _log_degrade,
    _maybe_await,
    PermanentDeliveryError,
    _resume_ref_is_durable,
    ResumeSpec,
    Session,
    _session_has_durable_resume_ref,
    _session_is_channel_revival_candidate,
    _session_is_external_tui_takeover_candidate,
    SessionHealth,
    SessionRole,
    SessionSummary,
    _STRUCTURED_TRANSPORT_KINDS,
    SubmitResult,
    TakeoverError,
    TakeoverPhase,
    TakeoverTransaction,
    _TOOL_SUMMARY_LIMIT,
    TransientDeliveryError,
    TransportCapabilities,
    TransportHandle,
    TransportUnavailable,
    TurnInput,
    UnsafeSandboxError,
    _WALKCODE_SESSION_ID_RE,
    WriterOwner,
)
from .config import (
    _agent_to_transport_kind,
    _channel_allowlist_configured,
    _CHANNEL_DISPLAY_NAMES,
    _channel_environment_context,
    _CHANNEL_ENVIRONMENT_CONTEXT_TEMPLATE,
    ChannelEndpointConfig,
    ChannelNativeConfig,
    ChannelNativeE2EGates,
    _CODEX_SANDBOX_POLICY_TYPES,
    _configured_agent,
    _configured_agent_options,
    _configured_channel_kind,
    _configured_profile,
    _configured_state_path,
    E2EGateResult,
    E2EGateSpec,
    _env_bool,
    _lark_config_from_env,
    _normalize_agent_name,
    _note_retired_runtime_env,
    _PROFILE_RE,
    _reject_removed_runtime_env,
    _retired_env_noticed,
    RETIRED_RUNTIME_ENV_KEYS,
    _split_csv,
)
from .stores import (
    _actor_from_dict,
    _actor_to_dict,
    _atomic_write_json,
    _attachment_from_dict,
    _attachment_to_dict,
    AuthorizationStore,
    _binding_from_dict,
    _binding_to_dict,
    _blocked_input_from_dict,
    _blocked_input_to_dict,
    CallbackToken,
    compact_sessions,
    DecisionResult,
    _delivery_from_dict,
    _delivery_to_dict,
    DeliveryItem,
    DurableOutbox,
    GATE_CARD_RETIRE_HORIZON_SECONDS,
    HitlRequest,
    HitlStore,
    _IDLE_LIFECYCLE_STATES,
    InboundLedger,
    InteractionContext,
    InteractionStore,
    JsonFileStateStore,
    _sent_view_stub,
    SESSION_FINAL_RETENTION_SECONDS,
    _session_from_dict,
    SESSION_IDLE_EXPIRY_SECONDS,
    _session_last_activity,
    _session_revivable_from_topic,
    SESSION_REVIVABLE_RETENTION_SECONDS,
    _session_to_dict,
    SessionRegistry,
    STALE_STATE_TEMP_SECONDS,
    StateSnapshot,
    _TRANSIENT_BINDING_KEYS,
    _writer_owner_from_dict,
    _writer_owner_to_dict,
)
from .views import (
    _model_slug_matches,
    render_view_text,
    ViewModelFactory,
)
from .process_control import (
    _c_locale_env,
    _claude_process_moved_to_another_session,
    claude_tui_current_session,
    _command_executable_basename,
    _command_is_claude_headless_sdk_process,
    _command_is_claude_tui_process,
    _command_is_codex_app_server_process,
    _command_is_codex_tui_process,
    _command_is_external_tui_process,
    _local_lstart_epochs,
    LocalProcessController,
    _LSTART_FORMAT,
    _probe_process,
    _probe_processes,
    _proc_identity_matches,
    _ProcProbe,
    SHARED_APP_SERVER_CONTROLLER,
    _terminate_ref_session_id,
    _TERMINATE_SESSION_ID_RE,
    _utc_lstart_epoch,
)
from .lark_adapter import (
    LarkBotApi,
    LarkChannelAdapter,
)
from .claude_headless import (
    _CLAUDE_LOW_RISK_TOOLS,
    _claude_tool_is_high_risk,
    ClaudeHeadlessTransport,
    _ClaudePermissionBridge,
    _compose_turn_text,
    EMPTY_TURN_NOTICE,
    EMPTY_TURN_PLACEHOLDER,
    _options_supports_field,
    _sdk_block_field,
)
from .codex_transport import (
    _codex_approval_actions,
    _codex_error_kind,
    _CODEX_ERROR_LABELS,
    _CODEX_FILE_CHANGE_PATHS_SHOWN,
    _codex_file_change_summary,
    _codex_first_answer_value,
    _codex_item_type,
    _codex_mcp_answer_value,
    _codex_mcp_elicitation_questions,
    _codex_mcp_scalar_answer,
    _codex_mcp_schema_options,
    _codex_mcp_schema_value_type,
    _codex_message_turn_id,
    _codex_native_decision_for_action,
    _codex_normalize_approval_action,
    CODEX_STARTED_TURNS_LIMIT,
    _codex_thread_id,
    _codex_tool_event,
    _CODEX_TOOL_ITEM_SPECS,
    _codex_tool_like_name,
    _codex_tool_state_from_event_name,
    _CODEX_TOOL_STATUS_STATES,
    CodexAppServerTransport,
    _CodexToolItemSpec,
    UNSAFE_SANDBOX_MESSAGE,
)
from .orchestrator import (
    AgentTransport,
    ChannelAdapter,
    _clean_session_title,
    compose_session_title,
    _context_window_limit,
    _estimate_context_tokens,
    EXTERNAL_CLAIM_SHUTDOWN_TIMEOUT_SECONDS,
    _external_claude_resume_ref,
    ExternalTuiController,
    _format_ask_answers,
    HANDOFF_CONTINUE_PROMPT,
    _is_takeover_command,
    Orchestrator,
    OutboxDispatcher,
    ROOT_CARD_EDIT_RETRY_BUDGET,
    SESSION_TITLE_MATERIAL_CHARS,
    SESSION_TITLE_MAX_CHARS,
    SESSION_TITLE_REFRESH_INTERVAL_SECONDS,
    SESSION_TITLE_ROLLING_SOURCES,
    _session_title_source_rank,
    SESSION_TITLE_SOURCE_RANKS,
    _submit_result_completes_inbound_ledger,
    _title_from_text,
    _TURN_IN_FLIGHT_STATES,
    TURN_REPLAY_DELAYS,
    _TURN_REPLAY_WATERMARK_TOLERANCE_SECONDS,
)
from .fakes import (
    FakeAgentTransport,
    FakeChannelAdapter,
    FakeExternalTuiController,
)


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
    "HANDOFF_CONTINUE_PROMPT",
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
    "TransportCapabilities",
    "TransportHandle",
    "TransportUnavailable",
    "TransientDeliveryError",
    "TurnInput",
    "ViewModelFactory",
    "WriterOwner",
    "render_view_text",
]
