"""Channel-native WalkCode V3 runtime."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import random
import re
import shlex
import shutil
import secrets
import struct
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from .channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    AgentTransport,
    AuthorizationStore,
    BlockedReason,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelAdapter,
    ChannelConfigError,
    ChannelEndpointConfig,
    ChannelNativeE2EGates,
    ChannelNativeConfig,
    ClaudeHeadlessTransport,
    CodexAppServerTransport,
    ControlResult,
    DurableOutbox,
    InboundLedger,
    JsonFileStateStore,
    LarkBotApi,
    LarkChannelAdapter,
    LaunchSpec,
    LocalProcessController,
    Orchestrator,
    _command_executable_basename,
    _command_is_claude_headless_sdk_process,
    _command_is_claude_tui_process,
    _command_is_codex_app_server_process,
    _command_is_codex_tui_process,
    _c_locale_env,
    _command_is_external_tui_process,
    _log_degrade,
    _probe_process,
    _proc_identity_matches,
    _ps_lstart_command,
    OutboxDispatcher,
    ResumeSpec,
    SessionRegistry,
    SessionRole,
    StateSnapshot,
    SubmitResult,
    TelegramBotApi,
    TelegramChannelAdapter,
    TransportCapabilities,
    TransportHandle,
    TransportUnavailable,
    TurnInput,
    ViewModelFactory,
    WriterOwner,
    _agent_to_transport_kind,
    _external_claude_resume_ref,
    _session_is_channel_revival_candidate,
    _session_is_external_tui_takeover_candidate,
)
from .channel_native import claude_gate
from .channel_native.claude_daemon import (
    ClaudeDaemonTransport,
    claude_daemon_short_from_resume_ref,
    claude_daemon_short_id,
)
from .channel_native.lark_live import AckRegistry, LarkIngressBridge, build_lark_live_api


TELEGRAM_FORUM_TOPIC_ICON_COLORS = (0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F)
CLAUDE_DAEMON_WATCH_INTERVAL_SECONDS = 5.0
CLAUDE_DAEMON_UNAVAILABLE_RETRY_SECONDS = 30.0
# List-fallback adoption (ADR 0048): a wild job must have existed this long
# before it is registered, so the runtime's own daemon-native spawn always
# wins the race and registers its session (with the user's chat binding) first.
# MUST exceed the spawner's own worst-case register latency — the daemon-native
# spawn holds a job in the daemon list (with its real createdAt) for up to
# SPAWN_BG_READY_TIMEOUT_SECONDS before it registers under the ingress lock; a
# threshold below that window lets the watcher adopt the runtime's own in-flight
# job, which the spawner then kills as a duplicate (ADR 0048 review finding).
CLAUDE_DAEMON_ADOPT_MIN_AGE_SECONDS = 60.0
# Bounded wait for the spawn-time observer attach handshake before the first
# turn is submitted (ADR 0048 round-2): long enough for a local unix-socket
# attach, short enough not to stall the ingress path if the daemon is wedged.
CLAUDE_DAEMON_OBSERVER_READY_TIMEOUT_SECONDS = 3.0
CLAUDE_GATE_DRAIN_INTERVAL_SECONDS = 1.0
# A pending gate request that cannot be routed to an observed session (or
# whose card cannot be delivered) is answered "pass" after this grace, so the
# blocking hook falls back to the native terminal flow instead of waiting out
# the full deny timeout.
CLAUDE_GATE_UNROUTABLE_GRACE_SECONDS = 10.0
# Pending files whose hook process must be gone (deadline long past) get
# reaped by the drain loop.
CLAUDE_GATE_REAP_SLACK_SECONDS = 60.0
# Hot-path budget for the gate hook's "is this session a daemon job?" probe:
# one `has` round trip on the local unix socket. Anything slower degrades to
# the blocking (v2) path, so a daemon blip costs UX, never correctness.
CLAUDE_GATE_DAEMON_PROBE_TIMEOUT_SECONDS = 0.5
# A notify pending only becomes a card once the native dialog actually shows
# (needs match). Auto-approved calls (safe read-only Bash etc.) never render
# one — after this grace the pending is dropped silently instead of leaving a
# dangling live card (live-E2E finding). Sized for dialog render latency
# behind model thinking (live-observed up to ~9s).
CLAUDE_GATE_NOTIFY_DIALOG_GRACE_SECONDS = 30.0
TUI_HOOK_DRAIN_TIMEOUT_SECONDS = 30.0
TUI_HOOK_DRAIN_BATCH_SIZE = 25
TUI_HOOK_RECENT_PRIORITY_WINDOW_SECONDS = 300.0
TUI_HOOK_DRAIN_INTERVAL_SECONDS = 1.0
OUTBOX_FLUSH_INTERVAL_SECONDS = 1.0
TUI_BINDING_REFRESH_INTERVAL_SECONDS = 5.0
CODEX_TUI_REQUIRED_HOOKS = (
    "SessionStart",
    "UserPromptSubmit",
    "MessageDisplay",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PermissionRequest",
    "Stop",
)


class CodexStdioAppServerClient:
    def __init__(
        self,
        *,
        command: tuple[str, ...] = ("codex", "app-server", "--stdio"),
        request_timeout: float = 30.0,
        event_timeout: float = 180.0,
        event_idle_timeout: float = 2.0,
        codex_home: str = "",
    ):
        self.command = command
        self.request_timeout = request_timeout
        self.event_timeout = event_timeout
        self.event_idle_timeout = event_idle_timeout
        self.codex_home = codex_home
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._buffered_notifications: list[dict[str, Any]] = []

    def _subprocess_env(self) -> dict[str, str] | None:
        # CODEX_HOME pins the profile's auth/config/daemon state; None keeps
        # plain environment inheritance for the no-profile setup.
        if not self.codex_home:
            return None
        return {**os.environ, "CODEX_HOME": self.codex_home}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await self._ensure_started()
            request_id = self._next_id
            self._next_id += 1
            await self._send({"id": request_id, "method": method, "params": params})
            response = await self._read_response(request_id, timeout=self.request_timeout)
            result = response.get("result", {})
            return result if isinstance(result, dict) else {"value": result}

    async def events(self, thread_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            await self._ensure_started()
            collected = self._take_buffered(thread_id)
            if _contains_codex_turn_completed(collected, thread_id) or _contains_codex_hitl_server_request(
                collected,
                thread_id,
            ):
                return collected
            deadline = time.monotonic() + self.event_timeout
            while time.monotonic() < deadline:
                timeout = min(self.event_idle_timeout, max(0.0, deadline - time.monotonic()))
                try:
                    message = await self._read_message(timeout=timeout)
                except TimeoutError:
                    continue
                if _is_response_message(message):
                    self._buffered_notifications.append(message)
                    continue
                if _is_codex_hitl_server_request_message(message):
                    if _notification_matches_thread(message, thread_id):
                        collected.append(message)
                        return collected
                    self._buffered_notifications.append(message)
                    continue
                if not _is_notification_message(message):
                    continue
                if _notification_matches_thread(message, thread_id):
                    collected.append(message)
                    if _notification_method(message) == "turn/completed":
                        return collected
                else:
                    self._buffered_notifications.append(message)
            return collected

    async def answer_request(self, request_id: str, result: dict[str, Any]) -> None:
        async with self._lock:
            await self._ensure_started()
            await self._send({"id": request_id, "result": result})

    async def _ensure_started(self) -> None:
        if self._process is not None and self._process.returncode is None:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        await self._send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "walkcode", "version": "channel-native-v3"},
                    "capabilities": {},
                },
            }
        )
        await self._read_response(0, timeout=self.request_timeout)

    async def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await process.stdin.drain()

    async def _read_response(self, request_id: int, *, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = await self._read_message(timeout=max(0.0, deadline - time.monotonic()))
            if _is_response_message(message) and message.get("id") == request_id:
                return message
            if _is_error_message(message) and message.get("id") == request_id:
                error = message.get("error", {})
                if isinstance(error, dict):
                    raise TransportUnavailable(str(error.get("message", "Codex app-server request failed")))
                raise TransportUnavailable("Codex app-server request failed")
            if _is_notification_message(message) or _is_codex_hitl_server_request_message(message):
                self._buffered_notifications.append(message)
        raise TransportUnavailable("Codex app-server request timed out")

    async def _read_message(self, *, timeout: float) -> dict[str, Any]:
        process = self._require_process()
        assert process.stdout is not None
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError from exc
        if not line:
            stderr = ""
            if process.stderr is not None:
                try:
                    data = await asyncio.wait_for(process.stderr.read(), timeout=0.1)
                    stderr = data.decode("utf-8", errors="replace").strip()
                except Exception:
                    stderr = ""
            reason = stderr or f"Codex app-server exited with code {process.returncode}"
            raise TransportUnavailable(reason)
        try:
            message = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TransportUnavailable("Codex app-server returned invalid JSON") from exc
        if not isinstance(message, dict):
            raise TransportUnavailable("Codex app-server returned non-object JSON")
        return message

    def _take_buffered(self, thread_id: str) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        taken: list[dict[str, Any]] = []
        for message in self._buffered_notifications:
            if _notification_matches_thread(message, thread_id):
                taken.append(message)
            else:
                kept.append(message)
        self._buffered_notifications = kept
        return taken

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise TransportUnavailable("Codex app-server is not started")
        return self._process


class CodexManagedAppServerClient(CodexStdioAppServerClient):
    _WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(
        self,
        *,
        socket_path: str = "",
        daemon_command: tuple[str, ...] = ("codex", "app-server", "daemon", "start"),
        request_timeout: float = 30.0,
        event_timeout: float = 180.0,
        event_idle_timeout: float = 2.0,
        codex_home: str = "",
    ):
        super().__init__(
            command=("codex", "app-server", "daemon"),
            request_timeout=request_timeout,
            event_timeout=event_timeout,
            event_idle_timeout=event_idle_timeout,
            codex_home=codex_home,
        )
        self.socket_path = socket_path or str(
            _codex_home_path(codex_home) / "app-server-control" / "app-server-control.sock"
        )
        self.daemon_command = daemon_command
        self._daemon_checked = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def _ensure_started(self) -> None:
        if not self._daemon_checked:
            await self._start_daemon()
            self._daemon_checked = True
        if self._writer is not None and not self._writer.is_closing():
            return
        await self._connect_websocket()
        await self._send(
            {
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "walkcode", "version": "channel-native-v3"},
                    "capabilities": {},
                },
            }
        )
        await self._read_response(0, timeout=self.request_timeout)
        await self._send({"method": "initialized", "params": {}})

    async def _start_daemon(self) -> None:
        process = await asyncio.create_subprocess_exec(
            *self.daemon_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
            raise TransportUnavailable(detail or "failed to start Codex app-server daemon")

    async def _connect_websocket(self) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as exc:
            raise TransportUnavailable(f"Codex app-server socket unavailable: {self.socket_path}") from exc
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        try:
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.request_timeout)
        except Exception as exc:
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable("Codex app-server websocket handshake timed out") from exc
        header = response.decode("iso-8859-1", errors="replace")
        if not header.startswith("HTTP/1.1 101") and not header.startswith("HTTP/1.0 101"):
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable(f"Codex app-server websocket handshake failed: {header.splitlines()[0] if header else 'empty response'}")
        expected_accept = base64.b64encode(
            hashlib.sha1((key + self._WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in header:
            writer.close()
            await _wait_closed_safely(writer)
            raise TransportUnavailable("Codex app-server websocket handshake returned invalid accept key")
        self._reader = reader
        self._writer = writer

    async def _send(self, message: dict[str, Any]) -> None:
        writer = self._require_writer()
        writer.write(_websocket_text_frame(json.dumps(message)))
        await writer.drain()

    async def _read_message(self, *, timeout: float) -> dict[str, Any]:
        reader = self._require_reader()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                opcode, payload = await asyncio.wait_for(_read_websocket_frame(reader), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError from exc
            except Exception as exc:
                raise TransportUnavailable("Codex app-server websocket read failed") from exc
            if opcode == 0x8:
                self._close_websocket()
                raise TransportUnavailable("Codex app-server websocket closed")
            if opcode == 0x9:
                writer = self._require_writer()
                writer.write(_websocket_control_frame(0xA, payload))
                await writer.drain()
                continue
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise TransportUnavailable("Codex app-server returned invalid JSON") from exc
            if not isinstance(message, dict):
                raise TransportUnavailable("Codex app-server returned non-object JSON")
            return message
        raise TimeoutError

    def _require_reader(self) -> asyncio.StreamReader:
        if self._reader is None:
            raise TransportUnavailable("Codex app-server websocket is not connected")
        return self._reader

    def _require_writer(self) -> asyncio.StreamWriter:
        if self._writer is None or self._writer.is_closing():
            raise TransportUnavailable("Codex app-server websocket is not connected")
        return self._writer

    def _close_websocket(self) -> None:
        if self._writer is not None:
            self._writer.close()
        self._reader = None
        self._writer = None


async def _wait_closed_safely(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except Exception:
        return


async def _read_websocket_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    payload = await reader.readexactly(length) if length else b""
    if masked:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def _websocket_text_frame(text: str) -> bytes:
    return _websocket_frame(0x1, text.encode("utf-8"), masked=True)


def _websocket_control_frame(opcode: int, payload: bytes = b"") -> bytes:
    return _websocket_frame(opcode, payload, masked=True)


def _websocket_frame(opcode: int, payload: bytes, *, masked: bool) -> bytes:
    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if masked else 0
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not masked:
        header.extend(payload)
        return bytes(header)
    mask = secrets.token_bytes(4)
    header.extend(mask)
    header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header)


class _UnavailableTransport:
    def __init__(self, kind: str, reason: str):
        self.kind = kind
        self.reason = reason

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            structured_input=False,
            structured_output=False,
            permission_callback=False,
            ask_user_question=False,
            interrupt=False,
            set_model=False,
            set_permission_mode=False,
            checkpoint_rewind=False,
            resume_after_complete=False,
            resume_active_turn=False,
            multi_client_observe=False,
            multi_client_write=False,
            external_tui_takeover=False,
        )

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        raise TransportUnavailable(self.reason)

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        raise TransportUnavailable(self.reason)

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        raise TransportUnavailable(self.reason)

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        raise CapabilityUnsupported(self.reason)

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        raise CapabilityUnsupported(self.reason)

    async def interrupt(self, handle: TransportHandle, reason: str) -> ControlResult:
        return ControlResult(False, self.reason)

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        return ControlResult(False, self.reason)

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        return ControlResult(False, self.reason)

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult:
        return ControlResult(False, self.reason)

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult:
        return ControlResult(False, self.reason)

    def events(self, handle: TransportHandle) -> list[Any]:
        return []


def _is_response_message(message: dict[str, Any]) -> bool:
    return "result" in message and "id" in message


def _is_error_message(message: dict[str, Any]) -> bool:
    return "error" in message and "id" in message


def _is_notification_message(message: dict[str, Any]) -> bool:
    if "method" in message and "id" not in message:
        return True
    return message.get("type") == "event_msg" and isinstance(message.get("payload"), dict)


_CODEX_HITL_SERVER_REQUEST_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "item/permissions/requestApproval",
    "item/tool/requestUserInput",
    "mcpServer/elicitation/request",
}


def _is_codex_hitl_server_request_message(message: dict[str, Any]) -> bool:
    return (
        "id" in message
        and "method" in message
        and "result" not in message
        and "error" not in message
        and str(message.get("method", "")) in _CODEX_HITL_SERVER_REQUEST_METHODS
    )


def _notification_method(message: dict[str, Any]) -> str:
    method = str(message.get("method", ""))
    if method:
        return method
    if message.get("type") == "event_msg":
        payload = message.get("payload", {})
        if isinstance(payload, dict):
            event_type = str(payload.get("type", "") or "")
            if event_type == "task_complete":
                return "turn/completed"
            return f"event_msg/{event_type}" if event_type else "event_msg"
    return ""


def _notification_thread_id(message: dict[str, Any]) -> str:
    params = message.get("params", {})
    if isinstance(params, dict):
        return str(params.get("threadId", "") or params.get("thread_id", "") or "")
    payload = message.get("payload", {})
    if isinstance(payload, dict):
        return str(payload.get("threadId", "") or payload.get("thread_id", "") or "")
    return ""


def _notification_matches_thread(message: dict[str, Any], thread_id: str) -> bool:
    notification_thread_id = _notification_thread_id(message)
    return not notification_thread_id or notification_thread_id == thread_id


def _contains_codex_turn_completed(messages: list[dict[str, Any]], thread_id: str) -> bool:
    return any(
        _notification_method(message) == "turn/completed"
        and _notification_matches_thread(message, thread_id)
        for message in messages
    )


def _contains_codex_hitl_server_request(messages: list[dict[str, Any]], thread_id: str) -> bool:
    return any(
        _is_codex_hitl_server_request_message(message)
        and _notification_matches_thread(message, thread_id)
        for message in messages
    )


class ChannelNativeRuntime:
    def __init__(
        self,
        *,
        config: ChannelNativeConfig,
        channels: dict[str, ChannelAdapter],
        transports: dict[str, AgentTransport],
        state_store: JsonFileStateStore,
        state: StateSnapshot,
        orchestrator: Orchestrator,
        outbox_dispatcher: OutboxDispatcher,
        e2e_gates: dict[str, dict[str, Any]] | None = None,
        now=time.time,
    ):
        self.config = config
        self.channels = channels
        self.transports = transports
        self.state_store = state_store
        self.state = state
        self.orchestrator = orchestrator
        self.outbox_dispatcher = outbox_dispatcher
        self.e2e_gates = e2e_gates or {}
        self._now = now
        self._telegram_offset: int | None = None
        self.last_telegram_offset_confirm_error = ""
        self.last_telegram_poll_error = ""
        self.last_lark_event_error = ""
        self._telegram_commands_installed = False
        self._tui_hook_queue_dir = _tui_hook_queue_dir(self.state_store.path)
        self._ingress_lock = asyncio.Lock()
        self._drain_lock = asyncio.Lock()
        # ADR 0055: per-session transcript read cursor (path, byte offset,
        # (st_dev, st_ino)) for mirroring mid-turn narration; first sight
        # fast-forwards to the hook's capture boundary so history is never
        # replayed into the channel. LRU-capped at 512 sessions.
        self._tui_transcript_cursors: dict[str, tuple[Any, ...]] = {}
        self._loaded_tui_observed_bindings_refreshed = False
        # PreToolUse gate bookkeeping (ADR 0046 v2): rid -> dispatch time for
        # pending requests already turned into cards, and per-session tools
        # the user chose "always allow" for (in-memory: hooks cannot persist
        # permission rules, so the scope is this runtime process).
        self._gate_dispatched: dict[str, float] = {}
        self._gate_always_allow: set[tuple[str, str]] = set()
        daemon_transport = self._claude_daemon_transport()
        if daemon_transport is not None:
            daemon_transport.on_gate_decision = self._record_gate_decision
            # Daemon-native spawn (ADR 0048): channel-born sessions start as
            # daemon bg workers when WALKCODE_CLAUDE_SPAWN_MODE=daemon. The
            # orchestrator calls this before start_session; None falls back.
            self.orchestrator.daemon_spawner = self._spawn_claude_daemon_native_session

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        telegram_api: TelegramBotApi | None = None,
        lark_api: LarkBotApi | None = None,
        transports: dict[str, AgentTransport] | None = None,
        now=time.time,
    ) -> "ChannelNativeRuntime":
        source = _load_native_env(env)
        return cls.from_config(
            ChannelNativeConfig.from_env(source),
            telegram_api=telegram_api,
            lark_api=lark_api,
            transports=transports,
            e2e_gates=_describe_e2e_gates(ChannelNativeE2EGates.from_env(source)),
            now=now,
        )

    @classmethod
    def from_config(
        cls,
        config: ChannelNativeConfig,
        *,
        telegram_api: TelegramBotApi | None = None,
        lark_api: LarkBotApi | None = None,
        transports: dict[str, AgentTransport] | None = None,
        external_tui_controllers: dict[str, Any] | None = None,
        e2e_gates: dict[str, dict[str, Any]] | None = None,
        now=time.time,
    ) -> "ChannelNativeRuntime":
        state_store = JsonFileStateStore(config.state_path, now=now)
        state = _load_or_create_state(state_store, now=now)
        channels = _build_channels(config, telegram_api=telegram_api, lark_api=lark_api)
        transport_map = transports or _build_transports(config)
        save_state = lambda: state_store.save(state)
        outbox_dispatcher = OutboxDispatcher(
            state.outbox,
            channels,
            owner=f"runtime:{config.channel.kind}:{config.agent}",
            on_state_changed=save_state,
        )
        orchestrator = Orchestrator(
            sessions=state.sessions,
            interactions=state.interactions,
            outbox=state.outbox,
            authz=state.authz,
            hitls=state.hitls,
            inbound_ledger=state.inbound_ledger,
            channels=channels,
            transports=transport_map,
            external_tui_controllers=external_tui_controllers or _build_external_tui_controllers(),
            outbox_dispatcher=outbox_dispatcher,
            on_state_changed=save_state,
            handoff_continue=config.handoff_continue,
            now=now,
        )
        return cls(
            config=config,
            channels=channels,
            transports=transport_map,
            state_store=state_store,
            state=state,
            orchestrator=orchestrator,
            outbox_dispatcher=outbox_dispatcher,
            e2e_gates=e2e_gates or _describe_e2e_gates(ChannelNativeE2EGates.from_env({})),
            now=now,
        )

    def describe(self) -> dict[str, Any]:
        agent_status = self._describe_agent(self.config.agent)
        codex_home = str(self.config.agent_options.get("codex", {}).get("codex_home", "") or "")
        return {
            "profile": self.config.profile,
            "channel": self._describe_channel(self.config.channel.kind, self.config.channel),
            "agent": self.config.agent,
            "agent_status": agent_status,
            "runtime_status": self._describe_runtime_status(),
            "tui_hook_status": _describe_tui_hook_status(self.config.agent, codex_home),
            "claude_daemon": self._describe_claude_daemon(),
            "handoff_continue": self.config.handoff_continue,
            "state_path": self.config.state_path,
            "cwd": self.config.cwd,
            "e2e_gates": self.e2e_gates,
        }

    def _describe_claude_daemon(self) -> dict[str, Any]:
        transport = self._claude_daemon_transport()
        if transport is None:
            return {
                "enabled": False,
                "reason": "daemon_mode is off or the agent is not claude",
            }
        socket_path = transport.client.socket_path
        options = self.config.agent_options.get("claude", {})
        return {
            "enabled": True,
            "socket_path": socket_path,
            "socket_present": os.path.exists(socket_path),
            "config_dir": transport.config_dir,
            # Rollout visibility (ADR 0048): daemon transport enabled does not
            # by itself mean new sessions are daemon-born (spawn_mode can be
            # headless), so surface the actual spawn/adoption policy for
            # status/doctor. The config parser resolves the default (headless
            # since ADR 0050; daemon is an explicit opt-in); the fallback
            # below only guards states that never went through the parser.
            "spawn_mode": str(options.get("spawn_mode", "") or "headless"),
            "list_adopt": str(options.get("list_adopt", "") or "auto"),
            "daemon_spawner_installed": self.orchestrator.daemon_spawner is not None,
        }

    def _describe_runtime_status(self) -> dict[str, Any]:
        label = _launchd_service_label(
            self.config.channel.kind, self.config.agent, self.config.profile
        )
        if not label:
            return {
                "service_label": "",
                "service_loaded": False,
                "service_state": "not_applicable",
            }
        try:
            result = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except FileNotFoundError:
            return {
                "service_label": label,
                "service_loaded": False,
                "service_state": "launchctl_unavailable",
            }
        except Exception as exc:
            return {
                "service_label": label,
                "service_loaded": False,
                "service_state": "check_failed",
                "reason": f"{type(exc).__name__}: {exc}",
            }
        loaded = result.returncode == 0
        return {
            "service_label": label,
            "service_loaded": loaded,
            "service_state": "loaded" if loaded else "not_loaded",
            "stdout": result.stdout.strip()[:400],
            "stderr": result.stderr.strip()[:400],
        }

    async def diagnose_lark_ingress(self) -> dict[str, Any]:
        channel = self.channels.get("lark")
        if not isinstance(channel, LarkChannelAdapter):
            raise ChannelConfigError("Lark channel is not configured for channel-native runtime")
        endpoint = self.config.channel
        report: dict[str, Any] = {
            "channel": self._describe_channel("lark", endpoint),
            "allowed_chat_ids": [
                str(item) for item in endpoint.options.get("allowed_chat_ids", ()) if item
            ],
            "allowed_open_ids": [
                str(item) for item in endpoint.options.get("allowed_open_ids", ()) if item
            ],
        }
        report["tenant_token"] = await asyncio.to_thread(
            _lark_tenant_token_self_check,
            str(endpoint.credentials.get("app_id", "") or ""),
            str(endpoint.credentials.get("app_secret", "") or ""),
            str(endpoint.options.get("openapi_domain", "") or "https://open.feishu.cn"),
        )
        try:
            import lark_oapi  # noqa: F401

            report["sdk"] = {"installed": True}
        except ImportError:
            report["sdk"] = {
                "installed": False,
                "hint": "reinstall walkcode with the lark extra (uv tool install walkcode --with lark-oapi)",
            }
        return report

    async def diagnose_telegram_ingress(self, *, limit: int = 5) -> dict[str, Any]:
        channel = self.channels.get("telegram")
        if not isinstance(channel, TelegramChannelAdapter):
            raise ChannelConfigError("Telegram channel is not configured for channel-native runtime")
        endpoint = self.config.channel
        allowed = tuple(str(item) for item in endpoint.options.get("allowed_chat_ids", ()) if item)
        known_chats = {
            summary.chat_id
            for summary in self.state.sessions.list_sessions(channel_kind="telegram")
            if summary.chat_id
        }
        report: dict[str, Any] = {
            "channel": {
                "kind": "telegram",
                "polling_enabled": bool(endpoint.options.get("polling", True)),
                "allowlist_configured": bool(allowed),
                "allowlist_count": len(allowed),
                "allowlist_matches_existing_session": bool(set(allowed) & known_chats),
            },
            "state": {
                "existing_session_chats": len(known_chats),
            },
            "safe_to_run_serve_once": True,
            "warnings": [],
            "note": "diagnostic getUpdates does not advance Telegram offset",
        }
        report["bot"] = await self._diagnose_telegram_bot(channel)
        report["target_chat"] = await self._diagnose_telegram_target_chat(
            channel,
            allowed=allowed,
            bot=report["bot"],
        )
        report["webhook"] = await self._diagnose_telegram_webhook(channel)
        try:
            updates = await self._peek_telegram_updates(channel, limit=limit)
        except Exception as exc:
            report["safe_to_run_serve_once"] = False
            report["pending_updates"] = {
                "count": 0,
                "limit": limit,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
                "items": [],
            }
            report["warnings"].append("could not inspect Telegram pending updates")
            return report

        disallowed = 0
        blocked = 0
        items = []
        for index, update in enumerate(updates):
            item = self._summarize_telegram_update(
                channel,
                update,
                index=index,
                known_chats=known_chats,
            )
            if item.get("chat_id_present") and not item.get("chat_allowed"):
                disallowed += 1
            if item.get("submit_would_accept") is False:
                blocked += 1
            items.append(item)
        report["pending_updates"] = {
            "count": len(updates),
            "limit": limit,
            "items": items,
        }
        if disallowed:
            report["safe_to_run_serve_once"] = False
            report["warnings"].append(
                "pending update(s) are outside Telegram allowlist; serve --once would confirm their offsets without starting an agent turn"
            )
        if blocked:
            report["safe_to_run_serve_once"] = False
            report["warnings"].append(
                "pending update(s) target a session that is not currently submittable; serve --once would confirm their offsets without submitting to an agent"
            )
        return report

    async def process_telegram_update(self, update: dict[str, Any]) -> SubmitResult:
        channel = self.channels.get("telegram")
        if not isinstance(channel, TelegramChannelAdapter):
            raise ChannelConfigError("Telegram channel is not configured for channel-native runtime")
        inbound = channel.parse_update(update)
        if not self._telegram_chat_allowed(inbound.chat_id):
            return SubmitResult(False, BlockedReason.UNAUTHORIZED)
        service_kind = _telegram_service_message_kind(inbound)
        if service_kind:
            return SubmitResult(True, f"telegram_service_message:{service_kind}")
        if not inbound.callback and not _telegram_message_is_empty(inbound):
            await self._ack_telegram_received(channel, inbound)
        command = _telegram_bot_command(inbound)
        if command:
            result = await self._handle_telegram_bot_command(channel, inbound, command)
            self.save_state()
            return result
        selector = _agent_selector_command(inbound)
        if selector:
            await channel.send_view(
                ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id=inbound.thread_id,
                    root_message_id=inbound.root_message_id or inbound.message_id,
                ),
                {
                    "type": "agent_selector_rejected",
                    "message": _agent_selector_rejected_message(
                        configured_agent=self.config.agent,
                        requested_agent=selector[0],
                    ),
                },
            )
            self.save_state()
            return SubmitResult(True, "agent_selector_rejected")
        unknown_slash = _telegram_unknown_slash_command(inbound)
        if unknown_slash and self._resolve_telegram_command_session(inbound) is None:
            await channel.send_view(
                ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id=inbound.thread_id,
                    root_message_id=inbound.root_message_id or inbound.message_id,
                ),
                {
                    "type": "text",
                    "text": (
                        "Unknown slash command. Use agent-native slash commands inside an existing "
                        "session topic or reply chain."
                    ),
                },
            )
            self.save_state()
            return SubmitResult(True, "telegram_unknown_slash_command")
        if unknown_slash:
            inbound = replace(inbound, text=_telegram_agent_command_text(self.config.agent, inbound.text))
        if _telegram_message_is_empty(inbound):
            return _ignore_empty_inbound(inbound)
        transport_kind = self.config.agent_transport_kind
        inbound = await self._place_telegram_new_session(channel, inbound)
        await self._send_telegram_processing_action(channel, inbound)
        result = await self.orchestrator.handle_inbound_event(
            inbound,
            agent_transport_kind=transport_kind,
            cwd=self.config.cwd,
        )
        self.save_state()
        return result

    async def _handle_telegram_bot_command(
        self,
        channel: TelegramChannelAdapter,
        inbound,
        command: tuple[str, str],
    ) -> SubmitResult:
        name, argument = command
        actor = ActorRef(inbound.channel_kind, inbound.sender_id, inbound.sender_display)
        session = self._resolve_telegram_command_session(inbound)
        if name == "takeover":
            return await self.orchestrator.handle_inbound_event(
                inbound,
                agent_transport_kind=self.config.agent_transport_kind,
                cwd=self.config.cwd,
            )
        if name == "status":
            if session is not None:
                view = self.orchestrator.check_session_health(
                    session.session_id,
                    progress_timeout=0,
                ).view_model
                view["actions"] = self.orchestrator._status_card_actions(session)
            else:
                view = self._telegram_runtime_status_view()
            await channel.send_view(self._telegram_command_reply_binding(inbound, session), view)
            return SubmitResult(True, "telegram_bot_command")
        if name == "sessions":
            sessions = self.state.sessions.list_sessions(
                channel_kind=inbound.channel_kind,
                account_id=inbound.account_id,
                chat_id=inbound.chat_id,
            )
            view = ViewModelFactory.session_chooser(
                reason="active_sessions",
                sessions=[item for item in sessions if item.status != "stopped"],
            )
            await channel.send_view(self._telegram_command_reply_binding(inbound, session), view)
            return SubmitResult(True, "telegram_bot_command")
        if name == "skills":
            await channel.send_view(
                self._telegram_command_reply_binding(inbound, session),
                {
                    "type": "text",
                    "text": (
                        "Skills are not introspectable through the current "
                        f"{self.config.agent} structured transport yet.\n"
                        "WalkCode-native commands: /status, /sessions, /model, /takeover."
                    ),
                },
            )
            return SubmitResult(True, "telegram_bot_command")
        if name == "commands":
            await channel.send_view(
                self._telegram_command_reply_binding(inbound, session),
                {
                    "type": "text",
                    "text": _telegram_commands_help_text(self.config.agent),
                },
            )
            return SubmitResult(True, "telegram_bot_command")
        if name == "model":
            await self._handle_telegram_model_command(channel, inbound, session, actor, argument)
            return SubmitResult(True, "telegram_bot_command")
        if name == "repo":
            return await self._handle_repo_command(channel, inbound, session, argument)
        return SubmitResult(False, BlockedReason.NOT_FOUND)

    async def _handle_repo_command(self, channel, inbound, session, argument: str) -> SubmitResult:
        binding = self._telegram_command_reply_binding(inbound, session)
        roots = self.config.workspace_roots
        if session is not None:
            await channel.send_view(
                binding,
                {
                    "type": "text",
                    "text": "这个话题已经绑定了会话，工作目录不能再改。/repo 只能用于发起新任务。",
                },
            )
            return SubmitResult(True, "repo_command_rejected")
        if not roots:
            await channel.send_view(
                binding,
                {
                    "type": "text",
                    "text": (
                        "未配置 WALKCODE_WORKSPACE_ROOTS，/repo 不可用。"
                        f"\n默认工作目录：{self.config.cwd}"
                    ),
                },
            )
            return SubmitResult(True, "repo_command_rejected")
        target, _sep, task_text = argument.partition(" ")
        task_text = task_text.strip()
        if not target:
            await channel.send_view(
                binding,
                {
                    "type": "text",
                    "text": _repo_usage_text(roots),
                },
            )
            return SubmitResult(True, "repo_command_usage")
        resolved, reason = _resolve_workspace_target(target, roots)
        if resolved is None:
            await channel.send_view(
                binding,
                {"type": "text", "text": f"目录不可用：{reason}\n\n{_repo_usage_text(roots)}"},
            )
            return SubmitResult(True, "repo_command_rejected")
        if not task_text:
            await channel.send_view(
                binding,
                {
                    "type": "text",
                    "text": f"目录 OK：{resolved}\n用法：/repo {target} <任务描述>（目录和任务要在同一条消息里）",
                },
            )
            return SubmitResult(True, "repo_command_usage")
        task_inbound = replace(inbound, text=task_text)
        if inbound.channel_kind == "lark":
            task_inbound = await self._place_lark_new_session(channel, task_inbound)
        result = await self.orchestrator.handle_inbound_event(
            task_inbound,
            agent_transport_kind=self.config.agent_transport_kind,
            cwd=resolved,
        )
        self.save_state()
        return result

    def _resolve_telegram_command_session(self, inbound):
        resolution = self.state.sessions.resolve_active_binding(inbound.binding_key())
        if resolution.session_id and not resolution.reason:
            return self.state.sessions.get(resolution.session_id)
        return None

    def _telegram_command_reply_binding(self, inbound, session=None) -> ChannelBinding:
        if session is not None and session.channel_binding is not None:
            return session.channel_binding
        return ChannelBinding(
            channel_kind=inbound.channel_kind,
            account_id=inbound.account_id,
            chat_id=inbound.chat_id,
            thread_id=inbound.thread_id,
            root_message_id=inbound.root_message_id or inbound.message_id,
        )

    def _telegram_runtime_status_view(self) -> dict[str, Any]:
        active = [
            item
            for item in self.state.sessions.list_sessions(channel_kind=self.config.channel_kind)
            if item.status != "stopped"
        ]
        return {
            "type": "text",
            "text": (
                f"WalkCode bot: {self.config.agent}\n"
                f"Active sessions: {len(active)}\n"
                f"Transport: {self.config.agent_transport_kind}\n"
                f"Cwd: {self.config.cwd}"
            ),
        }

    async def _handle_telegram_model_command(
        self,
        channel: TelegramChannelAdapter,
        inbound,
        session,
        actor: ActorRef,
        argument: str,
    ) -> None:
        binding = self._telegram_command_reply_binding(inbound, session)
        if session is None:
            await channel.send_view(
                binding,
                {"type": "text", "text": "Use /model inside a session topic, or reply to a session root message."},
            )
            return
        transport = self.transports.get(session.transport_kind)
        caps = transport.capabilities() if transport is not None else None
        if not argument:
            available = bool(caps and caps.set_model)
            inventory = _local_model_inventory(self.config, session.transport_kind)
            models = inventory.get("models") or []
            if available and models:
                ctx = self.orchestrator.interactions.register_model_choice(
                    session_id=session.session_id,
                    generation=session.generation,
                    models=models,
                    # Prefer the session's live model (from assistant events /
                    # a prior switch) over the static settings-file default.
                    current=str(session.model or inventory.get("current", "") or ""),
                )
                view = ViewModelFactory(self.orchestrator.interactions).model_choice(ctx)
                await channel.send_view(binding, view)
                return
            await channel.send_view(
                binding,
                {
                    "type": "text",
                    "text": _telegram_model_status_text(
                        transport_kind=session.transport_kind,
                        switching_available=available,
                        inventory=inventory,
                    ),
                },
            )
            return
        if caps is None or not caps.set_model:
            await channel.send_view(
                binding,
                {"type": "text", "text": "Model switching is not available in this transport yet."},
            )
            return
        result = await self.orchestrator.set_session_model(
            session.session_id,
            actor=actor,
            model=argument,
        )
        if result.accepted:
            await channel.send_view(binding, {"type": "text", "text": f"Model set: {argument}"})
            return
        await channel.send_view(
            binding,
            {"type": "text", "text": f"Model switch failed: {result.reason}"},
        )

    async def _send_telegram_processing_action(self, channel: TelegramChannelAdapter, inbound) -> None:
        send_action = getattr(channel, "send_action", None)
        if send_action is None:
            return
        try:
            await send_action(
                ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id=inbound.thread_id,
                    root_message_id=inbound.root_message_id or inbound.message_id,
                ),
                "typing",
            )
        except Exception:
            return

    async def _ack_telegram_received(self, channel: TelegramChannelAdapter, inbound) -> None:
        react_to_message = getattr(channel, "react_to_message", None)
        if react_to_message is None:
            return
        try:
            await react_to_message(
                ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id=inbound.thread_id,
                    root_message_id=inbound.root_message_id or inbound.message_id,
                ),
                inbound.message_id,
                "✅",
            )
        except Exception:
            return

    def _lark_chat_allowed(self, chat_id: str, *, is_callback: bool = False) -> bool:
        endpoint = self.config.channel
        if endpoint.kind != "lark":
            return False
        if is_callback and not chat_id:
            # Card callbacks may omit the chat context; the callback token
            # (single-use, TTL, generation-checked) is the real gate there.
            return True
        allowed = tuple(str(item) for item in endpoint.options.get("allowed_chat_ids", ()) if item)
        if not allowed:
            return True
        return str(chat_id) in allowed

    def _lark_sender_allowed(self, sender_id: str) -> bool:
        allowed = tuple(
            str(item)
            for item in self.config.channel.options.get("allowed_open_ids", ())
            if item
        )
        if not allowed:
            return True
        return str(sender_id) in allowed

    async def process_lark_event(self, payload: dict[str, Any]) -> SubmitResult:
        channel = self.channels.get("lark")
        if not isinstance(channel, LarkChannelAdapter):
            raise ChannelConfigError("Lark channel is not configured for channel-native runtime")
        inbound = channel.parse_event(payload)
        if not self._lark_chat_allowed(inbound.chat_id, is_callback=inbound.callback is not None):
            return SubmitResult(False, BlockedReason.UNAUTHORIZED)
        # Feishu re-pushes un-acked events with the same event_id (e.g. when
        # the WS connection dies before the ack goes out). The orchestrator's
        # ledger check inside handle_inbound_event runs too late for this
        # handler: the new-session root card, the echo reply and the command
        # branches below all fire first, so a redelivery would leave a zombie
        # topic card stuck at "starting". Drop known event_ids before any
        # side effect. "lark:" is the degenerate id of a payload without an
        # event_id — never treat those as duplicates of each other.
        ledger = self.state.inbound_ledger
        if (
            ledger is not None
            and inbound.event_id != "lark:"
            and ledger.seen(inbound.event_id)
        ):
            from .channel_native import _log_degrade

            # Dropped silently toward the user (a duplicate needs no reply),
            # but never silently toward the operator: redeliveries are the
            # symptom of the WS drop/ack loss this dedup exists for.
            _log_degrade(
                "lark_duplicate_inbound_dropped",
                event_id=inbound.event_id,
                chat_id=inbound.chat_id,
                message_id=inbound.message_id,
                is_callback=inbound.callback is not None,
            )
            return SubmitResult(False, BlockedReason.DUPLICATE_INBOUND)
        if inbound.callback is None:
            if not self._lark_sender_allowed(inbound.sender_id):
                return SubmitResult(False, BlockedReason.UNAUTHORIZED)
            reply_binding = ChannelBinding(
                channel_kind=inbound.channel_kind,
                account_id=inbound.account_id,
                chat_id=inbound.chat_id,
                thread_id=inbound.thread_id,
                root_message_id=inbound.root_message_id or inbound.message_id,
            )
            if str(inbound.text or "").lstrip().startswith("//"):
                # Escape hatch for agent-native commands shadowed by WalkCode
                # ones: //model reaches the agent as /model. Claude executes
                # slash commands natively on the headless channel.
                raw_text = str(inbound.text or "")
                stripped = raw_text.lstrip()
                inbound = replace(
                    inbound,
                    text=raw_text[: len(raw_text) - len(stripped)] + stripped[1:],
                )
                if _telegram_message_is_empty(inbound):
                    return _ignore_empty_inbound(inbound)
                inbound = await self._place_lark_new_session(channel, inbound)
                result = await self.orchestrator.handle_inbound_event(
                    inbound,
                    agent_transport_kind=self.config.agent_transport_kind,
                    cwd=self.config.cwd,
                )
                self.save_state()
                return result
            command = _telegram_bot_command(inbound)
            if command:
                result = await self._handle_telegram_bot_command(channel, inbound, command)
                self._complete_lark_local_inbound(inbound)
                self.save_state()
                return result
            selector = _agent_selector_command(inbound)
            if selector:
                await channel.send_view(
                    reply_binding,
                    {
                        "type": "agent_selector_rejected",
                        "message": _agent_selector_rejected_message(
                            configured_agent=self.config.agent,
                            requested_agent=selector[0],
                        ),
                    },
                )
                self._complete_lark_local_inbound(inbound)
                self.save_state()
                return SubmitResult(True, "agent_selector_rejected")
            unknown_slash = _telegram_unknown_slash_command(inbound)
            if unknown_slash and self._resolve_telegram_command_session(inbound) is None:
                await channel.send_view(
                    reply_binding,
                    {
                        "type": "text",
                        "text": (
                            "未知的斜杠命令。agent 原生斜杠命令请在已有会话话题里发送。"
                        ),
                    },
                )
                self._complete_lark_local_inbound(inbound)
                self.save_state()
                return SubmitResult(True, "lark_unknown_slash_command")
            if unknown_slash:
                inbound = replace(
                    inbound, text=_telegram_agent_command_text(self.config.agent, inbound.text)
                )
            if _telegram_message_is_empty(inbound):
                return _ignore_empty_inbound(inbound)
            inbound = await self._place_lark_new_session(channel, inbound)
        result = await self.orchestrator.handle_inbound_event(
            inbound,
            agent_transport_kind=self.config.agent_transport_kind,
            cwd=self.config.cwd,
        )
        self.save_state()
        # Authorized senders whose message is rejected must not be left
        # guessing (readonly/ambiguous cases already send their own cards;
        # allowlist rejections above stay deliberately silent).
        if not result.accepted and inbound.callback is None:
            note = _LARK_REJECTION_NOTES.get(str(result.reason or ""), "")
            if note:
                try:
                    await channel.send_view(
                        ChannelBinding(
                            channel_kind=inbound.channel_kind,
                            account_id=inbound.account_id,
                            chat_id=inbound.chat_id,
                            thread_id=inbound.thread_id,
                            root_message_id=inbound.root_message_id or inbound.message_id,
                        ),
                        {"type": "text", "text": note},
                    )
                except Exception as exc:
                    from .channel_native import _log_degrade

                    _log_degrade(
                        "lark_rejection_note_send_failed",
                        reason=str(result.reason or ""),
                        chat_id=inbound.chat_id,
                        message_id=inbound.message_id,
                        sender_id=inbound.sender_id,
                        error=f"{type(exc).__name__}: {exc}",
                    )
        return result

    def _complete_lark_local_inbound(self, inbound) -> None:
        """Record a locally-terminal Lark event in the inbound ledger.

        Command/selector/unknown-slash branches reply and return without ever
        reaching the orchestrator's start/complete lifecycle, so a Feishu
        redelivery of the same event_id would re-run the side effect (the
        early seen() check only knows events somebody recorded). record() is
        used deliberately: a failed local reply should not be retried by a
        redelivery — the user resends the command instead.
        """
        ledger = self.state.inbound_ledger
        if ledger is not None and inbound.event_id != "lark:":
            ledger.record(inbound.event_id)

    async def _place_lark_new_session(self, channel, inbound):
        """Root new Lark sessions at a bot-sent status card (V2 UX parity).

        The card becomes the thread root and is registered as the session's
        status card, so lifecycle refreshes patch the root card in place. The
        user's original message stays in the chat; its text is forwarded into
        the thread as the first reply.
        """
        if inbound.callback is not None:
            return inbound
        if inbound.root_message_id and inbound.root_message_id != inbound.message_id:
            return inbound
        resolution = self.state.sessions.resolve_active_binding(inbound.binding_key())
        if resolution.session_id or resolution.reason:
            return inbound
        text = str(inbound.text or "").strip()
        title = text.splitlines()[0][:40] if text else "新任务"
        try:
            card_id = await channel.send_view(
                ChannelBinding(
                    channel_kind=inbound.channel_kind,
                    account_id=inbound.account_id,
                    chat_id=inbound.chat_id,
                    thread_id="",
                    root_message_id="",
                ),
                {
                    "type": "health",
                    "status": "running",
                    "title": title,
                    "session_id": "",
                    "transport": self.config.agent_transport_kind,
                    "elapsed": 0.0,
                    "cwd": self.config.cwd,
                    "last_progress_event": "starting",
                },
            )
        except Exception as exc:
            print(
                f"lark session root card failed; falling back to message-rooted thread: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return inbound
        if not card_id:
            return inbound
        thread_binding = ChannelBinding(
            channel_kind=inbound.channel_kind,
            account_id=inbound.account_id,
            chat_id=inbound.chat_id,
            thread_id=str(card_id),
            root_message_id=str(card_id),
        )
        try:
            await channel.send_view(thread_binding, {"type": "text", "text": f"👤 {text}"})
        except Exception:
            pass
        raw = inbound.raw if isinstance(inbound.raw, dict) else {}
        raw["_walkcode_status_card_id"] = str(card_id)
        return replace(
            inbound,
            thread_id=str(card_id),
            root_message_id=str(card_id),
            raw=raw,
        )

    async def serve_lark_ws(
        self,
        *,
        retry_delay: float = 2.0,
        max_events: int | None = None,
        bridge_factory=None,
    ) -> None:
        channel = self.channels.get("lark")
        if not isinstance(channel, LarkChannelAdapter):
            raise ChannelConfigError("Lark channel is not configured for channel-native runtime")
        queue: asyncio.Queue = asyncio.Queue()
        ack_registry = getattr(channel.api, "ack_registry", None) or AckRegistry()
        loop = asyncio.get_running_loop()
        if bridge_factory is None:
            bridge = LarkIngressBridge(
                self.config.channel.credentials,
                self.config.channel.options,
                loop=loop,
                queue=queue,
                ack_registry=ack_registry,
            )
        else:
            bridge = bridge_factory(loop=loop, queue=queue, ack_registry=ack_registry)
        # Settle zombies from the previous process BEFORE ingress starts: the
        # first inbound message must not race the sweep into a dead worker
        # handle, and the sweep must never see sessions this process created.
        await self._settle_orphan_headless_sessions_once()
        bridge.start()
        previous_defer_event_drain = self.orchestrator.defer_event_drain
        self.orchestrator.defer_event_drain = True
        maintenance_tasks = self._start_telegram_maintenance_tasks()
        processed = 0
        try:
            while max_events is None or processed < max_events:
                payload = await queue.get()
                processed += 1
                try:
                    async with self._ingress_lock:
                        await self.process_lark_event(payload)
                except ChannelConfigError:
                    raise
                except Exception as exc:
                    self.last_lark_event_error = f"{type(exc).__name__}: {exc}"
                    print(
                        f"lark event transient error: {self.last_lark_event_error}",
                        file=sys.stderr,
                    )
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                else:
                    self.last_lark_event_error = ""
        finally:
            await self._stop_telegram_maintenance_tasks(maintenance_tasks)
            self.orchestrator.defer_event_drain = previous_defer_event_drain

    async def _place_telegram_new_session(
        self,
        channel: TelegramChannelAdapter,
        inbound,
    ):
        if inbound.callback or inbound.thread_id or inbound.root_message_id:
            return inbound
        resolution = self.state.sessions.resolve_active_binding(inbound.binding_key())
        if resolution.session_id or resolution.reason:
            return inbound
        topic_id = await self._create_telegram_session_topic_if_possible(channel, inbound)
        if not topic_id:
            return inbound
        await self._send_telegram_general_topic_created_notice(channel, inbound, topic_id=topic_id)
        return replace(inbound, thread_id=topic_id)

    async def _create_telegram_session_topic_if_possible(
        self,
        channel: TelegramChannelAdapter,
        inbound,
    ) -> str:
        message = inbound.raw.get("message", {}) if isinstance(inbound.raw, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        chat_type = str(chat.get("type", "") or "")
        return await self._create_telegram_topic_for_chat_if_possible(
            channel,
            chat_id=inbound.chat_id,
            chat_type=chat_type,
            topic_name=_telegram_session_topic_name(self.config.agent, inbound.text),
        )

    async def _create_telegram_topic_for_chat_if_possible(
        self,
        channel: TelegramChannelAdapter,
        *,
        chat_id: str,
        chat_type: str = "",
        topic_name: str,
    ) -> str:
        if chat_type == "supergroup":
            try:
                chat_result = await channel.api.call("getChat", {"chat_id": chat_id})
            except Exception:
                return ""
            chat_info = chat_result.get("result", {}) if isinstance(chat_result, dict) else {}
            if not bool(chat_info.get("is_forum")):
                return ""
            if not await self._telegram_bot_can_manage_topics(channel, chat_id):
                return ""
            return await self._create_telegram_forum_topic(
                channel,
                chat_id=chat_id,
                topic_name=topic_name,
            )
        if not chat_type:
            try:
                chat_result = await channel.api.call("getChat", {"chat_id": chat_id})
            except Exception:
                return ""
            chat_info = chat_result.get("result", {}) if isinstance(chat_result, dict) else {}
            return await self._create_telegram_topic_for_chat_if_possible(
                channel,
                chat_id=chat_id,
                chat_type=str(chat_info.get("type", "") or ""),
                topic_name=topic_name,
            )
        if chat_type == "private":
            try:
                bot_result = await channel.api.call("getMe", {})
            except Exception:
                return ""
            bot = bot_result.get("result", {}) if isinstance(bot_result, dict) else {}
            if not bool(bot.get("has_topics_enabled")):
                return ""
            return await self._create_telegram_forum_topic(
                channel,
                chat_id=chat_id,
                topic_name=topic_name,
            )
        return ""

    async def _telegram_bot_can_manage_topics(
        self,
        channel: TelegramChannelAdapter,
        chat_id: str,
    ) -> bool:
        try:
            bot_result = await channel.api.call("getMe", {})
        except Exception:
            return False
        bot = bot_result.get("result", {}) if isinstance(bot_result, dict) else {}
        bot_id = bot.get("id")
        if not bot_id:
            return False
        try:
            member_result = await channel.api.call("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
        except Exception:
            return False
        member = member_result.get("result", {}) if isinstance(member_result, dict) else {}
        if member.get("status") == "creator":
            return True
        return member.get("status") == "administrator" and bool(member.get("can_manage_topics"))

    async def _create_telegram_forum_topic(
        self,
        channel: TelegramChannelAdapter,
        *,
        chat_id: str,
        topic_name: str,
    ) -> str:
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "name": topic_name,
            }
            payload.update(await self._random_telegram_topic_icon(channel))
            result = await channel.api.call(
                "createForumTopic",
                payload,
            )
        except Exception:
            return ""
        topic = result.get("result", {}) if isinstance(result, dict) else {}
        return str(topic.get("message_thread_id", "") or "")

    async def _random_telegram_topic_icon(self, channel: TelegramChannelAdapter) -> dict[str, Any]:
        try:
            result = await channel.api.call("getForumTopicIconStickers", {})
        except Exception:
            result = {}
        stickers = result.get("result", []) if isinstance(result, dict) else []
        custom_emoji_ids = [
            str(item.get("custom_emoji_id", "") or "")
            for item in stickers
            if isinstance(item, dict) and item.get("custom_emoji_id")
        ]
        if custom_emoji_ids:
            return {"icon_custom_emoji_id": random.SystemRandom().choice(custom_emoji_ids)}
        return {"icon_color": random.SystemRandom().choice(TELEGRAM_FORUM_TOPIC_ICON_COLORS)}

    async def _send_telegram_general_topic_created_notice(
        self,
        channel: TelegramChannelAdapter,
        inbound,
        *,
        topic_id: str,
    ) -> None:
        message = inbound.raw.get("message", {}) if isinstance(inbound.raw, dict) else {}
        chat = message.get("chat", {}) if isinstance(message, dict) else {}
        if str(chat.get("type", "") or "") != "supergroup":
            return
        topic_name = _telegram_session_topic_name(self.config.agent, inbound.text)
        text = (
            f"已创建 session topic：{topic_name}\n"
            "请在新 topic 内继续这个任务；General 会保留原始启动消息作为记录。"
        )
        payload: dict[str, Any] = {
            "chat_id": inbound.chat_id,
            "text": text,
            "disable_notification": True,
        }
        if inbound.message_id:
            try:
                payload["reply_parameters"] = {"message_id": int(inbound.message_id)}
            except (TypeError, ValueError):
                pass
        topic_url = _telegram_topic_url(inbound.chat_id, topic_id)
        if topic_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Open topic", "url": topic_url}]]
            }
        try:
            await channel.api.call("sendMessage", payload)
        except Exception:
            return

    async def poll_telegram_once(self, *, timeout: int = 30, limit: int = 25) -> int:
        channel = self.channels.get("telegram")
        if not isinstance(channel, TelegramChannelAdapter):
            raise ChannelConfigError("Telegram channel is not configured for channel-native runtime")
        # Startup barrier for the --once path too (idempotent per process).
        await self._settle_orphan_headless_sessions_once()
        if not self.config.channel.options.get("polling", True):
            raise ChannelConfigError("Telegram polling is disabled; webhook ingress is not wired yet")
        payload: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._telegram_offset is not None:
            payload["offset"] = self._telegram_offset
        result = await channel.api.call("getUpdates", payload)
        updates = result.get("result", []) if isinstance(result, dict) else []
        processed = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            async with self._ingress_lock:
                result = await self.process_telegram_update(update)
            update_id = _telegram_update_id(update)
            if update_id is not None and _telegram_result_confirms_offset(result):
                next_offset = update_id + 1
                self._telegram_offset = max(self._telegram_offset or next_offset, next_offset)
            elif update_id is not None:
                break
            if result.accepted:
                processed += 1
        if self._telegram_offset is not None and updates:
            await self._confirm_telegram_offset(channel)
        return processed

    async def serve_telegram_polling(
        self,
        *,
        timeout: int = 30,
        limit: int = 25,
        retry_delay: float = 2.0,
        max_iterations: int | None = None,
    ) -> None:
        iterations = 0
        # Same startup barrier as serve_lark_ws: settle previous-process
        # zombies before any polling can route messages into dead handles.
        await self._settle_orphan_headless_sessions_once()
        previous_defer_event_drain = self.orchestrator.defer_event_drain
        self.orchestrator.defer_event_drain = True
        maintenance_tasks = self._start_telegram_maintenance_tasks()
        try:
            await asyncio.sleep(0)
            while max_iterations is None or iterations < max_iterations:
                iterations += 1
                try:
                    await self.poll_telegram_once(timeout=timeout, limit=limit)
                    await self._ensure_telegram_bot_commands()
                except ChannelConfigError:
                    raise
                except Exception as exc:
                    self.last_telegram_poll_error = f"{type(exc).__name__}: {exc}"
                    print(f"telegram polling transient error: {self.last_telegram_poll_error}", file=sys.stderr)
                    if retry_delay > 0:
                        await asyncio.sleep(retry_delay)
                else:
                    self.last_telegram_poll_error = ""
        finally:
            await self._stop_telegram_maintenance_tasks(maintenance_tasks)
            self.orchestrator.defer_event_drain = previous_defer_event_drain

    def _start_telegram_maintenance_tasks(self) -> list[asyncio.Task[None]]:
        return [
            asyncio.create_task(
                self._flush_outbox_forever(interval=OUTBOX_FLUSH_INTERVAL_SECONDS),
                name="walkcode-outbox-flush",
            ),
            asyncio.create_task(
                self._drain_deferred_tui_hooks_forever(interval=TUI_HOOK_DRAIN_INTERVAL_SECONDS),
                name="walkcode-tui-hook-drain",
            ),
            asyncio.create_task(
                self._refresh_loaded_tui_observed_bindings_forever(
                    interval=TUI_BINDING_REFRESH_INTERVAL_SECONDS
                ),
                name="walkcode-tui-binding-refresh",
            ),
            *(
                [
                    asyncio.create_task(
                        self._watch_claude_daemon_forever(),
                        name="walkcode-claude-daemon-watch",
                    ),
                    asyncio.create_task(
                        self._drain_claude_gate_requests_forever(),
                        name="walkcode-claude-gate-drain",
                    ),
                ]
                if self._claude_daemon_transport() is not None
                else []
            ),
        ]

    @staticmethod
    async def _stop_telegram_maintenance_tasks(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _settle_orphan_headless_sessions_once(self) -> None:
        # claude_headless workers are child processes of THIS runtime: the SDK
        # client, its CLI subprocess, and any in-flight can_use_tool prompt all
        # died with the previous process. A session still marked running after
        # a restart is a zombie — its cards look live but clicks can never
        # reach a worker. Settle them so the topic shows the truth and stale
        # cards get retired by the callback-failure path instead of hanging.
        # Guarded to one run per process so every ingress entry point (lark
        # ws, telegram polling, --once) can call it without re-sweeping
        # sessions this process created.
        if getattr(self, "_orphan_sweep_done", False):
            return
        self._orphan_sweep_done = True
        try:
            async with self._ingress_lock:
                settled = 0
                for session in self.state.sessions.iter_sessions():
                    if session.transport_kind != "claude_headless":
                        continue
                    if session.status == "stopped":
                        continue
                    if not (session.writer_owner and session.writer_owner.kind == "orchestrator"):
                        continue
                    # Only in-flight sessions are unrecoverable: their turn and
                    # any blocked can_use_tool Future died with the previous
                    # process, and active turns can't be resumed. IDLE (and
                    # error-recoverable) sessions stay — the resume path
                    # revives them on the next inbound message.
                    if session.lifecycle_state not in {
                        "ACTIVE",
                        "WAITING_PERMISSION",
                        "WAITING_USER",
                        "INTERRUPTED",
                    }:
                        if session.background_tasks:
                            # ADR 0052: an IDLE session can carry a background
                            # task ledger — but those subagents lived inside
                            # the previous process's worker and died with it.
                            # Clear the ledger or the status card shows
                            # phantom "background running" forever.
                            session.background_tasks = []
                            session.last_progress_at = self._now()
                            session.last_progress_event = "background.abandoned_on_restart"
                            try:
                                await self.orchestrator.refresh_session_status_card(session)
                            except Exception:
                                pass
                        continue
                    session.status = "stopped"
                    session.lifecycle_state = "STOPPED"
                    session.stop_reason = "runtime_restart"
                    session.writer_lease = None
                    session.writer_owner = WriterOwner(kind="none")
                    session.background_tasks = []
                    session.last_progress_at = self._now()
                    session.last_progress_event = "orchestrator.runtime_restart_settled"
                    settled += 1
                    try:
                        await self.orchestrator.refresh_session_status_card(session)
                    except Exception:
                        pass
                if settled:
                    self.save_state()
                    print(
                        f"settled {settled} orphan claude_headless session(s) from a previous runtime",
                        file=sys.stderr,
                    )
        except Exception as exc:
            print(
                f"orphan headless sweep failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    async def _best_effort_flush_outbox(self) -> None:
        try:
            await asyncio.wait_for(
                self.outbox_dispatcher.flush_once(),
                timeout=5.0,
            )
            self.save_state()
        except Exception as exc:
            print(f"outbox flush deferred: {type(exc).__name__}: {exc}", file=sys.stderr)

    async def _best_effort_refresh_loaded_tui_observed_bindings(self) -> None:
        try:
            # Cold start may pay tenant-token fetch + one lark patch per live
            # session; 2s cancelled the first pass mid-flight (dropping the
            # takeover re-grants) and the marker prevented any retry.
            await asyncio.wait_for(self._refresh_loaded_tui_observed_bindings(), timeout=15.0)
        except Exception as exc:
            print(f"TUI observed binding refresh deferred: {type(exc).__name__}: {exc}", file=sys.stderr)

    async def _best_effort_drain_deferred_tui_hooks(self) -> None:
        try:
            await asyncio.wait_for(
                self.drain_deferred_tui_hooks(limit=TUI_HOOK_DRAIN_BATCH_SIZE),
                timeout=TUI_HOOK_DRAIN_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            print(f"deferred TUI hook drain deferred: {type(exc).__name__}: {exc}", file=sys.stderr)

    async def _flush_outbox_forever(self, *, interval: float = OUTBOX_FLUSH_INTERVAL_SECONDS) -> None:
        while True:
            await self._best_effort_flush_outbox()
            await asyncio.sleep(interval)

    async def _refresh_loaded_tui_observed_bindings_forever(
        self,
        *,
        interval: float = TUI_BINDING_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        while True:
            await self._best_effort_refresh_loaded_tui_observed_bindings()
            await asyncio.sleep(interval)

    async def _ensure_telegram_bot_commands(self) -> None:
        if self._telegram_commands_installed:
            return
        channel = self.channels.get("telegram")
        if not isinstance(channel, TelegramChannelAdapter):
            return
        set_bot_commands = getattr(channel, "set_bot_commands", None)
        if set_bot_commands is None:
            return
        commands = _telegram_native_command_menu(self.config.agent)
        try:
            await set_bot_commands(commands)
        except Exception:
            return
        self._telegram_commands_installed = True

    async def process_tui_hook(
        self,
        *,
        hook_type: str,
        payload: dict[str, Any],
        agent: str = "",
    ) -> SubmitResult:
        # Belt-and-suspenders capture stamp: the CLI hook entry stamps this at
        # ingress and the deferred drain backfills it from the queue entry's
        # created_at, so by here every real hook already carries a truthful
        # stamp. Defaulting to "now" only ever applies to a live in-process
        # caller that skipped the CLI entry — treating "first seen now" as the
        # capture time is correct for those and keeps the freshness gate from
        # mis-reading them as unknown-age. Deferred replays are already stamped,
        # so setdefault never overrides them into looking fresh.
        if isinstance(payload, dict):
            payload.setdefault("_walkcode_hook_captured_at", time.time())
            # In-process callers that skipped the CLI entry get a "now"
            # boundary — correct for them, same as the capture stamp above.
            _stamp_transcript_size(payload)
        hook_type = _normalize_tui_hook_type(hook_type or _payload_hook_event_name(payload))
        if not hook_type:
            self.save_state()
            return SubmitResult(True, "missing_hook_type")
        if not _tui_hook_observes_session(hook_type):
            self.save_state()
            return SubmitResult(True, "non_observation_hook")
        agent_name = _normalize_tui_agent(agent or str(payload.get("agent", "") or ""))
        if not agent_name:
            agent_name = self.config.agent
        transport_kind = _agent_to_transport_kind(agent_name)
        resume_ref = _tui_resume_ref(transport_kind, payload)
        if not resume_ref:
            self.save_state()
            return SubmitResult(True, "missing_resume_ref")
        if _tui_hook_is_walkcode_headless_transport(transport_kind, payload):
            self.save_state()
            return SubmitResult(True, "internal_headless_hook_ignored")
        if (
            _tui_hook_can_claim_existing_session(hook_type)
            and self._tui_hook_is_unverified_walkcode_owned_session_hook(transport_kind, resume_ref, payload)
        ):
            self.save_state()
            return SubmitResult(True, "internal_headless_hook_ignored")

        event_id = _tui_event_id(hook_type, transport_kind, resume_ref, payload)
        ledger_started = False
        if self.state.inbound_ledger is not None and not self.state.inbound_ledger.start(event_id):
            return SubmitResult(True, BlockedReason.DUPLICATE_INBOUND)
        ledger_started = self.state.inbound_ledger is not None
        try:
            session = await self._claim_or_create_tui_observed_session(
                hook_type=hook_type,
                agent_name=agent_name,
                transport_kind=transport_kind,
                resume_ref=resume_ref,
                payload=payload,
            )
            if session is None:
                if ledger_started:
                    self.state.inbound_ledger.complete(event_id)
                self.save_state()
                return SubmitResult(True, "unobserved_tui_hook")
            if session.status != "stopped":
                # Backfill model + context usage for TUI-observed sessions
                # (status card shows "模型: — 上下文: —" otherwise); re-read on
                # turn stop so a mid-session /model switch and the growing
                # context land eventually without per-event file IO.
                if (
                    not session.model
                    or not session.last_usage
                    or hook_type == "stop"
                    or _tui_hook_stops_session(hook_type)
                ):
                    transcript_model, transcript_usage = _transcript_meta_from_payload(payload)
                    meta_changed = False
                    if transcript_model and transcript_model != session.model:
                        session.model = transcript_model
                        meta_changed = True
                    if transcript_usage and transcript_usage != session.last_usage:
                        session.last_usage = dict(transcript_usage)
                        meta_changed = True
                    if meta_changed:
                        # A session created by a no-text hook (sync /
                        # session-start) already sent its first status card
                        # with empty 模型/上下文, and _send_tui_hook_output
                        # returns before refreshing for those hooks — patch
                        # the card now instead of waiting for an unrelated
                        # later event.
                        await self.orchestrator.refresh_session_status_card(session)
                await self._send_tui_hook_output(session, hook_type=hook_type, payload=payload)
        except Exception:
            if ledger_started:
                self.state.inbound_ledger.fail(event_id)
            raise
        if session.status != "stopped" and _tui_hook_stops_session(hook_type):
            if await self._claude_daemon_session_alive(session):
                # Daemon-native session: the TUI process exiting is a detach
                # (the worker keeps running); the session ends on the daemon's
                # settled event, not here.
                session.last_progress_at = self._now()
                session.last_progress_event = "external_tui.tui_detached_daemon_alive"
            else:
                self._mark_tui_session_stopped(session, hook_type=hook_type)
            await self.orchestrator.refresh_session_status_card(session)
        if ledger_started:
            self.state.inbound_ledger.complete(event_id)
        self.save_state()
        return SubmitResult(True)

    def _tui_hook_is_unverified_walkcode_owned_session_hook(
        self,
        transport_kind: str,
        resume_ref: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        if _tui_hook_has_external_tui_process_identity(transport_kind, payload):
            return False
        existing_id = self.state.sessions.find_by_resume_ref(
            transport_kind=transport_kind,
            resume_ref=resume_ref,
        )
        if not existing_id:
            return False
        session = self.state.sessions.get(existing_id)
        if session.status == "stopped":
            return False
        if session.transport_kind != transport_kind:
            return False
        return bool(session.writer_owner and session.writer_owner.kind == "orchestrator")

    def defer_tui_hook(
        self,
        *,
        hook_type: str,
        payload: dict[str, Any],
        agent: str = "",
    ) -> dict[str, Any]:
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
        self._tui_hook_queue_dir.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{created_at_ns:019d}-"
            f"{os.getpid()}-{queued['id']}.json"
        )
        final_path = self._tui_hook_queue_dir / filename
        tmp_path = self._tui_hook_queue_dir / f".{filename}.tmp"
        tmp_path.write_text(json.dumps(queued, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, final_path)
        return {"queued": True, "id": queued["id"], "path": str(final_path)}

    async def drain_deferred_tui_hooks(self, *, limit: int = 100) -> int:
        async with self._drain_lock:
            return await self._drain_deferred_tui_hooks_unlocked(limit=limit)

    async def _drain_deferred_tui_hooks_unlocked(self, *, limit: int = 100) -> int:
        if not self._tui_hook_queue_dir.exists():
            return 0
        processed = 0
        for path in self._deferred_tui_hook_paths(limit=limit):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                self._archive_bad_tui_hook(path)
                continue
            if not isinstance(item, dict):
                # A JSON array/string parses fine but has no .get — archive it
                # rather than let the AttributeError below break the drain and
                # wedge the queue on retry (round-2 cluster).
                self._archive_bad_tui_hook(path)
                continue
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                self._archive_bad_tui_hook(path)
                continue
            # Backfill the capture stamp for pre-0.14.3 queued payloads (which
            # lack it) from the queue entry's own created_at, so a replayed
            # hook is correctly judged stale by age instead of dodging the
            # freshness gate as "age unknown" (deep-review cluster B). A queued
            # entry with no usable created_at is malformed — archive it rather
            # than let it reach processing stampless (where it would default to
            # "fresh now" and could re-flip ownership).
            if "_walkcode_hook_captured_at" not in payload:
                import math

                created_at = item.get("created_at")
                try:
                    created_at = float(created_at) if created_at is not None else None
                except (TypeError, ValueError):
                    created_at = None
                if created_at is None or not math.isfinite(created_at) or created_at <= 0:
                    self._archive_bad_tui_hook(path)
                    continue
                payload["_walkcode_hook_captured_at"] = created_at
            try:
                async with self._ingress_lock:
                    result = await self.process_tui_hook(
                        hook_type=str(item.get("hook_type", "") or ""),
                        agent=str(item.get("agent", "") or ""),
                        payload=payload,
                    )
            except ChannelConfigError as exc:
                # Configuration-level failures do not heal by retrying the same
                # hook against the same config; archiving avoids an endless
                # per-second retry loop on the spool.
                print(f"deferred TUI hook dropped (config): {exc}", file=sys.stderr)
                self._archive_bad_tui_hook(path)
                continue
            except Exception as exc:
                print(f"deferred TUI hook retry pending: {type(exc).__name__}: {exc}", file=sys.stderr)
                break
            if result.accepted:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                processed += 1
                continue
            print(f"deferred TUI hook dropped: {result.reason}", file=sys.stderr)
            self._archive_bad_tui_hook(path)
        return processed

    def _deferred_tui_hook_paths(self, *, limit: int) -> list[Path]:
        paths = sorted(self._tui_hook_queue_dir.glob("*.json"))
        if limit <= 0 or not paths:
            return []
        recent_cutoff = time.time() - TUI_HOOK_RECENT_PRIORITY_WINDOW_SECONDS
        recent: list[Path] = []
        older: list[Path] = []
        for path in paths:
            created_at = _deferred_tui_hook_created_at(path)
            if created_at >= recent_cutoff:
                recent.append(path)
            else:
                older.append(path)
        return [*recent, *older][:limit]

    async def _drain_deferred_tui_hooks_forever(
        self,
        *,
        interval: float = TUI_HOOK_DRAIN_INTERVAL_SECONDS,
    ) -> None:
        while True:
            await self._best_effort_drain_deferred_tui_hooks()
            await asyncio.sleep(interval)

    def _claude_daemon_transport(self) -> ClaudeDaemonTransport | None:
        transport = self.transports.get("claude_daemon")
        return transport if isinstance(transport, ClaudeDaemonTransport) else None

    async def _claude_daemon_session_alive(self, session) -> bool:
        """Is the daemon worker behind this TUI-observed session still running?

        Daemon-native sessions outlive their attach TUI (``/exit`` detaches).
        Stop paths keyed on the TUI process must not end the session while the
        worker is alive; the daemon's ``settled`` event is the authority.
        """
        transport = self._claude_daemon_transport()
        if transport is None:
            return False
        resume_ref = _external_claude_resume_ref(session)
        if not resume_ref:
            return False
        short = claude_daemon_short_from_resume_ref(resume_ref)
        if not short:
            return False
        try:
            alive = await transport.client.job_alive(short)
        except Exception:
            alive = None
        if alive is None:
            # Probe failure means "unknown", not "dead": a socket blip or a
            # restarting daemon must not let stop paths end a live session.
            # Fall back to the last observed state (settled clears the flag).
            return bool(
                isinstance(session.transport_ref, dict)
                and session.transport_ref.get("daemon_live")
            )
        return alive

    # -- daemon-native spawn + list adoption (ADR 0048) -----------------------

    async def _spawn_claude_daemon_native_session(
        self,
        binding: ChannelBinding,
        transport_kind: str,
        cwd: str,
        owner: ActorRef,
    ):
        """Create a channel-born session as a daemon bg worker.

        The session is registered external-TUI shaped — writer external_tui,
        nested claude resume_ref — so every already-verified v3 mechanism
        (daemon reply writes, subscribe watcher, hook content, dual gate)
        applies from the first turn. Returns None on any failure so the
        orchestrator falls back to the headless SDK spawn.

        Concurrency contract (ADR 0048): the sole caller is
        ``Orchestrator.handle_inbound_event`` via the ``daemon_spawner`` hook,
        which runs under ``_ingress_lock`` (held by ``serve_lark_ws`` /
        ``poll_telegram_once`` around ``process_*``). This method therefore
        MUST NOT re-acquire ``_ingress_lock`` — ``asyncio.Lock`` is not
        reentrant, so doing so self-deadlocks the whole ingress path. The
        registration below is already serialized by the caller's lock.
        """
        if transport_kind != "claude_headless":
            return None
        options = self.config.agent_options.get("claude", {})
        if str(options.get("spawn_mode", "") or "headless") != "daemon":
            return None
        transport = self._claude_daemon_transport()
        if transport is None:
            return None
        from .channel_native import _log_degrade

        headless = self.transports.get("claude_headless")
        settings = ""
        cli_path = ""
        if isinstance(headless, ClaudeHeadlessTransport):
            cli_path = str(headless.cli_path or "")
            try:
                if headless.anthropic_base_url:
                    # Same tap/base-url override file the headless SDK spawn
                    # uses — the bg worker must hit the same upstream.
                    settings = headless._anthropic_base_url_settings_override()
                elif headless.settings:
                    settings = str(headless.settings)
            except Exception as exc:
                _log_degrade(
                    "claude_daemon_spawn_settings_failed",
                    error=exc,
                    fallback="headless_spawn",
                )
                return None
        try:
            job = await transport.spawn_bg_job(cwd, settings=settings, cli_path=cli_path)
        except (TransportUnavailable, CapabilityUnsupported) as exc:
            _log_degrade(
                "claude_daemon_spawn_failed",
                error=exc,
                fallback="headless_spawn",
            )
            return None
        agent_session_id = str(job.get("session_id", "") or "")
        short = str(job.get("short", "") or "")
        nested_resume_ref = {
            "transport_kind": "claude_headless",
            "agent_session_id": agent_session_id,
        }
        external_ref = {
            "source": "walkcode_daemon_spawn",
            "agent": "claude",
            "resume_ref": nested_resume_ref,
            "daemon_short": short,
            "daemon_live": True,
        }
        writer_actor = ActorRef(
            channel_kind=self.config.channel.kind,
            actor_id=f"claude_daemon:{short}",
            display_name="claude bg worker",
        )
        # No _ingress_lock here — the caller already holds it (see contract in
        # the docstring). Any failure after spawn_bg_job succeeded must reap the
        # orphan job: otherwise a live worker is left that nobody tracks (and
        # that list adoption would later resurface as a duplicate session).
        try:
            existing = self.state.sessions.find_by_resume_ref(
                transport_kind="claude_headless",
                resume_ref={"agent_session_id": agent_session_id},
            )
            if existing:
                # A fresh uuid colliding with a known session means state is
                # inconsistent; reap the just-spawned job and go headless.
                _log_degrade(
                    "claude_daemon_spawn_duplicate_session",
                    session_id=existing,
                    fallback="headless_spawn",
                )
                await self._reap_daemon_job(transport, short, reason="spawn_duplicate_session")
                return None
            # The binding is the user's own chat/topic; this origin marker
            # blocks the hook-claim path from repainting it as a readonly
            # observation topic (_ensure_tui_observed_binding_capabilities).
            binding.capabilities["origin"] = "daemon_spawn"
            session = self.state.sessions.create_observed_session(
                session_id=self._tui_observed_session_id(
                    "claude", "claude_headless", nested_resume_ref
                ),
                binding=binding,
                cwd=str(job.get("cwd", "") or cwd),
                external_ref=external_ref,
                owner=writer_actor,
            )
            initial_title = str(binding.capabilities.get("initial_title", "") or "").strip()
            if initial_title:
                session.cached_title = initial_title
                session.title_source = "initial_user_input"
            if self.state.authz is not None:
                self.state.authz.grant(session.session_id, owner, SessionRole.OWNER)
        except Exception as exc:
            _log_degrade(
                "claude_daemon_spawn_register_failed",
                error=exc,
                fallback="headless_spawn",
            )
            await self._reap_daemon_job(transport, short, reason="spawn_register_failed")
            return None
        # Observer attach BEFORE the first turn is submitted: the daemon only
        # publishes state patches while ≥1 attacher is connected (live-E2E
        # finding 2026-07-07), and the first dialog can open right after the
        # first reply lands. Await the attach handshake (bounded) so the very
        # first permission/ask dialog is already observable — fire-and-forget
        # would race the first reply (round-2 review finding). Timeout is
        # non-fatal: the first turn still carries model latency before any
        # dialog, and the watcher sync keeps the observer alive afterwards.
        try:
            attached = await transport.ensure_observer_ready(
                short, timeout=CLAUDE_DAEMON_OBSERVER_READY_TIMEOUT_SECONDS
            )
        except Exception as exc:
            attached = False
            _log_degrade(
                "claude_daemon_observer_ready_error",
                error=exc,
                fallback="proceed_watcher_reensures",
            )
        if not attached:
            _log_degrade(
                "claude_daemon_observer_ready_timeout",
                short=short,
                fallback="proceed_watcher_reensures",
            )
        # State is persisted by the outer handle_inbound_event flow (submit ->
        # refresh_session_status_card -> on_state_changed). Calling save_state()
        # here would checkpoint the inbound ledger's in-progress mark mid-turn,
        # so a crash before completion would reject the replayed message.
        return session

    async def _reap_daemon_job(
        self, transport: "ClaudeDaemonTransport", short: str, *, reason: str
    ) -> None:
        """Best-effort kill of a half-born daemon job, with the failure logged.

        Silent suppression here (the old behavior) meant an orphan worker left
        by a failed spawn/adoption was invisible — the reap outcome must be
        observable so operators can tell "reaped" from "leaked" (ADR 0048).
        """
        if not short:
            return
        from .channel_native import _log_degrade

        transport.stop_observer(short)
        try:
            await transport.client.kill(short)
        except Exception as exc:
            _log_degrade(
                "claude_daemon_reap_failed",
                short=short,
                reason=reason,
                error=exc,
                fallback="orphan_job_may_persist",
            )

    async def _maybe_adopt_wild_claude_daemon_job(self, job: dict[str, Any]) -> str:
        """List-fallback session bootstrap (ADR 0048).

        Registers a live daemon job walkcode has never seen (hook not
        configured, spool lost, or an idle `claude --bg` that has produced no
        hook events yet) as a TUI-observed session, using the same binding
        shape hooks would create. Conservative on purpose: only CLI-born jobs
        (source=shell), only after a settle age so the runtime's own spawn
        path always registers its session (with the user's chat binding)
        first, and deduped against resume_ref under the ingress lock.
        Returns the session id ('' when not adopted).
        """
        if self.config.agent != "claude":
            return ""
        options = self.config.agent_options.get("claude", {})
        if str(options.get("list_adopt", "") or "auto") == "off":
            return ""
        if str(job.get("source", "") or "") != "shell":
            return ""
        agent_session_id = str(job.get("sessionId", "") or "")
        short = claude_daemon_short_id(job.get("short") or agent_session_id)
        if not agent_session_id or not short:
            return ""
        created_at = job.get("createdAt") or job.get("startedAt")
        try:
            created_seconds = float(created_at) / 1000.0
        except (TypeError, ValueError):
            return ""
        if self._now() - created_seconds < CLAUDE_DAEMON_ADOPT_MIN_AGE_SECONDS:
            return ""
        flat_resume_ref = {"agent_session_id": agent_session_id}
        nested_resume_ref = {
            "transport_kind": "claude_headless",
            "agent_session_id": agent_session_id,
        }
        # Unlocked pre-check before the channel side effect: if a hook (or the
        # daemon spawner) already registered this session, skip building an
        # observation binding entirely. Creating the Lark root message /
        # Telegram topic first and only then discovering the session exists
        # (under the lock below) would orphan that channel object (ADR 0048
        # review finding). The locked recheck stays authoritative for the
        # residual TOCTOU window.
        existing_pre = self.state.sessions.find_by_resume_ref(
            transport_kind="claude_headless",
            resume_ref=flat_resume_ref,
        )
        if existing_pre:
            return existing_pre
        try:
            binding = await self._create_tui_observed_binding(
                "claude", "claude_headless", flat_resume_ref, {}
            )
        except Exception as exc:
            print(
                f"claude daemon list adopt skipped ({short}): {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return ""
        external_ref = {
            "source": "claude_daemon_list",
            "agent": "claude",
            "resume_ref": nested_resume_ref,
        }
        actor = ActorRef(
            channel_kind=self.config.channel.kind,
            actor_id=f"local_tui:claude_headless:{agent_session_id}",
            display_name="claude TUI",
        )
        async with self._ingress_lock:
            existing = self.state.sessions.find_by_resume_ref(
                transport_kind="claude_headless",
                resume_ref=flat_resume_ref,
            )
            if existing:
                return existing
            session = self.state.sessions.create_observed_session(
                session_id=self._tui_observed_session_id(
                    "claude", "claude_headless", nested_resume_ref
                ),
                binding=binding,
                cwd=str(job.get("cwd", "") or self.config.cwd),
                external_ref=external_ref,
                owner=actor,
            )
            session.transport_ref["daemon_live"] = True
            session.cached_title = _telegram_session_topic_name(
                "claude", f"TUI {agent_session_id}"
            )
            session.title_source = "tui_hook"
            self._grant_tui_channel_owners(session.session_id, binding)
            self.save_state()
        await self.orchestrator.refresh_session_status_card(session)
        return session.session_id

    def _tui_observed_session_id(
        self, agent_name: str, transport_kind: str, resume_ref: dict[str, Any]
    ) -> str:
        identity = _resume_ref_identity(transport_kind, resume_ref)
        session_id = f"tui-{agent_name}-{hashlib.sha1(identity.encode()).hexdigest()[:12]}"
        base_session_id = session_id
        suffix = 1
        while True:
            try:
                self.state.sessions.get(session_id)
            except KeyError:
                return session_id
            suffix += 1
            session_id = f"{base_session_id}-{suffix}"

    # -- PreToolUse gate (ADR 0046 v2) ---------------------------------------
    #
    # Headless sessions close the permission / AskUserQuestion loop in-process
    # (SDK can_use_tool -> Future -> card -> resolve). TUI/daemon sessions run
    # the PreToolUse hook in a separate process, so the same loop runs over
    # the gate spool: the blocking hook (gate_tui_hook, hook-process side)
    # writes pending/<rid>.json and polls decisions/<rid>.json; the serve loop
    # (drain_claude_gate_requests) turns pendings into cards, and the card
    # callback writes the decision file via ClaudeDaemonTransport.

    def gate_tui_hook(
        self,
        *,
        hook_type: str,
        payload: dict[str, Any],
        agent: str = "",
    ) -> dict[str, Any] | None:
        """Blocking PreToolUse gate; returns hookSpecificOutput or None (abstain).

        Always spools the observation copy first (same as ``--defer``) so tool
        progress keeps flowing while the gate holds the tool call.
        """
        self.defer_tui_hook(hook_type=hook_type, payload=payload, agent=agent)
        normalized = _normalize_tui_hook_type(hook_type or _payload_hook_event_name(payload))
        if normalized != "pre-tool":
            return None
        agent_name = _normalize_tui_agent(agent or str(payload.get("agent", "") or ""))
        if (agent_name or self.config.agent) != "claude":
            return None
        if _tui_hook_is_walkcode_headless_transport("claude_headless", payload):
            # walkcode's own headless sessions gate in-process via can_use_tool;
            # gating here would double-prompt every tool.
            return None
        rid = str(payload.get("tool_use_id", "") or "")
        tool_name = str(payload.get("tool_name", "") or "")
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        if not rid or not tool_name:
            return None
        options = self.config.agent_options.get("claude", {})
        if str(options.get("daemon_mode", "") or "auto") == "off":
            return None
        config_dir = str(options.get("config_dir", "") or os.environ.get("CLAUDE_CONFIG_DIR", ""))
        permission_mode = str(payload.get("permission_mode", "") or "")
        gate_tools = options.get("gate_tools")
        kind = claude_gate.should_gate(
            tool_name=tool_name,
            tool_input=tool_input,
            permission_mode=permission_mode,
            allow_rules=claude_gate.profile_allow_rules(config_dir),
            gate_mode=str(options.get("gate_mode", "") or "auto"),
            gate_tools=gate_tools if isinstance(gate_tools, list) else None,
        )
        if not kind:
            return None
        state_path = self.state_store.path
        if not claude_gate.heartbeat_fresh(state_path):
            # No serve loop draining the spool: abstain so the native terminal
            # prompt flow keeps working without the walkcode service.
            claude_gate.trace("abstain_heartbeat_stale", rid=rid, tool=tool_name)
            return None
        now = time.time()
        request = {
            "rid": rid,
            "kind": kind,
            "agent": "claude",
            "transport_kind": "claude_headless",
            "session_id": str(payload.get("session_id", "") or ""),
            "resume_ref": _tui_resume_ref("claude_headless", payload),
            "tool_name": tool_name,
            "tool_input": tool_input,
            "permission_mode": permission_mode,
            "cwd": str(payload.get("cwd", "") or ""),
            "created_at": now,
            "hook_pid": os.getpid(),
        }
        # v3 routing (ADR 0046 v3): daemon jobs get the dual-surface path —
        # capture the structured tool_input, then abstain immediately so the
        # native dialog renders and both surfaces can answer. dontAsk stays on
        # the blocking path (abstain there means auto-deny: no dialog exists
        # to inject into), as do non-daemon TUI sessions (no attach plane) and
        # everything when gate_style=block (escape hatch).
        gate_style = str(options.get("gate_style", "") or "dual").strip().lower()
        if gate_style != "block" and permission_mode != "dontAsk":
            daemon_short = self._probe_claude_daemon_short(str(payload.get("session_id", "") or ""))
            if daemon_short:
                request["mode"] = claude_gate.MODE_NOTIFY
                request["daemon_short"] = daemon_short
                claude_gate.write_pending(state_path, request)
                claude_gate.trace(
                    "gate_notify", rid=rid, kind=kind, tool=tool_name, short=daemon_short
                )
                return None
        timeout = float(options.get("gate_timeout", 0) or claude_gate.DEFAULT_WAIT_TIMEOUT_SECONDS)
        request["mode"] = claude_gate.MODE_BLOCK
        request["deadline"] = now + timeout
        claude_gate.write_pending(state_path, request)
        claude_gate.trace("gate_open", rid=rid, kind=kind, tool=tool_name, timeout=int(timeout))
        started = time.monotonic()
        try:
            decision = claude_gate.wait_for_decision(state_path, rid, timeout=timeout)
        finally:
            claude_gate.cleanup_gate_files(state_path, rid)
        if decision is None:
            decision = claude_gate.timeout_decision(kind)
            claude_gate.trace(
                "gate_timeout_abstain",
                rid=rid,
                tool=tool_name,
                elapsed=int(time.monotonic() - started),
            )
        else:
            claude_gate.trace(
                "gate_decision",
                rid=rid,
                tool=tool_name,
                action=decision.get("action"),
                reason=decision.get("reason", ""),
                elapsed=int(time.monotonic() - started),
            )
        return claude_gate.pre_tool_use_output(kind, decision, tool_input)

    async def _drain_claude_gate_requests_forever(
        self,
        *,
        interval: float = CLAUDE_GATE_DRAIN_INTERVAL_SECONDS,
    ) -> None:
        while True:
            try:
                claude_gate.touch_heartbeat(self.state_store.path)
                await self.drain_claude_gate_requests()
            except Exception as exc:
                print(
                    f"claude gate drain transient error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            await asyncio.sleep(interval)

    async def drain_claude_gate_requests(self) -> int:
        state_path = self.state_store.path
        requests = claude_gate.list_pending(state_path)
        live_rids: set[str] = set()
        processed = 0
        now = time.time()
        for request in requests:
            rid = str(request.get("rid", "") or "")
            live_rids.add(rid)
            mode = claude_gate.pending_mode(request)
            deadline = float(request.get("deadline", 0) or 0)
            if deadline and now > deadline + CLAUDE_GATE_REAP_SLACK_SECONDS:
                # The hook process is gone (denied on timeout or was killed).
                claude_gate.trace("reap_expired_pending", rid=rid)
                claude_gate.cleanup_gate_files(state_path, rid)
                self._gate_dispatched.pop(rid, None)
                continue
            if rid in self._gate_dispatched:
                continue
            created_at = float(request.get("created_at", now) or now)
            if mode == claude_gate.MODE_NOTIFY:
                # The card mirrors a real dialog. Auto-approved tool calls
                # (safe read-only Bash etc.) never render one — wait for the
                # dialog, then give up silently instead of posting a card
                # with buttons that could never be delivered.
                transport = self._claude_daemon_transport()
                waiting = False
                if transport is not None:
                    try:
                        waiting = await transport.notify_dialog_waiting(request)
                    except Exception as exc:
                        claude_gate.trace(
                            "notify_dialog_probe_failed",
                            rid=rid,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        waiting = False
                if not waiting:
                    if now - created_at > CLAUDE_GATE_NOTIFY_DIALOG_GRACE_SECONDS:
                        claude_gate.trace("notify_dialog_never_rendered", rid=rid)
                        claude_gate.remove_pending(state_path, rid)
                    continue
            session_id = self._claude_gate_session_id(request)
            if not session_id:
                if now - created_at > CLAUDE_GATE_UNROUTABLE_GRACE_SECONDS:
                    claude_gate.trace("pass_session_not_observed", rid=rid, mode=mode)
                    if mode == claude_gate.MODE_NOTIFY:
                        # No hook is waiting: the native dialog is the surface.
                        claude_gate.remove_pending(state_path, rid)
                    else:
                        claude_gate.write_decision(
                            state_path, rid, {"action": "pass", "reason": "session_not_observed"}
                        )
                        self._gate_dispatched[rid] = now
                continue
            tool_name = str(request.get("tool_name", "") or "")
            if (
                str(request.get("kind", "")) == claude_gate.KIND_PERMISSION
                and (session_id, tool_name) in self._gate_always_allow
            ):
                if mode == claude_gate.MODE_NOTIFY:
                    # Single attempt, never a timed retry: a failed injection
                    # may already have written keys, and a second automatic
                    # press could confirm the WRONG dialog (review finding).
                    outcome = await self._auto_inject_gate_allow(rid, request, session_id)
                    if outcome in {"ok", "skip"}:
                        claude_gate.remove_pending(state_path, rid)
                        self._gate_dispatched[rid] = now
                        if outcome == "ok":
                            processed += 1
                        continue
                    # outcome == "card": fall through to a human card — clicks
                    # re-run the pre-injection dialog check, so they stay safe.
                else:
                    claude_gate.trace("auto_allow_session", rid=rid, tool=tool_name)
                    claude_gate.write_decision(
                        state_path, rid, {"action": "allow", "reason": "always_allow(session)"}
                    )
                    self._gate_dispatched[rid] = now
                    processed += 1
                    continue
            async with self._ingress_lock:
                posted = await self.orchestrator.post_claude_gate_prompt(session_id, request)
            if posted:
                if mode == claude_gate.MODE_NOTIFY:
                    # Card (or degraded notice) is up: the runtime owns the
                    # pending from here on (the hook abstained long ago).
                    # Degraded ask forms register too — the entry suppresses
                    # the duplicate needs notice and is reaped by the watcher
                    # when the terminal answers; unmappable answers are still
                    # rejected at injection time (keys_for_ask_answer -> None).
                    transport = self._claude_daemon_transport()
                    if transport is not None:
                        transport.register_notify_gate(rid, request, session_id=session_id)
                    claude_gate.remove_pending(state_path, rid)
                self._gate_dispatched[rid] = now
                processed += 1
                self.save_state()
            elif now - created_at > CLAUDE_GATE_UNROUTABLE_GRACE_SECONDS:
                claude_gate.trace("pass_card_not_delivered", rid=rid, mode=mode)
                if mode == claude_gate.MODE_NOTIFY:
                    claude_gate.remove_pending(state_path, rid)
                else:
                    claude_gate.write_decision(
                        state_path, rid, {"action": "pass", "reason": "card_not_delivered"}
                    )
                    self._gate_dispatched[rid] = now
        for rid in list(self._gate_dispatched):
            if rid not in live_rids:
                self._gate_dispatched.pop(rid, None)
        # Documented contract: decision files whose pending is gone (stale card
        # clicked after the hook gave up) are reaped here.
        for orphan in claude_gate.list_orphan_decision_paths(state_path):
            claude_gate.trace("reap_orphan_decision", file=orphan.name)
            try:
                orphan.unlink()
            except OSError:
                pass
        return processed

    def _claude_gate_session_id(self, request: dict[str, Any]) -> str | None:
        resume_ref = request.get("resume_ref")
        if isinstance(resume_ref, dict) and resume_ref:
            session_id = self.state.sessions.find_by_resume_ref(
                transport_kind=str(request.get("transport_kind", "") or "claude_headless"),
                resume_ref=resume_ref,
            )
            if session_id:
                return session_id
        sid = str(request.get("session_id", "") or "")
        if sid:
            return self.state.sessions.find_by_resume_ref(
                transport_kind="claude_headless",
                resume_ref={"agent_session_id": sid},
            )
        return None

    async def _auto_inject_gate_allow(
        self, rid: str, request: dict[str, Any], session_id: str
    ) -> str:
        """Session-scoped always_allow, v3 shape: press "1" on the dialog.

        The v2 memory wrote an allow decision file; with notify mode nobody
        reads decisions, so the same memory auto-injects allow-once instead.

        Returns "ok" (injected), "skip" (the terminal settled it first —
        drop the pending quietly), or "card" (injection could not be
        confirmed — hand over to a human card, never auto-retry: the keys
        may already have been written).
        """
        transport = self._claude_daemon_transport()
        if transport is None:
            return "card"
        transport.register_notify_gate(rid, request, session_id=session_id)
        handle = TransportHandle(
            handle_id=f"claude-daemon-{request.get('daemon_short', '')}",
            transport_kind="claude_daemon",
            ref={"short": str(request.get("daemon_short", "") or "")},
        )
        try:
            await transport.approve_permission(
                handle, rid, {"action": "allow", "reason": "always_allow(session)"}
            )
        except claude_gate.GateInjectionFailed as exc:
            claude_gate.trace("auto_allow_inject_missed", rid=rid, reason=exc.reason)
            if exc.reason in {"dialog_mismatch", "already_resolved", "stale_gate"}:
                return "skip"
            return "card"
        except Exception as exc:
            claude_gate.trace(
                "auto_allow_inject_error", rid=rid, error=f"{type(exc).__name__}: {exc}"
            )
            return "card"
        claude_gate.trace(
            "auto_allow_session", rid=rid, tool=request.get("tool_name", ""), mode="notify"
        )
        return "ok"

    def _record_gate_decision(self, rid: str, decision: dict[str, Any]) -> None:
        if str(decision.get("action", "") or "") != "always_allow":
            return
        # v3 notify gates embed routing info in the decision (their pending
        # file is gone by decision time); v2 block gates still read it back
        # from the pending spool.
        tool_name = str(decision.get("tool_name", "") or "")
        session_id = str(decision.get("session_id", "") or "")
        if not (session_id and tool_name):
            request = claude_gate.read_pending(self.state_store.path, rid)
            if not request:
                return
            tool_name = tool_name or str(request.get("tool_name", "") or "")
            session_id = session_id or (self._claude_gate_session_id(request) or "")
        if session_id and tool_name:
            self._gate_always_allow.add((session_id, tool_name))

    def _probe_claude_daemon_short(self, session_id: str) -> str:
        """8-hex daemon short id when this session is a live daemon job, else "".

        Runs on the gate hook's hot path in the hook process: one bounded
        ``has`` round trip through the registered daemon transport's client
        (absent transport = daemon_mode off = not a daemon route). Any
        failure or timeout returns "" and the caller stays on the blocking
        (v2) path — a daemon blip can only degrade UX, never the gate.
        """
        short = claude_daemon_short_id(session_id)
        if not short:
            return ""
        transport = self._claude_daemon_transport()
        if transport is None:
            return ""

        async def _probe() -> bool:
            return await asyncio.wait_for(
                transport.client.job_ready(short),
                timeout=CLAUDE_GATE_DAEMON_PROBE_TIMEOUT_SECONDS,
            )

        try:
            ready = asyncio.run(_probe())
        except Exception:
            return ""
        return short if ready else ""

    async def _watch_claude_daemon_forever(
        self,
        *,
        interval: float = CLAUDE_DAEMON_WATCH_INTERVAL_SECONDS,
    ) -> None:
        """Maintain one subscribe watcher per live TUI-owned Claude daemon job.

        Read half of ADR 0046: ``list`` discovers which known sessions have a
        live worker; each gets a long-lived ``subscribe`` connection whose
        ``state`` patches drive lifecycle + health cards. Content still comes
        from hooks, so this loop touches no message rendering.
        """
        transport = self._claude_daemon_transport()
        if transport is None:
            return
        watchers: dict[str, asyncio.Task[None]] = {}
        try:
            while True:
                delay = interval
                try:
                    await self._sync_claude_daemon_watchers(transport, watchers)
                except TransportUnavailable:
                    # No daemon for this profile right now (old Claude version,
                    # daemon not started, proto drift). Cheap to re-probe later.
                    delay = CLAUDE_DAEMON_UNAVAILABLE_RETRY_SECONDS
                except Exception as exc:
                    print(
                        f"claude daemon watch transient error: {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                await asyncio.sleep(delay)
        finally:
            observer_tasks = transport.stop_all_observers()
            for task in watchers.values():
                task.cancel()
            pending = [*watchers.values(), *observer_tasks]
            if pending:
                # Await observers too: cancel() only requests cancellation;
                # their attach sockets close in the CancelledError handler, so
                # the loop must not shut down before they drain (round-2
                # review finding — the old code awaited only subscribe
                # watchers and left observer tasks pending).
                await asyncio.gather(*pending, return_exceptions=True)

    async def _sync_claude_daemon_watchers(
        self,
        transport: ClaudeDaemonTransport,
        watchers: dict[str, asyncio.Task[None]],
    ) -> None:
        for short, task in list(watchers.items()):
            if task.done():
                watchers.pop(short, None)
        jobs = await transport.client.list_jobs()
        # Two passes: known jobs first so their subscribe watchers come up
        # immediately, THEN wild-job adoption (which does channel network I/O
        # to build an observation binding). Inlining adoption in a single pass
        # let one slow adoption stall subscribe creation for every known job
        # behind it in the list (ADR 0048 review finding).
        unknown_jobs: list[dict[str, Any]] = []
        for job in jobs:
            if job.get("dying") or job.get("outcome"):
                continue
            short = claude_daemon_short_id(job.get("short") or job.get("sessionId"))
            if not short:
                continue
            session_id = self.state.sessions.find_by_resume_ref(
                transport_kind="claude_headless",
                resume_ref={"agent_session_id": str(job.get("sessionId", "") or "")},
            )
            if not session_id:
                # Only try to adopt when there is no watcher yet; a job that
                # already has a watcher already has a session.
                if short not in watchers:
                    unknown_jobs.append(job)
                continue
            # Reconcile EVERY tick, not just when the subscribe watcher is
            # absent: the observer attach and the subscribe watcher are
            # independent connections, and the observer can exit on a
            # transient control-plane outage while the watcher stays alive.
            # Gating this on `short not in watchers` (the old bug) would then
            # leave a live job with zero attachers, freezing its tempo/needs
            # patches and re-blinding the notify gate. ensure_observer and the
            # watcher create are both idempotent, so re-calling is a no-op
            # when everything is already healthy.
            self._start_daemon_watcher_if_eligible(session_id, short, transport, watchers)
        for job in unknown_jobs:
            short = claude_daemon_short_id(job.get("short") or job.get("sessionId"))
            if not short or short in watchers:
                continue
            # Unknown to walkcode: hooks stay the primary creation channel
            # (clean cwd/transcript), but a live job nobody registered —
            # hook not configured, spool lost, idle `claude --bg` with no
            # first prompt yet — gets adopted from the list (ADR 0048).
            session_id = await self._maybe_adopt_wild_claude_daemon_job(job)
            if not session_id:
                continue
            self._start_daemon_watcher_if_eligible(session_id, short, transport, watchers)

    def _start_daemon_watcher_if_eligible(
        self,
        session_id: str,
        short: str,
        transport: ClaudeDaemonTransport,
        watchers: dict[str, asyncio.Task[None]],
    ) -> None:
        try:
            session = self.state.sessions.get(session_id)
        except KeyError:
            return
        if session.status == "stopped":
            return
        if not (session.writer_owner and session.writer_owner.kind == "external_tui"):
            return
        # The daemon only publishes state patches while the job has ≥1
        # attacher (live-E2E finding 2026-07-07): keep a persistent observer
        # attach alongside the subscribe watcher so dialogs on never-attached
        # (Feishu-spawned) or detached jobs still surface via needs/tempo.
        # ensure_observer rebuilds a task that exited on a transient outage
        # and no-ops when one is already running — safe to call every tick.
        transport.ensure_observer(short)
        # Idempotent: only create the subscribe watcher when absent, so the
        # every-tick observer reconciliation above does not orphan a live
        # watcher task by overwriting it.
        existing = watchers.get(short)
        if existing is not None and not existing.done():
            return
        watchers[short] = asyncio.create_task(
            self._watch_claude_daemon_job(session_id, short, transport),
            name=f"walkcode-claude-daemon-sub-{short}",
        )

    async def _watch_claude_daemon_job(
        self,
        session_id: str,
        short: str,
        transport: ClaudeDaemonTransport,
    ) -> None:
        last_needs = ""
        try:
            async for event in transport.client.subscribe(short):
                event_type = str(event.get("type", "") or "")
                if event_type == "state":
                    patch = event.get("patch")
                    if not isinstance(patch, dict):
                        continue
                    async with self._ingress_lock:
                        last_needs = await self._apply_claude_daemon_state_patch(
                            session_id, patch, last_needs, short=short
                        )
                        self.save_state()
                elif event_type == "settled":
                    async with self._ingress_lock:
                        await self._settle_claude_daemon_session(
                            session_id,
                            outcome=str(event.get("outcome", "") or ""),
                            short=short,
                        )
                        self.save_state()
                    transport.stop_observer(short)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The watcher is re-created by the discovery loop while the job is
            # alive, so a dropped subscribe connection self-heals.
            print(
                f"claude daemon subscribe ended ({short}): {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    async def _apply_claude_daemon_state_patch(
        self,
        session_id: str,
        patch: dict[str, Any],
        last_needs: str,
        *,
        short: str = "",
    ) -> str:
        try:
            session = self.state.sessions.get(session_id)
        except KeyError:
            return last_needs
        if session.status == "stopped":
            return last_needs
        if not (session.writer_owner and session.writer_owner.kind == "external_tui"):
            # Session was taken over meanwhile; the structured transport owns
            # state now and daemon patches are no longer authoritative.
            return last_needs
        session.last_progress_at = self._now()
        # A state patch only arrives from a live daemon worker: remember that
        # so stop paths (TUI detach, stale-pid sweep) and the status card can
        # tell "TUI closed" apart from "session over" (ADR 0046 v2).
        session.transport_ref["daemon_live"] = True
        tempo = str(patch.get("tempo", "") or "")
        detail = str(patch.get("detail", "") or "").strip()
        if tempo:
            session.last_progress_event = f"external_tui.daemon_{tempo}" + (
                f":{detail}" if detail else ""
            )
        changed = False
        needs = patch.get("needs")
        needs_text = str(needs or "").strip()
        # needs carries two unrelated meanings (live-verified): a real tool
        # permission gate is tempo=blocked / "approve <Tool>: <detail>", but
        # an idle worker also reports needs like "send a prompt to start".
        # Only the approve form may flip the session into WAITING_PERMISSION —
        # treating every non-empty needs as a gate is the false-orange-card
        # bug from the daemon-native rollout.
        blocking_needs = needs_text if needs_text and (
            needs_text.lower().startswith("approve ") or tempo == "blocked"
        ) else ""
        if blocking_needs:
            already_waiting = session.lifecycle_state == "WAITING_PERMISSION"
            if not already_waiting:
                session.lifecycle_state = "WAITING_PERMISSION"
                changed = True
            # The permission-request hook may have raced ahead with its own
            # notice card; only send ours on a fresh daemon-observed need.
            # v3 notify gates already produce a rich interactive card from the
            # gate drain — a second orange notice would be pure noise.
            if (
                blocking_needs != last_needs
                and not already_waiting
                and not (short and self._has_open_notify_gate(short))
            ):
                session.last_event_seq += 1
                match = re.match(r"^approve\s+([A-Za-z0-9_-]+)", blocking_needs, re.IGNORECASE)
                await self.orchestrator._send_session_view(
                    session,
                    {
                        "type": "tui_permission_notice",
                        "tool_name": match.group(1) if match else "",
                        "summary": blocking_needs,
                    },
                    idempotency_key=f"external_tui:daemon_needs:{session.last_event_seq}",
                )
            last_needs = blocking_needs
        elif needs is not None or needs_text:
            transport = self._claude_daemon_transport()
            injected = bool(
                short and transport is not None and transport.recently_injected(short)
            )
            if short and transport is not None:
                # Whoever answered, this job's open notify gates are done:
                # tombstone them so a late card click flips honestly. This
                # must NOT depend on lifecycle_state — a tool event can flip
                # WAITING_PERMISSION away before this patch arrives (review
                # finding + live-E2E observation).
                transport.resolve_notify_gates_for_short(short)
            was_waiting = session.lifecycle_state == "WAITING_PERMISSION"
            if was_waiting:
                session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
                changed = True
                # Sync the terminal-side decision back to the channel (the
                # notice/card would otherwise look pending forever) — unless
                # the clearing was our own injection: the clicked card already
                # flipped to the decision result.
                if last_needs and not injected:
                    session.last_event_seq += 1
                    await self.orchestrator._send_session_view(
                        session,
                        {"type": "text", "text": f"✅ 已在终端处理：{last_needs}"},
                        idempotency_key=f"external_tui:daemon_needs_cleared:{session.last_event_seq}",
                    )
            last_needs = ""
        if tempo or changed:
            await self.orchestrator.refresh_session_status_card(session)
        return last_needs

    async def _settle_claude_daemon_session(
        self, session_id: str, *, outcome: str, short: str = ""
    ) -> None:
        transport = self._claude_daemon_transport()
        if short and transport is not None:
            # Job is gone, so are its dialogs: retire any open notify gates.
            transport.resolve_notify_gates_for_short(short)
        try:
            session = self.state.sessions.get(session_id)
        except KeyError:
            return
        if session.status == "stopped":
            return
        if not (session.writer_owner and session.writer_owner.kind == "external_tui"):
            return
        self._mark_tui_session_stopped(
            session, hook_type=f"daemon_settled_{outcome or 'unknown'}"
        )
        await self.orchestrator.refresh_session_status_card(session)

    def _has_open_notify_gate(self, short: str, *, tool_name: str = "") -> bool:
        """Is a v3 dual-surface card open (or about to open) for this job?

        With ``tool_name`` the match narrows to that tool, so a notice for an
        unrelated native prompt is not swallowed by a gate on another tool.
        """
        transport = self._claude_daemon_transport()
        if transport is not None and transport.has_notify_gate_for_short(
            short, tool_name=tool_name
        ):
            return True
        for request in claude_gate.list_pending(self.state_store.path):
            if (
                claude_gate.pending_mode(request) == claude_gate.MODE_NOTIFY
                and str(request.get("daemon_short", "") or "") == short
                and (not tool_name or str(request.get("tool_name", "") or "") == tool_name)
            ):
                return True
        return False

    def _archive_bad_tui_hook(self, path: Path) -> None:
        try:
            bad_dir = self._tui_hook_queue_dir / "bad"
            bad_dir.mkdir(parents=True, exist_ok=True)
            os.replace(path, bad_dir / path.name)
        except FileNotFoundError:
            return
        except Exception:
            try:
                path.unlink()
            except Exception:
                return

    async def _claim_or_create_tui_observed_session(
        self,
        *,
        hook_type: str,
        agent_name: str,
        transport_kind: str,
        resume_ref: dict[str, Any],
        payload: dict[str, Any],
    ):
        external_ref = {
            "source": "native_tui_hook",
            "agent": agent_name,
            "hook_type": hook_type,
            "resume_ref": {"transport_kind": transport_kind, **dict(resume_ref)},
        }
        raw_hook_type = _payload_hook_event_name(payload)
        if raw_hook_type and _normalize_tui_hook_type(raw_hook_type) != hook_type:
            external_ref["raw_hook_type"] = raw_hook_type
        terminate_ref = _enrich_terminate_ref(_tui_terminate_ref(payload))
        if terminate_ref:
            external_ref["terminate_ref"] = terminate_ref
        actor = ActorRef(
            channel_kind=self.config.channel.kind,
            actor_id=f"local_tui:{transport_kind}:{_resume_ref_identity(transport_kind, resume_ref)}",
            display_name=f"{agent_name} TUI",
        )
        existing_id = self.state.sessions.find_by_resume_ref(
            transport_kind=transport_kind,
            resume_ref=resume_ref,
        )
        if existing_id:
            session = self.state.sessions.get(existing_id)
            if session.status == "stopped":
                # Off the event loop: the identity re-probe shells out to `ps`
                # per entry and can block for seconds during a deferred drain,
                # starving heartbeats and other channels (round-3 Concurrency).
                if not (
                    _session_is_external_tui_takeover_candidate(session)
                    or await asyncio.to_thread(_tui_hook_has_live_tui_process, transport_kind, payload)
                ):
                    # No TUI stamp on the record AND no *currently-live* TUI
                    # process behind the hook: a late/stale hook for a
                    # genuinely dead session. A command-string snapshot alone
                    # is not proof (a deferred hook replayed across a restart
                    # may describe an exited process) — the pid must still be
                    # running.
                    _log_degrade(
                        "tui_revival_refused",
                        session_id=session.session_id,
                        hook_type=hook_type,
                    )
                    return session
                # Revive. The record-stamp check alone is not enough: a
                # takeover rewrites transport_kind/transport_ref and the
                # restart sweep clears writer_owner, stripping every TUI
                # stamp — while the TUI process itself may still be alive and
                # hooking (live incident 2026-07-19: mirror went permanently
                # silent). A hook that carries a live TUI process identity is
                # sufficient proof by itself.
                session.generation += 1
                session.status = "running"
                session.stop_reason = ""
                session.transport_kind = "external_tui"
                session.transport_ref = dict(external_ref)
                session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
                session.writer_owner = WriterOwner(
                    kind="external_tui",
                    actor_id=actor.actor_id,
                    external_ref=dict(external_ref),
                    acquired_at=self._now(),
                )
                session.writer_lease = None
                session.last_progress_at = self._now()
                session.last_progress_event = "external_tui.claimed"
                self._ensure_tui_observed_binding_capabilities(session)
                await self.orchestrator.refresh_session_status_card(session)
                return session
            if not session.writer_owner or session.writer_owner.kind != "external_tui":
                if not _tui_hook_can_claim_existing_session(hook_type):
                    # ADR 0053 sentinel: activity hooks (not session-start /
                    # sync) from an external TUI while the orchestrator owns
                    # the writer lease mean a TUI process survived a takeover
                    # (bare `claude` argv is invisible to every scan — the
                    # hook is the only way it ever reveals itself). Kill it
                    # on fresh evidence; never flip ownership here.
                    await self._sentinel_terminate_remnant_tui(
                        session=session,
                        hook_type=hook_type,
                        payload=payload,
                    )
                    return None
                # Guard the orchestrator -> external_tui flip (deep-review
                # cluster B / concurrency#1). A claim may flip ownership only
                # when it is recent evidence of a real terminal:
                #   - fresh (capture stamp within the window) OR backed by a
                #     live TUI process — a stale replay describing a dead pid
                #     (the 2026-07-19 01:16 incident) satisfies neither and is
                #     refused, which is what fenced out the fresh worker while
                #     the card said "接管完成";
                #   - AND not predating the current owner: a claim captured
                #     before the takeover that installed this orchestrator owner
                #     must not flip it back (concurrency#1 — the claim belongs to
                #     a world that the takeover already superseded).
                # Freshness (not a live-process probe) is the primary gate so a
                # legitimate resume is never refused by a transient ps hiccup.
                hook_age = _tui_hook_captured_age(payload)
                fresh = hook_age is not None and hook_age <= self._tui_hook_fresh_seconds()
                live_tui = await asyncio.to_thread(
                    _tui_hook_has_live_tui_process, transport_kind, payload
                )
                captured_at = _payload_captured_at(payload)
                owner_acquired_at = float(getattr(session.writer_owner, "acquired_at", 0.0) or 0.0)
                predates_owner = (
                    captured_at is not None
                    and owner_acquired_at > 0
                    and captured_at < owner_acquired_at
                )
                # Two independent gates (round-3: predates is unconditional):
                #  - recency: the claim must be fresh OR backed by a live TUI
                #    (a dead-pid replay satisfies neither → refused);
                #  - ordering: the claim must NOT predate the current owner's
                #    acquisition. A claim captured before the takeover installed
                #    this owner describes a superseded world and must never flip
                #    it back — even if the old terminal is somehow still alive
                #    (that survivor is the sentinel's job to kill, not to hand
                #    back to). A deliberate post-takeover resume fires a FRESH
                #    SessionStart captured AFTER acquisition, so it is unaffected.
                if (not (fresh or live_tui)) or predates_owner:
                    _log_degrade(
                        "tui_claim_refused",
                        session_id=session.session_id,
                        hook_type=hook_type,
                        fresh=fresh,
                        live_tui=live_tui,
                        predates_owner=predates_owner,
                        age_seconds=round(hook_age, 1) if hook_age is not None else -1.0,
                    )
                    return None
                # Capture the structured owner's identity BEFORE the handoff
                # rewrites transport_kind/transport_ref: the worker shutdown
                # and stale-HITL sweep below need the old handle (ADR 0051).
                prior_generation = session.generation
                prior_transport_kind = session.transport_kind
                prior_owner_kind = session.writer_owner.kind if session.writer_owner else ""
                prior_handle = Orchestrator._handle_for_session(session)
                result = self.state.sessions.handoff_to_external_tui(
                    session.session_id,
                    generation=session.generation,
                    owner=actor,
                    resume_ref={"transport_kind": transport_kind, **dict(resume_ref)},
                    external_ref=external_ref,
                )
                if not result.accepted:
                    raise ChannelConfigError(f"could not claim structured session: {result.reason}")
                session = self.state.sessions.get(existing_id)
                await self.orchestrator.settle_hitls_for_external_claim(
                    session,
                    prior_transport_kind=prior_transport_kind,
                    prior_handle=prior_handle,
                    through_generation=prior_generation,
                )
                if prior_owner_kind == "orchestrator":
                    # The flip used to be silent — the user's next channel
                    # message just went nowhere. Make it explicit.
                    await self.orchestrator.notify_tui_conflict(
                        session,
                        kind="handback",
                        dedupe_key="handback",
                    )
            else:
                session.transport_ref.update(external_ref)
                if session.writer_owner is not None:
                    session.writer_owner.external_ref.update(external_ref)
            self._ensure_tui_observed_binding_capabilities(session)
            if not session.cached_title:
                session.cached_title = _telegram_session_topic_name(
                    agent_name,
                    f"TUI {_resume_ref_identity(transport_kind, resume_ref)}",
                )
                session.title_source = "tui_hook"
            await self.orchestrator.refresh_session_status_card(session)
            return session

        if not _tui_hook_can_create_session(hook_type):
            return None

        binding = await self._create_tui_observed_binding(agent_name, transport_kind, resume_ref, payload)
        session_id = self._tui_observed_session_id(agent_name, transport_kind, resume_ref)
        session = self.state.sessions.create_observed_session(
            session_id=session_id,
            binding=binding,
            cwd=str(payload.get("cwd", "") or self.config.cwd),
            external_ref=external_ref,
            owner=actor,
        )
        session.cached_title = _telegram_session_topic_name(
            agent_name,
            f"TUI {_resume_ref_identity(transport_kind, resume_ref)}",
        )
        session.title_source = "tui_hook"
        self._grant_tui_channel_owners(session.session_id, binding)
        await self.orchestrator.refresh_session_status_card(session)
        return session

    def _tui_hook_fresh_seconds(self) -> float:
        return self.config.tui_hook_fresh_seconds

    def _tui_hook_is_fresh(self, payload: dict[str, Any]) -> bool:
        age = _tui_hook_captured_age(payload)
        return age is not None and age <= self._tui_hook_fresh_seconds()

    def _tui_sentinel_enabled(self) -> bool:
        return self.config.tui_sentinel_enabled

    async def _sentinel_terminate_remnant_tui(
        self,
        *,
        session,
        hook_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Kill a TUI process that survived a takeover (ADR 0053).

        Preconditions enforced here, not by the caller:
        - the sentinel is enabled (WALKCODE_TUI_SENTINEL_ENABLED; kill switch);
        - the orchestrator holds the writer lease — a live external TUI hooking
          into an orchestrator-driven session is a double-writer conflict
          whether or not an explicit takeover happened;
        - the hook is FRESH (capture stamp within tui_hook_fresh_seconds): bare
          `claude` argv carries no session identity, so only a seconds-old hook
          proves the pid belongs to this session;
        - the pid still matches the identity captured AT HOOK TIME (pid +
          lstart + command), verified inside the controller right before each
          signal — a reused pid is skipped, never killed (cluster D);
        - the command classifies as an external TUI — never walkcode's own
          `_bundled` SDK workers (revert 6c83ed9 lesson: the freshly resumed
          worker must be untouchable).

        Termination runs concurrently with a short per-signal timeout so a
        hung process cannot pin the channel ingress lock for long (cluster G).
        """
        if not session.writer_owner or session.writer_owner.kind != "orchestrator":
            return
        if session.status == "stopped":
            return
        candidates = [
            entry
            for entry in _tui_hook_process_tree_entries(payload)
            if _command_is_external_tui_process(str(entry.get("command", "") or ""))
        ]
        if not candidates:
            return
        if not self._tui_hook_is_fresh(payload):
            _log_degrade(
                "sentinel_skip_stale_tui_hook",
                session_id=session.session_id,
                hook_type=hook_type,
                age_seconds=round(_tui_hook_captured_age(payload) or -1.0, 1),
            )
            return
        own_pid = os.getpid()
        targets: list[tuple[int, str, str]] = []
        for entry in candidates:
            command = str(entry.get("command", "") or "")
            lstart = str(entry.get("lstart", "") or "")
            try:
                pid = int(entry.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 1 or pid == own_pid:
                continue
            targets.append((pid, lstart, command))
        if not targets:
            return
        if not self._tui_sentinel_enabled():
            # Kill switch: do NOT terminate, but do NOT stay silent either — a
            # surviving double-writer with no notice is how the whole incident
            # went unnoticed. Degrade to notify-only (round-2 cluster).
            for pid, lstart, command in targets:
                _log_degrade(
                    "sentinel_disabled_remnant_detected",
                    session_id=session.session_id,
                    pid=pid,
                )
                await self.orchestrator.notify_tui_conflict(
                    session,
                    kind="remnant_detected",
                    pid=pid,
                    command=command,
                    detail="哨兵已关闭（WALKCODE_TUI_SENTINEL_ENABLED），未自动终止。",
                    dedupe_key=f"{pid}:{lstart}" if lstart else str(pid),
                )
            return
        controller = self._sentinel_process_controller()

        async def _terminate(target: tuple[int, str, str]):
            pid, lstart, command = target
            # The controller re-probes and compares (lstart, command) before
            # each signal, so a pid reused since hook capture is skipped.
            result = await controller.terminate(
                {
                    "pid": pid,
                    "command": command,
                    "lstart": lstart,
                    "allow_terminate": True,
                    "source": "tui_hook_sentinel",
                },
                "post_takeover_remnant_tui",
            )
            return pid, lstart, command, result

        outcomes = await asyncio.gather(
            *(_terminate(t) for t in targets), return_exceptions=True
        )
        # zip with targets so an exception (which loses the return tuple) is
        # still paired with its target and surfaced as remnant_detected
        # (round-2: a gather exception was logged but never notified).
        for target, outcome in zip(targets, outcomes):
            t_pid, t_lstart, t_command = target
            if isinstance(outcome, Exception):
                _log_degrade(
                    "sentinel_terminate_exception",
                    session_id=session.session_id,
                    pid=t_pid,
                    error=repr(outcome),
                )
                await self.orchestrator.notify_tui_conflict(
                    session,
                    kind="remnant_detected",
                    pid=t_pid,
                    command=t_command,
                    detail=f"终止异常：{type(outcome).__name__}",
                    dedupe_key=f"{t_pid}:{t_lstart}" if t_lstart else str(t_pid),
                )
                continue
            pid, lstart, command, result = outcome
            dedupe_key = f"{pid}:{lstart}" if lstart else str(pid)
            if result.accepted and result.state in {"terminated", "killed"}:
                _log_degrade(
                    "sentinel_terminated_remnant_tui",
                    session_id=session.session_id,
                    pid=pid,
                    state=result.state,
                )
                await self.orchestrator.notify_tui_conflict(
                    session,
                    kind="remnant_terminated",
                    pid=pid,
                    command=command,
                    dedupe_key=dedupe_key,
                )
            elif not result.accepted:
                # Includes identity_probe_failed: a candidate exists but we
                # could not verify/kill it — surface it, do not stay silent.
                _log_degrade(
                    "sentinel_terminate_failed",
                    session_id=session.session_id,
                    pid=pid,
                    reason=result.reason,
                )
                await self.orchestrator.notify_tui_conflict(
                    session,
                    kind="remnant_detected",
                    pid=pid,
                    command=command,
                    detail=f"终止失败：{result.reason}",
                    dedupe_key=dedupe_key,
                )
            # accepted + already_exited → remnant died on its own or the pid was
            # reused (controller logged terminate_stale_pid_skipped); no notice.

    def _sentinel_process_controller(self):
        # Short per-signal timeout so a hung remnant cannot pin channel ingress
        # (cluster G). Test seam: monkeypatched in unit tests to avoid signals.
        return LocalProcessController(timeout=1.5)

    async def _refresh_loaded_tui_observed_bindings(self) -> None:
        if self._loaded_tui_observed_bindings_refreshed:
            return
        changed = False
        summaries = [
            summary
            for kind in ("telegram", "lark")
            for summary in self.state.sessions.list_sessions(channel_kind=kind)
        ]
        for summary in summaries:
            session = self.state.sessions.get(summary.session_id)
            if session.status == "stopped" and session.lifecycle_state not in {
                "EXTERNAL_DETACHED_IMPORTABLE",
                "EXTERNAL_DETACHED_UNIMPORTABLE",
            }:
                continue
            if session.writer_owner is None or session.writer_owner.kind != "external_tui":
                continue
            if session.channel_binding is not None:
                # Re-grant on load: owners are granted at creation, but config
                # (e.g. LARK_ALLOWED_OPEN_IDS) may have been added afterwards.
                # Detached sessions need this too — importing them requires an
                # authorized actor just like takeover does.
                self._grant_tui_channel_owners(session.session_id, session.channel_binding)
            if await self._maybe_mark_stale_tui_process_detached(session):
                changed = True
                await self.orchestrator.refresh_session_status_card(session)
                continue
            if self._ensure_tui_observed_binding_capabilities(session):
                changed = True
            await self.orchestrator.refresh_session_status_card(session)
        # Mark done only after a full pass: a cancelled/failed first pass must
        # be retried on the next tick instead of silently skipping re-grants.
        self._loaded_tui_observed_bindings_refreshed = True
        if changed:
            self.save_state()

    async def _maybe_mark_stale_tui_process_detached(self, session) -> bool:
        process_ref = _external_tui_process_ref(session)
        if not process_ref or _process_ref_is_running(process_ref):
            return False
        if await self._claude_daemon_session_alive(session):
            # Attach TUI is gone but the daemon worker lives on: this is a
            # detach, not an end. Keep the session writable via daemon reply.
            if session.last_progress_event != "external_tui.tui_detached_daemon_alive":
                session.last_progress_event = "external_tui.tui_detached_daemon_alive"
                session.last_progress_at = self._now()
                return True
            return False
        return self._mark_stale_tui_process_detached_if_needed(session)

    def _mark_stale_tui_process_detached_if_needed(self, session) -> bool:
        process_ref = _external_tui_process_ref(session)
        if not process_ref:
            return False
        if _process_ref_is_running(process_ref):
            return False
        changed = False
        if session.status != "stopped":
            session.status = "stopped"
            changed = True
        target_state = (
            "EXTERNAL_DETACHED_IMPORTABLE"
            if _session_has_durable_resume_ref(session)
            else "EXTERNAL_DETACHED_UNIMPORTABLE"
        )
        if session.lifecycle_state != target_state:
            session.lifecycle_state = target_state
            changed = True
        if session.stop_reason != "external_tui_process_gone":
            session.stop_reason = "external_tui_process_gone"
            changed = True
        if session.writer_owner is None or session.writer_owner.kind != "none":
            session.writer_owner = WriterOwner(kind="none")
            changed = True
        if session.writer_lease is not None:
            session.writer_lease = None
            changed = True
        if session.last_progress_event != "external_tui.detached":
            session.last_progress_event = "external_tui.detached"
            changed = True
        session.last_progress_at = self._now()
        session.generation += 1
        self._ensure_tui_observed_binding_capabilities(session)
        return True if changed else False

    @staticmethod
    def _ensure_tui_observed_binding_capabilities(session) -> bool:
        binding = session.channel_binding
        if binding is None or binding.channel_kind not in {"telegram", "lark"}:
            return False
        if binding.capabilities.get("origin") == "daemon_spawn":
            # Channel-born daemon session (ADR 0048): the binding is the
            # user's own chat/topic. Repainting it as a readonly observation
            # topic would strip the interactive semantics the user started
            # the session with.
            return False
        if not binding.thread_id:
            return False
        changed = False
        if binding.channel_kind == "telegram":
            keys = ("status_card", "native_topic", "readonly_topic", "pin_status_card", "static_status_card")
        else:
            keys = ("status_card", "readonly_topic")
        for key in keys:
            if key not in binding.capabilities:
                binding.capabilities[key] = True
                changed = True
        if binding.capabilities.get("origin") != "external_tui":
            binding.capabilities["origin"] = "external_tui"
            changed = True
        if "topic_closed" in binding.capabilities:
            binding.capabilities.pop("topic_closed", None)
            changed = True
        if session.writer_owner and session.writer_owner.kind == "external_tui":
            if session.lifecycle_state != "EXTERNAL_OBSERVED_READONLY":
                session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
                changed = True
        return changed

    async def _create_tui_observed_binding(
        self,
        agent_name: str,
        transport_kind: str,
        resume_ref: dict[str, Any],
        payload: dict[str, Any],
    ) -> ChannelBinding:
        channel_kind = self.config.channel.kind
        if channel_kind == "lark":
            return await self._create_lark_tui_observed_binding(
                agent_name, transport_kind, resume_ref
            )
        if channel_kind != "telegram":
            raise ChannelConfigError(
                f"TUI observed session ingress is not supported for channel: {channel_kind}"
            )
        chat_id = _tui_telegram_chat_id(self.config.channel)
        if not chat_id:
            raise ChannelConfigError(
                "missing Telegram chat for TUI observation; set WALKCODE_TELEGRAM_TUI_CHAT_ID or exactly one TELEGRAM_ALLOWED_CHAT_IDS"
            )
        thread_id = str(
            payload.get("telegram_thread_id")
            or self.config.channel.options.get("tui_thread_id", "")
            or ""
        )
        channel = self.channels["telegram"]
        if not thread_id:
            thread_id = await self._create_telegram_topic_for_chat_if_possible(
                channel,
                chat_id=chat_id,
                topic_name=_telegram_session_topic_name(
                    agent_name,
                    f"TUI {_resume_ref_identity(transport_kind, resume_ref)}",
                ),
            )
        return ChannelBinding(
            channel_kind="telegram",
            account_id="bot",
            chat_id=chat_id,
            thread_id=thread_id,
            capabilities={
                "status_card": True,
                "native_topic": True,
                "readonly_topic": True,
                "pin_status_card": True,
                "static_status_card": True,
                "origin": "external_tui",
            },
        )

    async def _create_lark_tui_observed_binding(
        self,
        agent_name: str,
        transport_kind: str,
        resume_ref: dict[str, Any],
    ) -> ChannelBinding:
        chat_id = _tui_lark_chat_id(self.config.channel)
        if not chat_id:
            raise ChannelConfigError(
                "missing Lark chat for TUI observation; set WALKCODE_LARK_TUI_CHAT_ID "
                "or exactly one LARK_ALLOWED_CHAT_IDS"
            )
        channel = self.channels["lark"]
        # Lark has no topic-creation API; the observation thread is rooted at a
        # bot-sent notice message so the session's transcript lands in one
        # reply chain instead of the chat root.
        root_id = ""
        try:
            root_id = await channel.send_view(
                ChannelBinding(
                    channel_kind="lark",
                    account_id="bot",
                    chat_id=chat_id,
                    thread_id="",
                    root_message_id="",
                ),
                {
                    "type": "text",
                    "text": (
                        f"👀 TUI: {agent_name} "
                        f"{_resume_ref_identity(transport_kind, resume_ref)}"
                    ),
                },
            )
        except Exception as exc:
            print(
                f"lark TUI observation root message failed; falling back to chat root: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
        return ChannelBinding(
            channel_kind="lark",
            account_id="bot",
            chat_id=chat_id,
            thread_id=root_id,
            root_message_id=root_id,
            # No static_status_card: Lark card patches are uncapped, so the
            # observation card keeps refreshing (e.g. drops the Take over
            # button once the session is taken over).
            capabilities={
                "status_card": True,
                "readonly_topic": True,
                "origin": "external_tui",
            },
        )

    def _grant_tui_channel_owners(self, session_id: str, binding: ChannelBinding) -> None:
        if self.state.authz is None:
            return
        actor_ids = list(self.config.channel.options.get("allowed_actor_ids", ()) or ())
        if binding.channel_kind == "lark":
            # Lark actors are open_ids (ou_...), never the chat id (oc_...);
            # without this grant an observed session silently rejects every
            # takeover request from the chat.
            actor_ids.extend(self.config.channel.options.get("allowed_open_ids", ()) or ())
        if binding.channel_kind == "telegram" and binding.chat_id and not binding.chat_id.startswith("-"):
            actor_ids.append(binding.chat_id)
        for actor_id in dict.fromkeys(str(item) for item in actor_ids if item):
            self.state.authz.grant(
                session_id,
                ActorRef(binding.channel_kind, actor_id, actor_id),
                SessionRole.OWNER,
            )

    async def _drain_tui_narration(self, session, payload: dict[str, Any]) -> list[str]:
        path = str(payload.get("transcript_path", "") or "")
        if not path:
            return []
        cursor = self._tui_transcript_cursors.get(session.session_id)
        new_cursor, texts = await asyncio.to_thread(
            _read_transcript_narration, path, cursor, _payload_transcript_boundary(payload)
        )
        if new_cursor is not None:
            self._store_tui_narration_cursor(session.session_id, new_cursor)
        return texts

    def _advance_tui_narration_cursor(self, session, payload: dict[str, Any]) -> None:
        path = str(payload.get("transcript_path", "") or "")
        if not path:
            return
        try:
            info = os.stat(path)
        except OSError:
            return
        size = int(info.st_size)
        file_key = (int(info.st_dev), int(info.st_ino))
        boundary = _payload_transcript_boundary(payload)
        offset = size
        if boundary is not None:
            boundary_size, boundary_key = boundary
            if boundary_key is None or boundary_key == file_key:
                offset = max(0, min(boundary_size, size))
            # else: the hook's file is gone — everything currently at this
            # path is pre-cursor history; skipping to EOF is the only safe
            # advance ("never replay").
        discarding = False
        prev = self._tui_transcript_cursors.get(session.session_id)
        if (
            prev is not None
            and len(prev) >= 4
            and prev[0] == path
            and prev[2] == file_key
        ):
            # Advance is monotonic: an out-of-order (older) hook must not
            # rewind the cursor and re-emit already-mirrored narration.
            if int(prev[1]) >= offset:
                offset = int(prev[1])
            # An in-progress over-long-line discard is preserved even across
            # a forward jump: we cannot prove the jump crossed that line's
            # real newline, and clearing the flag mid-line would hand the
            # line's tail to the JSON parser. Worst case the reader drops
            # one legit line after the jump — safe direction.
            discarding = bool(prev[3])
        self._store_tui_narration_cursor(session.session_id, (path, offset, file_key, discarding))

    def _store_tui_narration_cursor(self, session_id: str, cursor: tuple[Any, ...]) -> None:
        cursors = self._tui_transcript_cursors
        # LRU: re-insert on every write so eviction hits the least recently
        # ACTIVE session, and shrink until under the cap no matter which
        # write path grew it.
        cursors.pop(session_id, None)
        cursors[session_id] = cursor
        while len(cursors) > 512:
            cursors.pop(next(iter(cursors)))

    async def _send_tui_hook_output(self, session, *, hook_type: str, payload: dict[str, Any]) -> None:
        tool_event = _tui_hook_tool_event(hook_type, payload)
        if tool_event is not None:
            session.last_event_seq += 1
            self.orchestrator._record_session_progress(session, tool_event)
            # A TUI approval prompt must be loud: flip the health card to
            # waiting_permission and drop a dedicated notice card instead of
            # hiding behind one tool-progress line (it can only be answered in
            # the terminal, so the reader needs to know to go there).
            if hook_type == "permission-request":
                session.lifecycle_state = "WAITING_PERMISSION"
            elif session.lifecycle_state == "WAITING_PERMISSION":
                session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
            await self.orchestrator.refresh_session_status_card(session)
            view = self.orchestrator._event_to_view(session, tool_event)
            channel = self.channels.get(session.channel_binding.channel_kind) if session.channel_binding else None
            if channel is not None:
                # ADR 0055: the narration that preceded this tool call is in
                # the transcript but in NO hook payload — drain it onto the
                # burst card ahead of the tool line it narrates.
                for narration in await self._drain_tui_narration(session, payload):
                    await self.orchestrator._upsert_tool_progress_view(
                        session, channel, {"type": "turn_narration", "text": narration}
                    )
                await self.orchestrator._upsert_tool_progress_view(session, channel, view)
            if hook_type == "permission-request":
                # v3 dual-surface: when this dialog already has an interactive
                # gate card (open notify gate for the same tool), the old
                # "answer in the terminal" notice is both redundant and wrong.
                notice_tool = str(tool_event.payload.get("tool_name", "") or "")
                resume_ref = _external_claude_resume_ref(session)
                short = claude_daemon_short_from_resume_ref(resume_ref) if resume_ref else ""
                if not (short and self._has_open_notify_gate(short, tool_name=notice_tool)):
                    await self.orchestrator._send_session_view(
                        session,
                        {
                            "type": "tui_permission_notice",
                            "tool_name": notice_tool,
                            "summary": str(tool_event.payload.get("summary", "") or ""),
                        },
                        idempotency_key=f"external_tui:permission:{session.last_event_seq}",
                    )
            return
        text = _tui_hook_text(hook_type, payload)
        if hook_type in {"stop", "user-prompt-submit"}:
            # The turn-final text goes out as its own bubble below; skipping
            # the cursor past it keeps it from doubling as a narration line.
            self._advance_tui_narration_cursor(session, payload)
        session.last_progress_at = self._now()
        session.last_progress_event = f"external_tui.{hook_type}"
        if not text:
            return
        # The permission-request hook already sent the orange notice card;
        # Claude's follow-up Notification ("Claude needs your permission")
        # would just repeat it.
        if hook_type == "notification" and session.lifecycle_state == "WAITING_PERMISSION":
            return
        # Same dedup for the v3 path: an open notify gate means a rich card is
        # already (or about to be) up for this dialog.
        if hook_type == "notification" and "permission" in text.lower():
            resume_ref = _external_claude_resume_ref(session)
            short = claude_daemon_short_from_resume_ref(resume_ref) if resume_ref else ""
            if short and self._has_open_notify_gate(short):
                return
        # Idle noise ("Claude is waiting for your input") adds nothing on the
        # channel: the status card already shows the session is idle.
        if hook_type == "notification" and _is_idle_notification_text(text):
            return
        # A user prompt / turn end breaks the tool burst; next tools open a new card.
        self.orchestrator._seal_tool_progress_burst(session)
        await self.orchestrator.refresh_session_status_card(session)
        # A prompt injected from the channel via daemon reply echoes back as a
        # user-prompt-submit hook; re-posting it would repeat the sender's own
        # message ("TUI input" echo bug from the daemon-native rollout).
        if hook_type == "user-prompt-submit" and self.orchestrator.consume_daemon_reply_echo(
            session.session_id, text
        ):
            return
        session.last_event_seq += 1
        view = (
            {"type": "tui_user_input", "input": text}
            if hook_type == "user-prompt-submit"
            else {"type": "turn_completed", "message": text}
        )
        await self.orchestrator._send_session_view(
            session,
            view,
            idempotency_key=f"external_tui:{hook_type}:{session.last_event_seq}",
        )

    @staticmethod
    def _mark_tui_session_stopped(session, *, hook_type: str) -> None:
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.stop_reason = f"external_tui_{hook_type}"
        session.writer_lease = None
        session.writer_owner = WriterOwner(kind="none")
        if isinstance(session.transport_ref, dict):
            session.transport_ref.pop("daemon_live", None)

    def save_state(self) -> None:
        self.state_store.save(
            sessions=self.state.sessions,
            interactions=self.state.interactions,
            outbox=self.state.outbox,
            authz=self.state.authz,
            inbound_ledger=self.state.inbound_ledger,
            hitls=self.state.hitls,
        )

    def _describe_channel(self, kind: str, endpoint: ChannelEndpointConfig) -> dict[str, Any]:
        channel = self.channels.get(kind)
        item = {
            "kind": endpoint.kind,
            "configured": channel is not None,
            "live_ingress": self._channel_live_ingress(kind, endpoint),
            "credential_keys": sorted(endpoint.credentials),
        }
        if endpoint.kind == "lark":
            item["openapi_domain"] = str(endpoint.options.get("openapi_domain", "") or "")
            item["app_id_prefix"] = str(endpoint.credentials.get("app_id", "") or "")[:8]
        return item

    def _describe_agent(self, agent: str) -> dict[str, Any]:
        transport_kind = _agent_to_transport_kind(agent)
        transport = self.transports.get(transport_kind)
        if transport is None:
            return {
                "agent": agent,
                "available": False,
                "reason": "agent adapter is not wired",
                "capabilities": {},
            }
        item = self._describe_transport(transport_kind, transport)
        item["agent"] = agent
        return item

    @staticmethod
    def _channel_live_ingress(kind: str, endpoint: ChannelEndpointConfig) -> str:
        if kind == "telegram":
            return "polling" if endpoint.options.get("polling", True) else "webhook_not_wired"
        if kind == "lark":
            return "websocket"
        return "not_wired"

    def _telegram_chat_allowed(self, chat_id: str) -> bool:
        endpoint = self.config.channel
        if endpoint.kind != "telegram":
            return False
        allowed = tuple(str(item) for item in endpoint.options.get("allowed_chat_ids", ()) if item)
        if not allowed:
            return True
        return str(chat_id) in allowed

    async def _diagnose_telegram_bot(self, channel: TelegramChannelAdapter) -> dict[str, Any]:
        try:
            result = await channel.api.call("getMe", {})
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
            }
        bot = result.get("result", {}) if isinstance(result, dict) else {}
        return {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "bot_id_present": bool(bot.get("id")),
            "username": bot.get("username", ""),
            "first_name": bot.get("first_name", ""),
            "can_join_groups": bot.get("can_join_groups"),
            "can_read_all_group_messages": bot.get("can_read_all_group_messages"),
            "has_private_topics_enabled": bool(bot.get("has_topics_enabled")),
            "allows_users_to_create_topics": bool(bot.get("allows_users_to_create_topics")),
        }

    async def _diagnose_telegram_target_chat(
        self,
        channel: TelegramChannelAdapter,
        *,
        allowed: tuple[str, ...],
        bot: dict[str, Any],
    ) -> dict[str, Any]:
        if len(allowed) != 1:
            return {
                "ok": False,
                "reason": "exactly_one_allowed_chat_required",
                "allowed_chat_count": len(allowed),
                "topic_per_session_available": False,
            }
        try:
            result = await channel.api.call("getChat", {"chat_id": allowed[0]})
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
                "topic_per_session_available": False,
            }
        chat = result.get("result", {}) if isinstance(result, dict) else {}
        chat_type = str(chat.get("type", "") or "")
        is_forum = bool(chat.get("is_forum"))
        private_topics = chat_type == "private" and bool(bot.get("has_private_topics_enabled"))
        bot_admin = await self._diagnose_telegram_topic_admin(channel, chat_id=allowed[0]) if is_forum else {}
        can_manage_topics = bool(bot_admin.get("can_manage_topics"))
        topic_available = bool(private_topics or (is_forum and can_manage_topics))
        return {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "chat_id_present": bool(chat.get("id")),
            "type": chat_type,
            "is_forum": is_forum,
            "bot_admin": bot_admin,
            "native_topic_surface": (
                "forum_supergroup"
                if is_forum
                else "private_bot_topics"
                if private_topics
                else ""
            ),
            "topic_per_session_available": topic_available,
            "recommended_placement": "topic_per_session"
            if topic_available
            else "root_reply_chain",
            "topic_unavailable_reason": "bot_missing_manage_topics"
            if is_forum and not can_manage_topics
            else "",
        }

    async def _diagnose_telegram_topic_admin(
        self,
        channel: TelegramChannelAdapter,
        *,
        chat_id: str,
    ) -> dict[str, Any]:
        try:
            bot_result = await channel.api.call("getMe", {})
            bot = bot_result.get("result", {}) if isinstance(bot_result, dict) else {}
            bot_id = bot.get("id")
            if not bot_id:
                return {"checked": False, "reason": "bot_id_missing"}
            member_result = await channel.api.call("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
        except Exception as exc:
            return {
                "checked": False,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
            }
        member = member_result.get("result", {}) if isinstance(member_result, dict) else {}
        return {
            "checked": True,
            "status": str(member.get("status", "") or ""),
            "can_manage_topics": bool(
                member.get("status") == "creator"
                or member.get("can_manage_topics")
            ),
            "can_delete_messages": bool(member.get("can_delete_messages")),
            "can_invite_users": bool(member.get("can_invite_users")),
            "can_pin_messages": bool(member.get("can_pin_messages")),
        }

    async def _diagnose_telegram_webhook(self, channel: TelegramChannelAdapter) -> dict[str, Any]:
        try:
            result = await channel.api.call("getWebhookInfo", {})
        except Exception as exc:
            return {
                "ok": False,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
            }
        webhook = result.get("result", {}) if isinstance(result, dict) else {}
        return {
            "ok": bool(result.get("ok")) if isinstance(result, dict) else False,
            "has_url": bool(webhook.get("url")),
            "pending_update_count": webhook.get("pending_update_count", 0),
            "last_error_present": bool(webhook.get("last_error_date") or webhook.get("last_error_message")),
            "allowed_updates": list(webhook.get("allowed_updates") or []),
        }

    async def _peek_telegram_updates(self, channel: TelegramChannelAdapter, *, limit: int) -> list[dict[str, Any]]:
        result = await channel.api.call(
            "getUpdates",
            {
                "timeout": 0,
                "limit": limit,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        updates = result.get("result", []) if isinstance(result, dict) else []
        return [update for update in updates if isinstance(update, dict)]

    def _summarize_telegram_update(
        self,
        channel: TelegramChannelAdapter,
        update: dict[str, Any],
        *,
        index: int,
        known_chats: set[str],
    ) -> dict[str, Any]:
        try:
            inbound = channel.parse_update(update)
        except Exception as exc:
            return {
                "index": index,
                "parse_ok": False,
                "error": type(exc).__name__,
                "message": _safe_error_message(exc, channel.api.token),
            }
        text = inbound.text or ""
        item = {
            "index": index,
            "parse_ok": True,
            "event_kind": "callback_query" if inbound.callback else "message",
            "update_id_present": _telegram_update_id(update) is not None,
            "message_id_present": bool(inbound.message_id),
            "chat_id_present": bool(inbound.chat_id),
            "chat_allowed": self._telegram_chat_allowed(inbound.chat_id),
            "chat_matches_existing_session": inbound.chat_id in known_chats,
            "thread_id_present": bool(inbound.thread_id),
            "sender_id_present": bool(inbound.sender_id),
            "text_present": bool(text),
            "text_length": len(text),
            "attachment_count": len(inbound.attachments),
        }
        if not inbound.callback and item["chat_allowed"]:
            item.update(self._summarize_submit_gate(inbound))
        return item

    def _summarize_submit_gate(self, inbound) -> dict[str, Any]:
        transport_kind = self.config.agent_transport_kind
        selector = _agent_selector_command(inbound)
        if selector:
            return {
                "active_session_present": False,
                "submit_would_accept": True,
                "submit_action": "agent_selector_rejected",
                "submit_blocked_reason": "",
                "agent_selector_command": selector[0],
                "configured_agent": self.config.agent,
            }
        if _telegram_message_is_empty(inbound):
            return {
                "active_session_present": False,
                "submit_would_accept": True,
                "submit_action": "empty_message_ignored",
                "submit_blocked_reason": "",
            }
        resolution = self.state.sessions.resolve_active_binding(
            inbound.binding_key(), revival_eligible=self._revival_transport_ready
        )
        if resolution.reason:
            if resolution.reason == BlockedReason.AMBIGUOUS_SESSION:
                return {
                    "active_session_present": False,
                    "submit_would_accept": True,
                    "submit_action": "session_chooser",
                    "submit_blocked_reason": resolution.reason,
                }
            return {
                "active_session_present": False,
                "submit_would_accept": False,
                "submit_blocked_reason": resolution.reason,
            }
        if not resolution.session_id:
            return self._summarize_new_session_gate(transport_kind)

        session = self.state.sessions.get(resolution.session_id)
        actor = ActorRef(inbound.channel_kind, inbound.sender_id, inbound.sender_display)
        if self.state.authz is not None:
            authz = self.state.authz.can_submit(session.session_id, actor)
            if not authz.allowed:
                return {
                    "active_session_present": True,
                    "active_session_status": session.status,
                    "active_session_lifecycle": session.lifecycle_state,
                    "submit_would_accept": False,
                    "submit_blocked_reason": authz.reason,
                }
        if _session_is_channel_revival_candidate(session) and self._revival_transport_ready(session):
            # ADR 0054: the real submit path revives this session instead of
            # dead-ending at SESSION_STOPPED — report it as submittable.
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": True,
                "submit_action": "revive_stopped_session",
                "submit_blocked_reason": "",
                "submit_requires_resume": True,
            }
        transport = self.transports.get(session.transport_kind)
        if session.lifecycle_state == "IDLE":
            if transport is None:
                return {
                    "active_session_present": True,
                    "active_session_status": session.status,
                    "active_session_lifecycle": session.lifecycle_state,
                    "submit_would_accept": False,
                    "submit_blocked_reason": "transport_not_wired",
                }
            try:
                caps = transport.capabilities()
            except Exception as exc:
                return {
                    "active_session_present": True,
                    "active_session_status": session.status,
                    "active_session_lifecycle": session.lifecycle_state,
                    "submit_would_accept": False,
                    "submit_blocked_reason": type(exc).__name__,
                }
            if not caps.resume_after_complete:
                return {
                    "active_session_present": True,
                    "active_session_status": session.status,
                    "active_session_lifecycle": session.lifecycle_state,
                    "submit_would_accept": False,
                    "submit_blocked_reason": BlockedReason.CAPABILITY_DISABLED,
                }
            if not _session_has_durable_resume_ref(session):
                return {
                    "active_session_present": True,
                    "active_session_status": session.status,
                    "active_session_lifecycle": session.lifecycle_state,
                    "submit_would_accept": False,
                    "submit_blocked_reason": "missing_resume_ref",
                }
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": True,
                "submit_blocked_reason": "",
                "submit_requires_resume": True,
            }
        validation = self.state.sessions.validate_submit(session.session_id, session.generation)
        if not validation.accepted:
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": False,
                "submit_blocked_reason": validation.reason,
            }
        if transport is None:
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": False,
                "submit_blocked_reason": "transport_not_wired",
            }
        try:
            caps = transport.capabilities()
        except Exception as exc:
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": False,
                "submit_blocked_reason": type(exc).__name__,
            }
        if not caps.structured_input:
            return {
                "active_session_present": True,
                "active_session_status": session.status,
                "active_session_lifecycle": session.lifecycle_state,
                "submit_would_accept": False,
                "submit_blocked_reason": BlockedReason.CAPABILITY_DISABLED,
            }
        return {
            "active_session_present": True,
            "active_session_status": session.status,
            "active_session_lifecycle": session.lifecycle_state,
            "submit_would_accept": True,
            "submit_blocked_reason": "",
        }

    def _revival_transport_ready(self, session) -> bool:
        """Mirror of Orchestrator._revival_transport_ready for the doctor path."""
        transport = self.transports.get(session.transport_kind)
        if transport is None:
            return False
        try:
            return bool(transport.capabilities().resume_after_complete)
        except Exception:
            return False

    def _summarize_new_session_gate(self, transport_kind: str | None = None) -> dict[str, Any]:
        selected_transport = transport_kind or self.config.agent_transport_kind
        transport = self.transports.get(selected_transport)
        if transport is None:
            return {
                "active_session_present": False,
                "submit_would_accept": False,
                "submit_blocked_reason": "transport_not_wired",
            }
        try:
            caps = transport.capabilities()
        except Exception as exc:
            return {
                "active_session_present": False,
                "submit_would_accept": False,
                "submit_blocked_reason": type(exc).__name__,
            }
        if not (caps.structured_input and caps.structured_output):
            return {
                "active_session_present": False,
                "submit_would_accept": False,
                "submit_blocked_reason": BlockedReason.CAPABILITY_DISABLED,
            }
        return {
            "active_session_present": False,
            "submit_would_accept": True,
            "submit_blocked_reason": "",
        }

    async def _confirm_telegram_offset(self, channel: TelegramChannelAdapter) -> None:
        try:
            await channel.api.call(
                "getUpdates",
                {
                    "offset": self._telegram_offset,
                    "timeout": 0,
                    "limit": 1,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
        except Exception as exc:
            self.last_telegram_offset_confirm_error = str(exc)
        else:
            self.last_telegram_offset_confirm_error = ""

    @staticmethod
    def _describe_transport(_kind: str, transport: AgentTransport) -> dict[str, Any]:
        try:
            caps = transport.capabilities()
        except Exception as exc:
            return {"available": False, "reason": str(exc), "capabilities": {}}
        capability_map = dict(caps.__dict__)
        return {
            "available": bool(caps.structured_input and caps.structured_output),
            "capabilities": capability_map,
        }


def run_native_cli(args) -> None:
    try:
        runtime = ChannelNativeRuntime.from_env()
    except ChannelConfigError as exc:
        print(f"channel-native config error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    if args.native_command == "doctor":
        status = runtime.describe()
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, sort_keys=True))
        else:
            print(_format_status(status))
        return
    if args.native_command == "debug":
        module = getattr(args, "debug_module", "")
        if module == "telegram":
            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=args.limit))
            if getattr(args, "json", False):
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(_format_telegram_diagnosis(report))
            return
        if module == "lark":
            report = asyncio.run(runtime.diagnose_lark_ingress())
            if getattr(args, "json", False):
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print(_format_lark_diagnosis(report))
            return
        raise ChannelConfigError(f"unknown native debug module: {module}")
    if args.native_command == "hook":
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"invalid hook JSON: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        if not isinstance(payload, dict):
            print("invalid hook JSON: expected object", file=sys.stderr)
            raise SystemExit(1) from None
        payload.setdefault("_walkcode_hook_pid", os.getpid())
        parent_pid = os.getppid()
        payload.setdefault("_walkcode_hook_process_group", os.getpgrp())
        payload.setdefault("_walkcode_hook_parent_pid", parent_pid)
        payload.setdefault("_walkcode_hook_process_tree_entries", _process_tree_entries(parent_pid, max_depth=4))
        payload.setdefault("_walkcode_hook_process_tree", _process_tree_commands(parent_pid, max_depth=4))
        payload.setdefault("_walkcode_infer_tui_pid", True)
        # Capture-time stamp: ownership decisions (handoff / sentinel kill)
        # must distinguish a hook fired seconds ago from a deferred-queue
        # replay describing a world that no longer exists (ADR 0053).
        payload.setdefault("_walkcode_hook_captured_at", time.time())
        # Capture-time transcript boundary for narration mirroring (ADR 0055).
        _stamp_transcript_size(payload)
        if getattr(args, "gate", False):
            output = runtime.gate_tui_hook(
                hook_type=args.hook_type,
                payload=payload,
                agent=getattr(args, "agent", "") or "",
            )
            if output is not None:
                print(json.dumps(output, ensure_ascii=False))
            raise SystemExit(0)
        if getattr(args, "defer", False):
            queued = runtime.defer_tui_hook(
                hook_type=args.hook_type,
                payload=payload,
                agent=getattr(args, "agent", "") or "",
            )
            if getattr(args, "json", False):
                print(json.dumps(queued, indent=2, sort_keys=True))
            raise SystemExit(0)
        result = asyncio.run(
            runtime.process_tui_hook(
                hook_type=args.hook_type,
                payload=payload,
                agent=getattr(args, "agent", "") or "",
            )
        )
        output = {
            "accepted": result.accepted,
            "reason": result.reason,
            "blocked_input_id": result.blocked_input_id,
        }
        if getattr(args, "json", False):
            print(json.dumps(output, indent=2, sort_keys=True))
        elif not result.accepted:
            print(f"native hook rejected: {result.reason}", file=sys.stderr)
        raise SystemExit(0 if result.accepted else 1)
    if args.native_command == "serve":
        if runtime.config.channel_kind == "lark":
            if getattr(args, "once", False):
                raise ChannelConfigError(
                    "serve --once is not supported for the lark channel (WebSocket push has "
                    "no pull semantics); use doctor and debug lark instead"
                )
            print(_format_status(runtime.describe()))
            print("channel-native V3 runtime listening via Lark WebSocket")
            asyncio.run(runtime.serve_lark_ws())
            return
        if getattr(args, "once", False):
            processed = asyncio.run(
                runtime.poll_telegram_once(timeout=args.poll_timeout, limit=args.limit)
            )
            print(f"processed {processed} update(s)")
            return
        print(_format_status(runtime.describe()))
        print("channel-native V3 runtime listening via Telegram polling")
        asyncio.run(runtime.serve_telegram_polling(timeout=args.poll_timeout, limit=args.limit))
        return
    raise ChannelConfigError(f"unknown native command: {args.native_command}")


