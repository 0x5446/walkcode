"""Claude Code daemon control-plane client for multi-UI session sync.

Protocol grounding: ``docs/design/daemon-appserver-protocol-reference.md``
(reverse-engineered and live-verified 2026-07-04 against Claude Code v2.1.201,
``proto: 1``). Design: ``docs/design/claude-daemon-multi-ui-sync.md`` /
ADR 0046.

Layering:

- ``ClaudeDaemonClient`` speaks the raw ndjson protocol over the per-profile
  unix socket. One connection per request; ``subscribe`` holds its connection
  open and yields pushed events until ``settled`` or disconnect.
- ``ClaudeDaemonTransport`` adapts the client to the ``AgentTransport``
  protocol: ``submit_turn`` -> ``reply`` (text injected into the running
  session as if typed in the TUI). Content rendering stays on the TUI hook
  pipeline, so ``events()`` is intentionally empty; the runtime's subscribe
  watcher consumes ``state`` patches separately.

The protocol is vendor-experimental: every entry point degrades to
``TransportUnavailable`` so callers can fall back to the hook/takeover path.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator

from . import (
    CapabilityUnsupported,
    ClaudeHeadlessTransport,
    ControlResult,
    LaunchSpec,
    ResumeSpec,
    TransportCapabilities,
    TransportHandle,
    TransportUnavailable,
    TurnInput,
)
from . import claude_gate

CLAUDE_DAEMON_PROTO = 1

# Daemon replies "job is not accepting replies" while a turn is mid-flight;
# these codes mean "fall back to takeover", not "daemon is broken".
REPLY_REJECTED_CODES = frozenset({"ENOJOB", "ENOREPLY", "ERESPAWNING"})


def _resolved_config_dir(config_dir: str = "") -> str:
    resolved = str(Path(config_dir or "~/.claude").expanduser())
    return resolved.rstrip("/") or "/"


def claude_daemon_socket_path(config_dir: str = "", *, uid: int | None = None) -> str:
    """Derive the control socket path for a profile's daemon.

    Live-verified: the per-daemon directory hash is
    ``sha256(<expanded CLAUDE_CONFIG_DIR, no trailing slash>)[:8]``
    (e.g. ``sha256("/Users/alpha/.claude-profiles/work")[:8] == "19e5f12f"``).
    """
    digest = hashlib.sha256(_resolved_config_dir(config_dir).encode("utf-8")).hexdigest()[:8]
    owner = os.getuid() if uid is None else uid
    return f"/tmp/cc-daemon-{owner}/{digest}/control.sock"


def claude_daemon_control_key_path(config_dir: str = "") -> str:
    return str(Path(_resolved_config_dir(config_dir)) / "daemon" / "control.key")


def claude_daemon_short_id(value: Any) -> str:
    """Normalize a session UUID / short id to the daemon's 8-hex job id."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    candidate = text.split("-", 1)[0] if "-" in text else text[:8]
    candidate = candidate[:8]
    if len(candidate) != 8:
        return ""
    try:
        int(candidate, 16)
    except ValueError:
        return ""
    return candidate


def claude_daemon_short_from_resume_ref(resume_ref: dict[str, Any]) -> str:
    if not isinstance(resume_ref, dict):
        return ""
    nested = resume_ref.get("resume_ref")
    if isinstance(nested, dict):
        short = claude_daemon_short_from_resume_ref(nested)
        if short:
            return short
    for key in ("short", "agent_session_id", "claude_session_id", "session_id"):
        short = claude_daemon_short_id(resume_ref.get(key))
        if short:
            return short
    return ""