def _load_or_create_state(state_store: JsonFileStateStore, *, now=time.time) -> StateSnapshot:
    if state_store.path.exists():
        return state_store.load()
    return StateSnapshot(
        sessions=SessionRegistry(now=now),
        interactions=_new_interaction_store(now=now),
        outbox=DurableOutbox(now=now),
        authz=AuthorizationStore(now=now),
        inbound_ledger=InboundLedger(now=now),
    )


def _new_interaction_store(*, now=time.time):
    from .channel_native import InteractionStore

    return InteractionStore(now=now)


def _build_channels(
    config: ChannelNativeConfig,
    *,
    telegram_api: TelegramBotApi | None = None,
    lark_api: LarkBotApi | None = None,
) -> dict[str, ChannelAdapter]:
    channels: dict[str, ChannelAdapter] = {}
    endpoint = config.channel
    if endpoint.kind == "telegram":
        api = telegram_api or TelegramBotApi(endpoint.credentials["bot_token"])
        channels[endpoint.kind] = TelegramChannelAdapter(
            api,
            use_rich_messages=bool(endpoint.options.get("rich_messages")),
        )
    elif endpoint.kind == "lark":
        if lark_api is None:
            ack_registry = AckRegistry()
            lark_api = build_lark_live_api(
                endpoint.credentials,
                endpoint.options,
                ack_registry=ack_registry,
            )
            # serve_lark_ws picks the registry up from the api so the WS
            # bridge and ack_callback share the same future map.
            lark_api.ack_registry = ack_registry
        channels[endpoint.kind] = LarkChannelAdapter(lark_api)
    return channels


def _build_transports(config: ChannelNativeConfig) -> dict[str, AgentTransport]:
    transports: dict[str, AgentTransport] = {}
    kind = config.agent_transport_kind
    if kind == "claude_headless":
        claude_options = config.agent_options.get("claude", {})
        transports[kind] = ClaudeHeadlessTransport(
            settings=claude_options.get("settings"),
            cli_path=claude_options.get("cli_path"),
            config_dir=claude_options.get("config_dir"),
            anthropic_base_url=claude_options.get("anthropic_base_url"),
            permission_mode=claude_options.get("permission_mode"),
            settle_grace_seconds=float(claude_options.get("settle_grace_seconds", 5.0)),
            background_wait_ceiling_seconds=float(
                claude_options.get("background_wait_ceiling_seconds", 3600.0)
            ),
        )
        # Multi-UI sync (ADR 0046): the daemon transport rides alongside the
        # headless one — reply/subscribe against TUI-owned daemon workers.
        # "auto" registers it unconditionally; every op degrades to
        # TransportUnavailable when the per-profile daemon is not running.
        if str(claude_options.get("daemon_mode", "") or "auto") != "off":
            transports["claude_daemon"] = ClaudeDaemonTransport(
                config_dir=str(claude_options.get("config_dir", "") or ""),
                # Enables the PreToolUse gate decision path: card callbacks
                # write decisions/<rid>.json under this state's hook spool.
                gate_state_path=config.state_path,
            )
    elif kind == "codex_app_server":
        if shutil.which("codex"):
            transports[kind] = CodexAppServerTransport(client=_build_codex_app_server_client(config))
        else:
            transports[kind] = _UnavailableTransport(kind, "codex CLI is not installed")
    else:
        transports[kind] = _UnavailableTransport(kind, f"unknown transport: {kind}")
    return transports