class ClaudeDaemonError(RuntimeError):
    """Structured daemon error response (``{"ok": false, "code": ...}``)."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class ClaudeDaemonClient:
    def __init__(
        self,
        *,
        config_dir: str = "",
        socket_path: str = "",
        control_key_path: str = "",
        request_timeout: float = 10.0,
    ):
        self.config_dir = _resolved_config_dir(config_dir)
        self.socket_path = socket_path or claude_daemon_socket_path(config_dir)
        self.control_key_path = control_key_path or claude_daemon_control_key_path(config_dir)
        self.request_timeout = request_timeout

    # -- protocol operations ------------------------------------------------

    async def ping(self) -> dict[str, Any]:
        return await self._request({"proto": CLAUDE_DAEMON_PROTO, "op": "ping"})

    async def probe(self) -> dict[str, Any]:
        """Ping and enforce the protocol version gate (ADR 0046)."""
        response = await self.ping()
        proto = response.get("proto")
        if proto != CLAUDE_DAEMON_PROTO:
            raise TransportUnavailable(
                f"Claude daemon protocol mismatch: server proto {proto!r}, "
                f"client supports {CLAUDE_DAEMON_PROTO}"
            )
        return response

    async def list_jobs(self) -> list[dict[str, Any]]:
        response = await self._request({"proto": CLAUDE_DAEMON_PROTO, "op": "list"})
        jobs = response.get("jobs", [])
        if not isinstance(jobs, list):
            return []
        return [job for job in jobs if isinstance(job, dict)]

    async def has(self, short: str) -> dict[str, Any]:
        return await self._request({"proto": CLAUDE_DAEMON_PROTO, "op": "has", "short": short})

    async def job_ready(self, short: str) -> bool:
        try:
            status = await self.has(short)
        except (TransportUnavailable, ClaudeDaemonError):
            return False
        return bool(status.get("alive")) and bool(status.get("present")) and bool(status.get("ready"))

    async def reply(self, short: str, text: str) -> dict[str, Any]:
        return await self._request(
            {
                "proto": CLAUDE_DAEMON_PROTO,
                "op": "reply",
                "auth": self._auth(),
                "short": short,
                "text": text,
            }
        )

    async def kill(self, short: str, *, signal: str = "SIGTERM") -> dict[str, Any]:
        return await self._request(
            {
                "proto": CLAUDE_DAEMON_PROTO,
                "op": "kill",
                "auth": self._auth(),
                "short": short,
                "signal": signal,
            }
        )

    async def subscribe(self, short: str, *, tail: int = 0) -> AsyncIterator[dict[str, Any]]:
        """Yield pushed events (snapshot / stream / state / settled).

        The generator ends after ``settled`` or when the daemon drops the
        connection; a protocol-level error response raises instead.
        """
        reader, writer = await self._connect()
        try:
            request: dict[str, Any] = {
                "proto": CLAUDE_DAEMON_PROTO,
                "op": "subscribe",
                "short": short,
            }
            if tail:
                request["tail"] = tail
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    return
                message = _raise_on_error(_decode_line(line))
                yield message
                if message.get("type") == "settled":
                    return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    # -- plumbing ------------------------------------------------------------

    def _auth(self) -> str:
        try:
            key = Path(self.control_key_path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TransportUnavailable(
                f"Claude daemon control key unavailable: {self.control_key_path}"
            ) from exc
        if not key:
            raise TransportUnavailable(
                f"Claude daemon control key is empty: {self.control_key_path}"
            )
        return key

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            # subscribe snapshots can carry a whole scrollback in one ndjson
            # line (live-observed >64KB, which overflows asyncio's default
            # readline limit and kills the watcher in a reconnect loop).
            return await asyncio.open_unix_connection(self.socket_path, limit=16 * 1024 * 1024)
        except OSError as exc:
            raise TransportUnavailable(
                f"Claude daemon socket unavailable: {self.socket_path}"
            ) from exc

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        reader, writer = await self._connect()
        try:
            writer.write(json.dumps(payload).encode("utf-8") + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self.request_timeout)
            except asyncio.TimeoutError as exc:
                raise TransportUnavailable("Claude daemon request timed out") from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not line:
            raise TransportUnavailable("Claude daemon closed the connection without a response")
        return _raise_on_error(_decode_line(line))


def _decode_line(line: bytes) -> dict[str, Any]:
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise TransportUnavailable("Claude daemon returned invalid JSON") from exc
    if not isinstance(message, dict):
        raise TransportUnavailable("Claude daemon returned non-object JSON")
    return message


def _raise_on_error(message: dict[str, Any]) -> dict[str, Any]:
    if message.get("ok") is False:
        raise ClaudeDaemonError(
            str(message.get("code", "") or "EUNKNOWN"),
            str(message.get("error", "") or ""),
        )
    return message


class ClaudeDaemonTransport:
    kind = "claude_daemon"

    def __init__(
        self,
        *,
        config_dir: str = "",
        client: ClaudeDaemonClient | None = None,
        gate_state_path: str | Path = "",
    ):
        self.config_dir = _resolved_config_dir(config_dir)
        self.client = client or ClaudeDaemonClient(config_dir=config_dir)
        self.gate_state_path = Path(gate_state_path) if gate_state_path else None
        # Runtime-installed observer: (rid, decision) after a decision file
        # lands, so the runtime can learn e.g. session-scoped always_allow.
        self.on_gate_decision: Any = None

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            structured_input=True,
            # Turn content still flows through the TUI hook pipeline; this
            # transport carries no output event stream of its own.
            structured_output=False,
            # The daemon's own permission-response op is a shell (auth-checks
            # then drops the payload — live-verified). Decisions travel over
            # the PreToolUse gate spool instead: the blocking hook polls
            # decisions/<rid>.json that these callbacks write (ADR 0046 v2).
            permission_callback=self.gate_state_path is not None,
            ask_user_question=self.gate_state_path is not None,
            interrupt=False,
            set_model=False,
            set_permission_mode=False,
            checkpoint_rewind=False,
            resume_after_complete=True,
            resume_active_turn=False,
            multi_client_observe=True,
            multi_client_write=True,
            external_tui_takeover=False,
            requires_single_writer=False,
        )

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        raise CapabilityUnsupported(
            "Claude daemon transport cannot create sessions (dispatch not implemented); "
            "new sessions go through claude_headless"
        )

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        short = claude_daemon_short_from_resume_ref(spec.resume_ref)
        if not short:
            raise CapabilityUnsupported("Claude daemon resume requires an agent session id")
        if not await self.client.job_ready(short):
            raise TransportUnavailable(f"Claude daemon job {short} is not alive and ready")
        agent_session_id = str(
            spec.resume_ref.get("agent_session_id")
            or spec.resume_ref.get("claude_session_id")
            or spec.resume_ref.get("session_id")
            or ""
        )
        return TransportHandle(
            handle_id=f"claude-daemon-{short}",
            transport_kind=self.kind,
            ref={
                "short": short,
                "agent_session_id": agent_session_id,
                "cwd": spec.cwd,
            },
        )

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        short = str(handle.ref.get("short", "")) or claude_daemon_short_from_resume_ref(handle.ref)
        if not short:
            raise CapabilityUnsupported("Claude daemon reply requires a job short id")
        text = ClaudeHeadlessTransport._compose_turn_text(turn)
        await self.client.reply(short, text)

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        if self.gate_state_path is None:
            raise CapabilityUnsupported("Claude gate spool is not configured for this transport")
        payload = {
            "kind": claude_gate.KIND_PERMISSION,
            "action": str((decision or {}).get("action", "") or "deny"),
        }
        reason = str((decision or {}).get("reason", "") or "")
        if reason:
            payload["reason"] = reason
        claude_gate.write_decision(self.gate_state_path, rid, payload)
        self._notify_gate_decision(rid, payload)

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        if self.gate_state_path is None:
            raise CapabilityUnsupported("Claude gate spool is not configured for this transport")
        payload = {
            "kind": claude_gate.KIND_ASK_USER,
            "action": "answers",
            "answers": {
                str(key): value
                for key, value in (answers or {}).items()
                if not str(key).startswith("_")
            },
        }
        claude_gate.write_decision(self.gate_state_path, rid, payload)
        self._notify_gate_decision(rid, payload)

    def _notify_gate_decision(self, rid: str, decision: dict[str, Any]) -> None:
        if self.on_gate_decision is None:
            return
        with contextlib.suppress(Exception):
            self.on_gate_decision(rid, dict(decision))

    async def interrupt(self, handle: TransportHandle, reason: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_daemon")

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        short = str(handle.ref.get("short", ""))
        if not short:
            return ControlResult(False, "missing_short_id")
        try:
            await self.client.kill(short)
        except (TransportUnavailable, ClaudeDaemonError) as exc:
            return ControlResult(False, f"{type(exc).__name__}: {exc}")
        return ControlResult(True, state="killed")

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_daemon")

    async def set_permission_mode(self, handle: TransportHandle, mode: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_daemon")

    async def rewind_checkpoint(self, handle: TransportHandle, checkpoint_id: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_daemon")

    def events(self, handle: TransportHandle) -> list[Any]:
        # Content events come from the hook pipeline; state sync comes from the
        # runtime's subscribe watcher. An empty stream keeps the orchestrator's
        # post-submit drain a no-op instead of a protocol error.
        return []