def _codex_home_path(codex_home: str = "") -> Path:
    if codex_home:
        return Path(codex_home).expanduser()
    return Path.home() / ".codex"


def _build_codex_app_server_client(config: ChannelNativeConfig) -> Any:
    options = config.agent_options.get("codex", {})
    mode = str(options.get("app_server_mode", "") or "auto").strip().lower()
    socket_path = str(options.get("app_server_socket", "") or "")
    codex_home = str(options.get("codex_home", "") or "")
    if mode == "auto":
        if _codex_standalone_daemon_available(codex_home):
            return CodexManagedAppServerClient(socket_path=socket_path, codex_home=codex_home)
        return CodexStdioAppServerClient(codex_home=codex_home)
    if mode in {"daemon", "managed", "shared"}:
        return CodexManagedAppServerClient(socket_path=socket_path, codex_home=codex_home)
    if mode == "stdio":
        return CodexStdioAppServerClient(codex_home=codex_home)
    raise ChannelConfigError(
        f"unknown WALKCODE_CODEX_APP_SERVER_MODE: {mode}; use auto, daemon, or stdio"
    )


def _codex_standalone_daemon_available(codex_home: str = "") -> bool:
    return (_codex_home_path(codex_home) / "packages" / "standalone" / "current" / "codex").exists()


def _build_external_tui_controllers() -> dict[str, Any]:
    return {"process": LocalProcessController()}


def _resolve_workspace_target(target: str, roots: tuple[str, ...]) -> tuple[str | None, str]:
    """Resolve a /repo target (bare name or path) to a directory inside one of
    the allowlisted workspace roots. Returns (resolved_path, "") on success or
    (None, reason). Realpath containment is checked on both sides so neither
    `..` segments nor symlinks can escape a root."""
    cleaned = str(target or "").strip()
    if not cleaned:
        return None, "empty target"
    resolved_roots = []
    for root in roots:
        try:
            resolved_roots.append(Path(root).expanduser().resolve())
        except OSError:
            continue
    if not resolved_roots:
        return None, "no usable workspace roots"
    candidates: list[Path] = []
    if "/" in cleaned or cleaned.startswith("~"):
        path = Path(cleaned).expanduser()
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.extend(root / cleaned for root in resolved_roots)
    else:
        candidates.extend(root / cleaned for root in resolved_roots)
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        for root in resolved_roots:
            if resolved == root or root in resolved.parents:
                return str(resolved), ""
        return None, f"{resolved} is outside the configured workspace roots"
    return None, f"no directory named {cleaned!r} under the configured workspace roots"


def _repo_usage_text(roots: tuple[str, ...]) -> str:
    lines = [
        "用法：/repo <目录名或路径> <任务描述>",
        "目录白名单：",
    ]
    for root in roots:
        lines.append(f"- {root}")
        try:
            children = sorted(
                item.name
                for item in Path(root).expanduser().iterdir()
                if item.is_dir() and not item.name.startswith(".")
            )[:12]
        except OSError:
            children = []
        if children:
            lines.append("  " + "、".join(children))
    return "\n".join(lines)


def _telegram_update_id(update: dict[str, Any]) -> int | None:
    try:
        return int(update.get("update_id"))
    except (TypeError, ValueError):
        return None


# Reply text for silently-rejected Lark messages from authorized senders.
# Reasons with their own feedback card (external_tui_readonly → takeover
# prompt, ambiguous_session → session chooser) are intentionally absent.
# LEASE_EXPIRED is deliberately absent: it does not complete the inbound
# ledger, so channel redelivery retries the submit once the lease recovers —
# telling the user to "resend" would conflict with that automatic retry.
_LARK_REJECTION_NOTES = {
    BlockedReason.UNAUTHORIZED: "⛔ 这条消息没有提交：你没有操作这个会话的权限。",
    BlockedReason.SESSION_STOPPED: "⚪ 这条消息没有提交：会话已结束。到根会话发新消息即可开新任务。",
}


def _telegram_result_confirms_offset(result: SubmitResult) -> bool:
    if result.accepted:
        return True
    return result.reason in {
        BlockedReason.UNAUTHORIZED,
        BlockedReason.DUPLICATE_INBOUND,
        BlockedReason.INVALID_TOKEN,
        BlockedReason.ALREADY_DECIDED,
        BlockedReason.STALE_GENERATION,
        BlockedReason.NOT_FOUND,
        BlockedReason.SESSION_STOPPED,
        BlockedReason.EXTERNAL_TUI_READONLY,
        "keep_readonly",
    }


def _agent_selector_command(inbound: Any) -> tuple[str, str] | None:
    text = str(getattr(inbound, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    command, _sep, prompt = text.partition(" ")
    name = command[1:].split("@", 1)[0].lower().replace("_", "-")
    aliases = {
        "claude": "claude",
        "claude-code": "claude",
        "claudecode": "claude",
        "codex": "codex",
    }
    agent = aliases.get(name)
    if agent is None:
        return None
    return agent, prompt.strip()


def _telegram_bot_command(inbound: Any) -> tuple[str, str] | None:
    text = str(getattr(inbound, "text", "") or "").strip()
    if not text.startswith("/"):
        return None
    command, _sep, argument = text.partition(" ")
    name = command[1:].split("@", 1)[0].lower().replace("_", "-")
    aliases = {
        "status": "status",
        "sessions": "sessions",
        "session": "sessions",
        "model": "model",
        "skills": "skills",
        "commands": "commands",
        "takeover": "takeover",
        "take-over": "takeover",
        "repo": "repo",
    }
    resolved = aliases.get(name)
    if resolved is None:
        return None
    return resolved, argument.strip()


def _telegram_service_message_kind(inbound: Any) -> str:
    raw = getattr(inbound, "raw", {})
    message = raw.get("message", {}) if isinstance(raw, dict) else {}
    if not isinstance(message, dict):
        return ""
    for key in (
        "forum_topic_created",
        "forum_topic_closed",
        "forum_topic_reopened",
        "forum_topic_edited",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
    ):
        if key in message:
            return key
    return ""


_WALKCODE_TELEGRAM_COMMANDS = [
    {"command": "status", "description": "Show current session or runtime status"},
    {"command": "sessions", "description": "List active sessions in this chat"},
    {"command": "model", "description": "Show or switch the current session model"},
    {"command": "skills", "description": "Show agent skill support status"},
    {"command": "takeover", "description": "Request takeover for a TUI-origin session"},
    {"command": "commands", "description": "Show all WalkCode and agent commands"},
]


_CLAUDE_NATIVE_COMMANDS = [
    {"command": "add_dir", "description": "Claude: add an extra working directory"},
    {"command": "agents", "description": "Claude: manage subagent definitions"},
    {"command": "bug", "description": "Claude: report a bug"},
    {"command": "clear", "description": "Claude: clear the conversation"},
    {"command": "compact", "description": "Claude: compact conversation context"},
    {"command": "config", "description": "Claude: open configuration"},
    {"command": "cost", "description": "Claude: show token usage and cost"},
    {"command": "doctor", "description": "Claude: check local installation health"},
    {"command": "help", "description": "Claude: show command help"},
    {"command": "init", "description": "Claude: initialize project memory"},
    {"command": "login", "description": "Claude: authenticate"},
    {"command": "logout", "description": "Claude: sign out"},
    {"command": "mcp", "description": "Claude: manage MCP servers"},
    {"command": "memory", "description": "Claude: edit memory files"},
    {"command": "model", "description": "Claude: choose model"},
    {"command": "permissions", "description": "Claude: manage permissions"},
    {"command": "pr_comments", "description": "Claude: view pull request comments"},
    {"command": "release_notes", "description": "Claude: show release notes"},
    {"command": "resume", "description": "Claude: resume a conversation"},
    {"command": "review", "description": "Claude: request a code review"},
    {"command": "status", "description": "Claude: show account and system status"},
    {"command": "terminal_setup", "description": "Claude: configure terminal integration"},
    {"command": "vim", "description": "Claude: enter vim mode"},
]


_CODEX_NATIVE_COMMANDS = [
    {"command": "approvals", "description": "Codex: manage approval behavior"},
    {"command": "clear", "description": "Codex: clear conversation context"},
    {"command": "compact", "description": "Codex: compact conversation context"},
    {"command": "diff", "description": "Codex: show current diff"},
    {"command": "help", "description": "Codex: show command help"},
    {"command": "init", "description": "Codex: initialize project instructions"},
    {"command": "login", "description": "Codex: authenticate"},
    {"command": "logout", "description": "Codex: sign out"},
    {"command": "mcp", "description": "Codex: manage MCP servers"},
    {"command": "model", "description": "Codex: choose model"},
    {"command": "new", "description": "Codex: start a new conversation"},
    {"command": "quit", "description": "Codex: exit the session"},
    {"command": "review", "description": "Codex: request a code review"},
    {"command": "status", "description": "Codex: show session status"},
]


def _telegram_native_command_menu(agent: str) -> list[dict[str, str]]:
    native = _CLAUDE_NATIVE_COMMANDS if agent == "claude" else _CODEX_NATIVE_COMMANDS if agent == "codex" else []
    commands: list[dict[str, str]] = []
    seen: set[str] = set()
    for command in [*_WALKCODE_TELEGRAM_COMMANDS, *native]:
        name = str(command.get("command", "")).lstrip("/").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        commands.append({"command": name, "description": str(command.get("description", ""))})
    return commands[:100]


def _telegram_agent_command_aliases(agent: str) -> dict[str, str]:
    if agent == "claude":
        return {
            "add_dir": "add-dir",
            "release_notes": "release-notes",
            "terminal_setup": "terminal-setup",
        }
    return {}


def _telegram_agent_command_text(agent: str, text: str) -> str:
    stripped = str(text or "")
    if not stripped.strip().startswith("/"):
        return stripped
    leading = stripped[: len(stripped) - len(stripped.lstrip())]
    body = stripped.lstrip()
    command, sep, argument = body.partition(" ")
    name, suffix = command[1:].split("@", 1)[0], ""
    if "@" in command:
        suffix = "@" + command[1:].split("@", 1)[1]
    mapped = _telegram_agent_command_aliases(agent).get(name.lower())
    if not mapped:
        return stripped
    return f"{leading}/{mapped}{suffix}{sep}{argument}"


def _telegram_commands_help_text(agent: str) -> str:
    commands = _telegram_native_command_menu(agent)
    aliases = _telegram_agent_command_aliases(agent)
    lines = [f"Commands for {agent} bot:"]
    for item in commands:
        alias = aliases.get(item["command"])
        suffix = f" -> /{alias}" if alias else ""
        lines.append(f"/{item['command']}{suffix} - {item['description']}")
    lines.append("/repo <dir> <task> - Start a new task in an allowlisted workspace directory")
    return "\n".join(lines)


def _telegram_unknown_slash_command(inbound: Any) -> bool:
    text = str(getattr(inbound, "text", "") or "").strip()
    return bool(text.startswith("/") and not _telegram_bot_command(inbound) and not _agent_selector_command(inbound))


def _telegram_model_status_text(
    *,
    transport_kind: str,
    switching_available: bool,
    inventory: dict[str, Any],
) -> str:
    lines = [
        f"Current transport: {transport_kind}",
        f"Model switching: {'available' if switching_available else 'not available'}",
    ]
    source = str(inventory.get("source", "") or "")
    if source:
        lines.extend(["", f"Local model source: {source}"])
    current = str(inventory.get("current", "") or "")
    if current:
        lines.append(f"Current/default: {current}")
    provider = str(inventory.get("provider", "") or "")
    if provider:
        lines.append(f"Provider: {provider}")
    reasoning = str(inventory.get("reasoning", "") or "")
    if reasoning:
        lines.append(f"Reasoning: {reasoning}")
    models = list(inventory.get("models", []) or [])
    if models:
        lines.append("")
        lines.append("Available models:")
        for model in models[:20]:
            slug = str(model.get("slug", "") or "").strip()
            display = str(model.get("display_name", "") or "").strip()
            marker = str(model.get("marker", "") or "").strip()
            if not slug and not display:
                continue
            label = slug if not display or display == slug else f"{slug} - {display}"
            if marker:
                label = f"{label} ({marker})"
            lines.append(f"- {label}")
        if len(models) > 20:
            lines.append(f"... {len(models) - 20} more hidden by Telegram summary")
    notes = [str(item).strip() for item in inventory.get("notes", []) if str(item).strip()]
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def _local_model_inventory(config: ChannelNativeConfig, transport_kind: str) -> dict[str, Any]:
    if transport_kind == "claude_headless":
        return _claude_local_model_inventory(config)
    if transport_kind == "codex_app_server":
        return _codex_local_model_inventory(config)
    return {"source": "", "models": [], "notes": [f"No local model inventory for {transport_kind}."]}


def _claude_local_model_inventory(config: ChannelNativeConfig) -> dict[str, Any]:
    options = config.agent_options.get("claude", {})
    settings_path = str(options.get("settings", "") or "").strip()
    if not settings_path:
        # Fall back to the profile's own settings.json so a configured
        # CLAUDE_CONFIG_DIR surfaces its model env without extra wiring.
        config_dir = str(options.get("config_dir", "") or "").strip()
        if config_dir:
            candidate = Path(config_dir).expanduser() / "settings.json"
            if candidate.exists():
                settings_path = str(candidate)
    if not settings_path:
        return {
            "source": "",
            "models": [],
            "notes": ["No WALKCODE_CLAUDE_SETTINGS configured; model list is unavailable."],
        }
    path = Path(settings_path).expanduser()
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return {
            "source": str(path),
            "models": [],
            "notes": [f"Could not read Claude settings: {exc}"],
        }
    env = data.get("env") if isinstance(data, dict) else {}
    if not isinstance(env, dict):
        env = {}
    current = str(env.get("ANTHROPIC_MODEL", "") or "").strip()
    small_fast = str(env.get("ANTHROPIC_SMALL_FAST_MODEL", "") or "").strip()
    models: list[dict[str, str]] = []
    # Explicit picker list (settings.json "walkcode_model_choices"): entries are
    # either "slug" or {"slug","display_name"}. This is what /model offers; it
    # does not change routing (ANTHROPIC_MODEL stays the default).
    explicit = data.get("walkcode_model_choices") if isinstance(data, dict) else None
    if isinstance(explicit, list) and explicit:
        for entry in explicit:
            if isinstance(entry, str) and entry.strip():
                slug = entry.strip()
                models.append({"slug": slug, "display_name": slug})
            elif isinstance(entry, dict) and str(entry.get("slug", "")).strip():
                slug = str(entry["slug"]).strip()
                models.append(
                    {"slug": slug, "display_name": str(entry.get("display_name", "") or slug)}
                )
    else:
        if current:
            models.append({"slug": current, "display_name": current, "marker": "ANTHROPIC_MODEL"})
        if small_fast and small_fast != current:
            models.append(
                {
                    "slug": small_fast,
                    "display_name": small_fast,
                    "marker": "ANTHROPIC_SMALL_FAST_MODEL",
                }
            )
    notes = ["Claude list is derived from the configured settings file, not a live provider query."]
    return {
        "source": str(path),
        "current": current,
        "models": models,
        "notes": notes,
    }


def _codex_local_model_inventory(config: ChannelNativeConfig) -> dict[str, Any]:
    options = config.agent_options.get("codex", {})
    codex_home = _codex_home_path(str(options.get("codex_home", "") or ""))
    config_path = Path(str(options.get("config", "") or codex_home / "config.toml")).expanduser()
    cache_path = Path(str(options.get("models_cache", "") or codex_home / "models_cache.json")).expanduser()
    current = ""
    provider = ""
    reasoning = ""
    notes: list[str] = []
    try:
        codex_config = tomllib.loads(config_path.read_text())
        if isinstance(codex_config, dict):
            current = str(codex_config.get("model", "") or "").strip()
            provider = str(codex_config.get("model_provider", "") or "").strip()
            reasoning = str(codex_config.get("model_reasoning_effort", "") or "").strip()
    except Exception as exc:
        notes.append(f"Could not read Codex config: {exc}")

    models: list[dict[str, Any]] = []
    try:
        cache = json.loads(cache_path.read_text())
        raw_models = cache.get("models", []) if isinstance(cache, dict) else []
        for item in raw_models if isinstance(raw_models, list) else []:
            if not isinstance(item, dict):
                continue
            visibility = str(item.get("visibility", "") or "").lower()
            if visibility not in {"", "list"}:
                continue
            slug = str(item.get("slug", "") or "").strip()
            if not slug:
                continue
            marker = "current" if current and slug == current else ""
            models.append(
                {
                    "slug": slug,
                    "display_name": str(item.get("display_name", "") or "").strip(),
                    "marker": marker,
                    "priority": item.get("priority", 9999),
                }
            )
    except Exception as exc:
        notes.append(f"Could not read Codex model cache: {exc}")
    if current and all(model.get("slug") != current for model in models):
        models.insert(0, {"slug": current, "display_name": current, "marker": "current/custom", "priority": -1})
    models.sort(key=lambda item: (int(item.get("priority", 9999) or 9999), str(item.get("slug", ""))))
    notes.append("Codex list is derived from local config/cache, not a live provider query.")
    return {
        "source": f"{config_path}, {cache_path}",
        "current": current,
        "provider": provider,
        "reasoning": reasoning,
        "models": models,
        "notes": notes,
    }


def _telegram_message_is_empty(inbound: Any) -> bool:
    return not str(getattr(inbound, "text", "") or "").strip() and not list(
        getattr(inbound, "attachments", []) or []
    )


# One content key per kind in a Telegram message payload; used to name what
# the parser failed to understand when a message is dropped as empty.
_TELEGRAM_PAYLOAD_KEYS = (
    "text",
    "photo",
    "document",
    "sticker",
    "voice",
    "audio",
    "video",
    "video_note",
    "animation",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
)


def _inbound_message_type(inbound: Any) -> str:
    raw = getattr(inbound, "raw", None)
    if not isinstance(raw, dict):
        return ""
    event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    lark_type = str(message.get("message_type", "") or message.get("msg_type", "") or "")
    if lark_type:
        return lark_type
    tg_message = raw.get("message") if isinstance(raw.get("message"), dict) else {}
    for key in _TELEGRAM_PAYLOAD_KEYS:
        if key in tg_message:
            return key
    return ""


def _ignore_empty_inbound(inbound: Any) -> SubmitResult:
    """Confirm-and-drop for messages that parse to no text and no attachments.

    Quiet toward the user (an empty message needs no reply), never toward the
    operator: a payload the parser doesn't understand also lands here, and
    without a trace the visible symptom is "the bot ignored me" with no
    evidence in the ledger, the outbox, or the agent transcript.
    """
    from .channel_native import _log_degrade

    _log_degrade(
        "empty_inbound_ignored",
        channel=getattr(inbound, "channel_kind", ""),
        event_id=getattr(inbound, "event_id", ""),
        chat_id=getattr(inbound, "chat_id", ""),
        message_id=getattr(inbound, "message_id", ""),
        message_type=_inbound_message_type(inbound) or "unknown",
    )
    return SubmitResult(True, "empty_message_ignored")


def _agent_selector_rejected_message(*, configured_agent: str, requested_agent: str) -> str:
    if requested_agent == configured_agent:
        return (
            f"This bot is configured for {configured_agent}. "
            "Send the task text directly instead of using an agent selector command."
        )
    return (
        f"This bot is configured for {configured_agent}. "
        f"Use a separate {requested_agent} bot for {requested_agent} sessions."
    )


def _telegram_session_topic_name(agent: str, text: str) -> str:
    words = " ".join(str(text or "").strip().split())
    if not words:
        words = "new session"
    name = f"{agent}: {words}"
    return name[:128].strip() or f"{agent}: new session"


def _telegram_topic_url(chat_id: str, topic_id: str) -> str:
    chat = str(chat_id or "").strip()
    topic = str(topic_id or "").strip()
    if not (chat.startswith("-100") and topic.isdigit()):
        return ""
    return f"https://t.me/c/{chat[4:]}/{topic}"


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


def _transcript_model_from_payload(payload: dict[str, Any]) -> str:
    return _transcript_meta_from_payload(payload)[0]


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
    return hook_type in {"sync", "session-start", "user-prompt-submit", "pre-tool"}


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

    if transport_kind == "claude_headless":
        value = (
            payload.get("agent_session_id")
            or payload.get("claude_session_id")
            or payload.get("session_id")
            or payload.get("resume")
        )
        return {"agent_session_id": str(value)} if value else {}
    if transport_kind == "codex_app_server":
        value = (
            payload.get("thread_id")
            or payload.get("codex_thread_id")
            or payload.get("conversation_id")
            or payload.get("session_id")
        )
        return {"thread_id": str(value)} if value else {}
    value = payload.get("session_id") or payload.get("handle_id")
    return {"session_id": str(value)} if value else {}


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
        captured_ref = _external_tui_process_ref_from_entries(_tui_hook_process_tree_entries(payload))
        if captured_ref is not None:
            return {"controller_kind": "process", "process_ref": captured_ref}
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
    if transport_kind == "codex_app_server":
        return any(_command_is_codex_app_server_process(command) for command in commands)
    return False


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
    import math

    try:
        captured_at = float(payload.get("_walkcode_hook_captured_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(captured_at) or captured_at <= 0:
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
    identity = _resume_ref_identity(transport_kind, resume_ref)
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


def _resume_ref_identity(transport_kind: str, resume_ref: dict[str, Any]) -> str:
    if transport_kind == "claude_headless":
        return str(
            resume_ref.get("agent_session_id")
            or resume_ref.get("claude_session_id")
            or resume_ref.get("session_id")
            or resume_ref.get("resume")
            or ""
        )
    if transport_kind == "codex_app_server":
        return str(
            resume_ref.get("thread_id")
            or resume_ref.get("codex_thread_id")
            or resume_ref.get("conversation_id")
            or resume_ref.get("session_id")
            or ""
        )
    return str(resume_ref.get("session_id") or resume_ref.get("handle_id") or "")


def _tui_telegram_chat_id(endpoint: ChannelEndpointConfig) -> str:
    configured = str(endpoint.options.get("tui_chat_id", "") or "").strip()
    if configured:
        return configured
    allowed = tuple(str(item).strip() for item in endpoint.options.get("allowed_chat_ids", ()) if str(item).strip())
    if len(allowed) == 1:
        return allowed[0]
    return ""


def _tui_lark_chat_id(endpoint: ChannelEndpointConfig) -> str:
    # Same resolution rule as Telegram: explicit TUI chat wins, otherwise a
    # single-entry allowlist unambiguously names the observation chat.
    return _tui_telegram_chat_id(endpoint)


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
    """Read new assistant narration texts from a Claude transcript (ADR 0055).

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
        if str(entry.get("type", "")) != "assistant":
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


def _tui_hook_stops_session(hook_type: str) -> bool:
    return str(hook_type or "").strip().lower() in {"process-exit", "process-exited"}


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
    if transport_kind == "claude_headless":
        return bool(ref.get("agent_session_id") or ref.get("claude_session_id") or ref.get("session_id"))
    if transport_kind == "codex_app_server":
        return bool(ref.get("thread_id"))
    return bool(ref)


def _external_tui_process_ref(session: Any) -> dict[str, Any]:
    terminate_ref = Orchestrator._takeover_terminate_ref(session)
    if not isinstance(terminate_ref, dict) or not terminate_ref:
        return {}
    controller_kind, process_ref = Orchestrator._normalize_takeover_terminate_ref(terminate_ref)
    if controller_kind != "process" or not isinstance(process_ref, dict):
        return {}
    return process_ref


def _process_ref_is_running(process_ref: dict[str, Any]) -> bool:
    try:
        pid = int(process_ref.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 1 or pid == os.getpid():
        return False
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            env=_c_locale_env(),
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    stat = result.stdout.strip()
    return bool(stat) and "Z" not in stat.upper()


def _safe_error_message(exc: Exception, *secrets: str) -> str:
    message = str(exc)
    for secret in secrets:
        if secret:
            message = message.replace(str(secret), "<redacted>")
    return message


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


def _load_native_env(env: dict[str, str] | None) -> dict[str, str]:
    # No implicit fallback env file: with multiple profile instances on one
    # machine, a silent default would misroute hooks/CLI runs to the wrong
    # instance. Every hook command and launchd plist must set WALKCODE_ENV_FILE.
    base = dict(os.environ) if env is None else dict(env)
    env_file = base.get("WALKCODE_ENV_FILE", "")
    if not env_file:
        return base
    path = Path(env_file).expanduser()
    merged = dict(base)
    merged.update(_read_env_file(path))
    merged["WALKCODE_ENV_FILE"] = str(path)
    return merged


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key:
            values[key.strip()] = value.strip()
    return values


def _describe_e2e_gates(gates: ChannelNativeE2EGates) -> dict[str, dict[str, Any]]:
    return {
        name: {
            "enabled": result.enabled,
            "missing": list(result.missing),
            "reason": result.reason,
        }
        for name, result in gates.all().items()
    }


def _describe_tui_hook_status(agent: str, codex_home: str = "") -> dict[str, Any]:
    normalized = _normalize_tui_agent(agent)
    if normalized != "codex":
        return {
            "agent": normalized or agent,
            "checked": False,
            "reason": "codex_hooks_only",
        }
    path = _codex_home_path(codex_home) / "hooks.json"
    if not path.exists():
        return {
            "agent": "codex",
            "checked": True,
            "configured": False,
            "ok": False,
            "path": str(path),
            "missing": list(CODEX_TUI_REQUIRED_HOOKS),
            "reason": "hooks_json_missing",
        }
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "agent": "codex",
            "checked": True,
            "configured": False,
            "ok": False,
            "path": str(path),
            "missing": list(CODEX_TUI_REQUIRED_HOOKS),
            "reason": f"hooks_json_invalid:{type(exc).__name__}",
        }
    hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
    if not isinstance(hooks, dict):
        hooks = {}
    missing = [name for name in CODEX_TUI_REQUIRED_HOOKS if name not in hooks]
    command_missing = [
        name
        for name in CODEX_TUI_REQUIRED_HOOKS
        if name in hooks and not _codex_hook_event_runs_walkcode_native(hooks.get(name))
    ]
    return {
        "agent": "codex",
        "checked": True,
        "configured": True,
        "ok": not missing and not command_missing,
        "path": str(path),
        "missing": missing,
        "command_missing": command_missing,
        "required": list(CODEX_TUI_REQUIRED_HOOKS),
    }


def _codex_hook_event_runs_walkcode_native(event_config: Any) -> bool:
    if not isinstance(event_config, list):
        return False
    for item in event_config:
        if not isinstance(item, dict):
            continue
        hooks = item.get("hooks", [])
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if not isinstance(hook, dict):
                continue
            command = str(hook.get("command", ""))
            if "walkcode native hook" in command and "--agent codex" in command:
                return True
    return False


def _format_status(status: dict[str, Any]) -> str:
    channel = status.get("channel", {})
    runtime_status = status.get("runtime_status", {})
    lines = [
        "channel-native V3 runtime",
        f"profile: {status.get('profile', '') or '-'}",
        f"channel: {channel.get('kind', '')}",
        f"agent: {status.get('agent', '')}",
        f"state_path: {status.get('state_path', '')}",
        f"cwd: {status.get('cwd', '')}",
        "runtime_status:",
        f"  - service_label={runtime_status.get('service_label', '-') or '-'} "
        f"loaded={runtime_status.get('service_loaded')}",
        f"  - service_state={runtime_status.get('service_state', '-')}",
        "channel_status:",
    ]
    lines.append(f"  - live_ingress={channel.get('live_ingress')} configured={channel.get('configured')}")
    item = status.get("agent_status", {})
    lines.append(f"agent_status: available={item.get('available')}")
    daemon = status.get("claude_daemon", {})
    if daemon:
        if daemon.get("enabled"):
            lines.append(
                "claude_daemon: enabled=True "
                f"socket_present={daemon.get('socket_present')} "
                f"spawn_mode={daemon.get('spawn_mode', '-')} "
                f"list_adopt={daemon.get('list_adopt', '-')} "
                f"spawner_installed={daemon.get('daemon_spawner_installed')}"
            )
        else:
            lines.append(f"claude_daemon: enabled=False reason={daemon.get('reason', '-')}")
    if "handoff_continue" in status:
        lines.append(f"handoff_continue: {status.get('handoff_continue') or 'auto'}")
    hook_status = status.get("tui_hook_status", {})
    if hook_status.get("checked"):
        missing = ",".join(hook_status.get("missing") or []) or "-"
        command_missing = ",".join(hook_status.get("command_missing") or []) or "-"
        lines.append(
            "tui_hook_status: "
            f"ok={hook_status.get('ok')} path={hook_status.get('path', '-')}"
        )
        lines.append(f"  - missing={missing}")
        lines.append(f"  - command_missing={command_missing}")
    if status.get("e2e_gates"):
        lines.append("e2e_gates:")
        for kind, item in status.get("e2e_gates", {}).items():
            missing = ",".join(item.get("missing") or []) or "-"
            reason = item.get("reason") or "-"
            lines.append(
                f"  - {kind}: enabled={item.get('enabled')} missing={missing} reason={reason}"
            )
    return "\n".join(lines)


def _launchd_service_label(channel_kind: str, agent: str, profile: str = "") -> str:
    value = str(agent or "").strip().lower()
    if value not in {"claude", "codex"}:
        return ""
    if profile:
        return f"com.walkcode.{profile}-{value}"
    if channel_kind != "telegram":
        return ""
    return f"com.walkcode.telegram-{value}"


def _lark_tenant_token_self_check(app_id: str, app_secret: str, domain: str) -> dict[str, Any]:
    """SDK-free tenant_access_token probe used by `walkcode native debug lark`.

    Only reports reachability and credential validity; the token value itself
    is never included in the report.
    """
    import urllib.error
    import urllib.request

    url = f"{domain.rstrip('/')}/open-apis/auth/v3/tenant_access_token/internal"
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"ok": False, "reason": f"http_{exc.code}"}
    except Exception as exc:
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    code = int(payload.get("code", -1))
    if code != 0:
        return {"ok": False, "reason": f"lark_code_{code}: {payload.get('msg', '')}"}
    return {"ok": True, "expire_in": payload.get("expire", 0)}


def _format_lark_diagnosis(report: dict[str, Any]) -> str:
    channel = report.get("channel", {})
    token = report.get("tenant_token", {})
    sdk = report.get("sdk", {})
    lines = [
        "lark ingress diagnosis",
        f"app_id_prefix: {channel.get('app_id_prefix', '-')}",
        f"openapi_domain: {channel.get('openapi_domain', '-')}",
        f"tenant_token: ok={token.get('ok')}"
        + (f" reason={token.get('reason')}" if not token.get("ok") else ""),
        f"sdk_installed: {sdk.get('installed')}",
        f"allowed_chat_ids: {','.join(report.get('allowed_chat_ids', [])) or '-'}",
        f"allowed_open_ids: {','.join(report.get('allowed_open_ids', [])) or '-'}",
    ]
    hint = sdk.get("hint", "")
    if hint:
        lines.append(f"hint: {hint}")
    return "\n".join(lines)


def _format_telegram_diagnosis(report: dict[str, Any]) -> str:
    channel = report.get("channel", {})
    bot = report.get("bot", {})
    webhook = report.get("webhook", {})
    pending = report.get("pending_updates", {})
    lines = [
        "telegram ingress diagnosis",
        f"bot: ok={bot.get('ok')} username={bot.get('username', '')}",
        (
            "channel: "
            f"polling_enabled={channel.get('polling_enabled')} "
            f"allowlist_configured={channel.get('allowlist_configured')} "
            f"allowlist_count={channel.get('allowlist_count')} "
            f"allowlist_matches_existing_session={channel.get('allowlist_matches_existing_session')}"
        ),
        (
            "webhook: "
            f"has_url={webhook.get('has_url')} "
            f"pending_update_count={webhook.get('pending_update_count')} "
            f"last_error_present={webhook.get('last_error_present')}"
        ),
        f"pending_updates: count={pending.get('count')} limit={pending.get('limit')}",
        f"safe_to_run_serve_once: {report.get('safe_to_run_serve_once')}",
    ]
    for item in pending.get("items", []):
        lines.append(
            "  - "
            f"index={item.get('index')} "
            f"kind={item.get('event_kind')} "
            f"chat_allowed={item.get('chat_allowed')} "
            f"known_chat={item.get('chat_matches_existing_session')} "
            f"text_present={item.get('text_present')} "
            f"attachments={item.get('attachment_count')}"
        )
    for warning in report.get("warnings", []):
        lines.append(f"warning: {warning}")
    note = report.get("note")
    if note:
        lines.append(f"note: {note}")
    return "\n".join(lines)


__all__ = ["ChannelNativeRuntime", "run_native_cli"]
