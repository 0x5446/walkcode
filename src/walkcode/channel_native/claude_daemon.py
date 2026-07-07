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
import re
import sys
import time
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

# `claude --bg` spawn (ADR 0048): CLI call + list/ready settle budgets. The
# live-measured happy path is <2s end to end; the generous ceilings only bound
# the fallback-to-headless latency when the daemon is wedged.
SPAWN_BG_CLI_TIMEOUT_SECONDS = 60.0
SPAWN_BG_READY_TIMEOUT_SECONDS = 20.0
SPAWN_BG_POLL_SECONDS = 0.5

# `claude --bg` colors its stdout even when piped under FORCE_COLOR-style
# environments (live-observed), so the short id is parsed ANSI-stripped.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_BACKGROUNDED_RE = re.compile(r"backgrounded · ([0-9a-f]{8})")


def parse_backgrounded_short(output: str) -> str:
    """Extract the job short id from `claude --bg` output ('' if absent)."""
    match = _BACKGROUNDED_RE.search(_ANSI_RE.sub("", output or ""))
    return match.group(1) if match else ""

# -- keystroke injection (v3 true dual-surface, ADR 0046 v3) ------------------
#
# A second attacher's raw PTY bytes drive the native permission /
# AskUserQuestion dialogs exactly like a keyboard. All sequences below were
# live-verified on Claude Code 2.1.201 (mapping table: "交互闭环 v3" in
# docs/design/claude-daemon-multi-ui-sync.md). Non-obvious dialog behaviors
# the mapping relies on:
#
# - digit semantics vary by dialog kind: single-select / multi-question
#   digits confirm in one press (no Enter); multiSelect digits only toggle;
# - Enter is context-dependent and is only ever sent after non-empty Other
#   free text (Enter on an empty Other item cancels the whole dialog);
# - multiSelect submits via right-arrow to the Submit page then "1";
#   multi-question auto-advances per answer and ends on the same Submit page.

KeyFrame = tuple[bytes, float]  # (raw PTY bytes, delay after writing them)

ATTACH_SETTLE_SECONDS = 0.8
INTER_KEY_DELAY_SECONDS = 0.15
DEFAULT_INJECT_ATTACH_ID = "walkcode-injector"

# Persistent observer attach (ADR 0048 live-E2E finding, 2026-07-07): the
# daemon only publishes state patches (tempo/needs) to its ledger while the
# job has at least one attacher. A never-attached job — exactly what a
# Feishu-spawned or detached session is — renders its dialogs in the PTY but
# the supervisor's list/subscribe view stays frozen, so the notify-gate probe
# concludes "no dialog" and the dual-surface card is never posted. The fix is
# the ADR's own sentence made literal: walkcode holds one long-lived attach
# per watched job ("第一 attacher 从终端变成注入连接"). Size matches the
# pty-host spawn default (200x50) so our handshake never changes the layout.
OBSERVER_ATTACH_ID = "walkcode-observer"
OBSERVER_ATTACH_COLS = 200
OBSERVER_ATTACH_ROWS = 50
OBSERVER_RECONNECT_SECONDS = 5.0

# Post-injection verify: the dialog must resolve (needs cleared / changed /
# tempo off blocked) within this window, or the card degrades to "answer in
# the terminal" — no blind retries (a second pass could double-press).
GATE_INJECT_VERIFY_TIMEOUT_SECONDS = 3.0
GATE_INJECT_VERIFY_POLL_SECONDS = 0.5
# Terminal-resolved rids kept as tombstones so a late card click flips to
# "answered in the terminal" instead of pretending the click took effect.
_NOTIFY_TOMBSTONE_LIMIT = 256

# needs format (live-verified): "approve <Tool>: <detail>". The tool token is
# matched EXACTLY — substring matching would let an "Edit" card drive a
# "MultiEdit"/"NotebookEdit" dialog (deep-review finding, 9-dimension hit).
_APPROVE_NEEDS_RE = re.compile(r"^approve\s+([^\s:]+)\s*(?::|$)")

# Sentinel for "the daemon control plane could not be queried" — distinct
# from None ("queried fine, job not in the list"). Folding the two into None
# turned probe outages into fake injection success (deep-review finding).
_JOB_STATE_UNAVAILABLE = object()

_ESC = b"\x1b"
_RIGHT_ARROW = b"\x1b[C"  # multiSelect: option page -> Submit page
# Dialog slots are picked with a single digit key; anything past 9 has no key.
_MAX_DIGIT_SLOT = 9


def keys_for_permission(action: str) -> list[KeyFrame] | None:
    """Map a Feishu permission decision to native-dialog keystrokes.

    allow / always_allow -> ``1`` (allow once). The always-allow item's digit
    and wording shift with tool/context (2- vs 3-option layouts), so
    persistence stays with the runtime's session-scoped always_allow memory
    (v2 semantics) instead of a fragile dialog position.

    deny -> ESC. The dialog's "No, and tell Claude what to do differently"
    item carries the ``(esc)`` binding, making ESC the only
    position-independent reject; the No digit moves between layouts. The
    tool call is rejected and the turn returns to idle.

    Returns None for unknown actions: the caller degrades to "answer in the
    terminal" rather than guessing a key.
    """
    normalized = str(action or "").strip().lower()
    if normalized in {"allow", "allow_once", "accept", "acceptforsession", "always_allow"}:
        # Digit + Enter (2026-07-07 live finding): a digit that lands on the
        # ALREADY-highlighted slot only re-selects in select-style dialogs;
        # Enter confirms it. When the digit did act on its own, the trailing
        # Enter falls on the post-dialog composer and is a no-op — a new
        # dialog cannot render within one INTER_KEY_DELAY (model latency is
        # seconds), so the Enter can never confirm the wrong dialog.
        return [(b"1", INTER_KEY_DELAY_SECONDS), (b"\r", INTER_KEY_DELAY_SECONDS)]
    if normalized == "deny":
        # ESC only — never append Enter here: if the ESC were ever swallowed,
        # a trailing Enter would confirm the highlighted item, i.e. ALLOW.
        return [(_ESC, INTER_KEY_DELAY_SECONDS)]
    return None


def keys_for_ask_answer(
    tool_input: dict[str, Any],
    answers: dict[Any, Any],
) -> list[KeyFrame] | None:
    """Map AskUserQuestion answers to native-dialog keystrokes.

    ``answers`` follows the gate/card convention: keyed by question index
    (int, or str after a JSON round trip); values are an option label, a list
    of labels (multiSelect), or free text (Other).

    Returns None when the combination falls outside the live-verified support
    matrix — the caller then degrades the card to "answer in the terminal"
    (the native dialog is rendered and usable; None is a routing verdict, not
    an error). Verified forms: single question single-select; single question
    Other free text (digit locates the "Type something." slot, the text is
    typed inline, Enter confirms — empty text would cancel, hence -> None);
    single question multiSelect; multi-question with every answer a plain
    option pick. Unverified -> None: free text or multiSelect inside a
    multi-question dialog, digit slots past 9.
    """
    questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
    if not isinstance(questions, list) or not questions:
        return None
    if not all(isinstance(question, dict) for question in questions):
        return None
    answers = answers if isinstance(answers, dict) else {}

    if len(questions) == 1:
        question = questions[0]
        labels = _question_option_labels(question)
        value = _answer_for_index(answers, 0)
        if value is None:
            return None
        if _question_is_multi_select(question):
            return _frames_for_multi_select(labels, value)
        return _frames_for_single_select(labels, value)

    frames: list[KeyFrame] = []
    for index, question in enumerate(questions):
        if _question_is_multi_select(question):
            return None
        value = _answer_for_index(answers, index)
        if value is None or isinstance(value, (list, tuple)):
            return None
        slot = _option_slot(_question_option_labels(question), value)
        if slot is None:
            # Free text inside a multi-question dialog is unverified.
            return None
        frames.append((str(slot).encode("ascii"), INTER_KEY_DELAY_SECONDS))
    frames.append((b"1", INTER_KEY_DELAY_SECONDS))  # Submit answers page
    frames.append((b"\r", INTER_KEY_DELAY_SECONDS))  # confirm if "1" was the highlight
    return frames


def _question_option_labels(question: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    options = question.get("options", [])
    for option in options if isinstance(options, list) else []:
        if isinstance(option, dict):
            labels.append(str(option.get("label", option.get("value", "")) or "").strip())
        else:
            labels.append(str(option).strip())
    return labels


def _question_is_multi_select(question: dict[str, Any]) -> bool:
    return bool(question.get("multiSelect") or question.get("allow_multiple"))


def _answer_for_index(answers: dict[Any, Any], index: int) -> Any:
    value = answers.get(index)
    if value is None:
        value = answers.get(str(index))
    return value


def _option_slot(labels: list[str], value: Any) -> int | None:
    text = str(value).strip()
    for position, label in enumerate(labels, start=1):
        if label == text:
            return position if position <= _MAX_DIGIT_SLOT else None
    return None


def _frames_for_single_select(labels: list[str], value: Any) -> list[KeyFrame] | None:
    if isinstance(value, (list, tuple)):
        return None
    slot = _option_slot(labels, value)
    if slot is not None:
        # Digit + Enter (2026-07-07 live finding): a digit on a slot that is
        # NOT the current highlight selects and confirms in one stroke, but a
        # digit on the ALREADY-highlighted slot (e.g. answering "option 1"
        # while ❯ sits on 1) only re-selects — the dialog stays open waiting
        # for Enter. The trailing Enter makes the sequence universal: it
        # confirms in the second case and is a no-op composer keypress in the
        # first (no new dialog can render within one INTER_KEY_DELAY).
        return [
            (str(slot).encode("ascii"), INTER_KEY_DELAY_SECONDS),
            (b"\r", INTER_KEY_DELAY_SECONDS),
        ]
    text = _sanitize_other_text(value)
    if not text:
        return None
    other_slot = len(labels) + 1
    if other_slot > _MAX_DIGIT_SLOT:
        return None
    return [
        (str(other_slot).encode("ascii"), INTER_KEY_DELAY_SECONDS),
        (text.encode("utf-8"), INTER_KEY_DELAY_SECONDS),
        (b"\r", INTER_KEY_DELAY_SECONDS),
    ]


def _frames_for_multi_select(labels: list[str], value: Any) -> list[KeyFrame] | None:
    picked = value if isinstance(value, (list, tuple)) else [value]
    frames: list[KeyFrame] = []
    seen: set[int] = set()
    for item in picked:
        slot = _option_slot(labels, item)
        if slot is None:
            # Free text with multiSelect is unverified; a label outside the
            # option list would toggle the wrong item.
            return None
        if slot in seen:
            # A duplicate digit would toggle the option back off.
            continue
        seen.add(slot)
        frames.append((str(slot).encode("ascii"), INTER_KEY_DELAY_SECONDS))
    if not frames:
        return None
    frames.append((_RIGHT_ARROW, INTER_KEY_DELAY_SECONDS))
    frames.append((b"1", INTER_KEY_DELAY_SECONDS))  # "1. Submit answers"
    # Same universal digit+Enter rule as single-select: "1" on the Submit
    # page is the highlighted default, so it may need the confirm stroke.
    frames.append((b"\r", INTER_KEY_DELAY_SECONDS))
    return frames


def _sanitize_other_text(value: Any) -> str:
    # The text is typed into the dialog's inline editor: newlines would act
    # as Enter mid-input and other control bytes could break the dialog, so
    # whitespace collapses to single spaces and C0/DEL bytes are dropped.
    collapsed = " ".join(str(value).split())
    return "".join(ch for ch in collapsed if ord(ch) >= 0x20 and ch != "\x7f").strip()


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

    async def job_alive(self, short: str) -> bool | None:
        """Existence probe for stop guards: True/False definitive, None unknown.

        Unlike ``job_ready`` this does not require ``ready`` (a worker still
        starting up is alive) and does not fold probe failures into "dead" —
        a socket blip must not let stop paths end a running session.
        """
        try:
            status = await self.has(short)
        except (TransportUnavailable, ClaudeDaemonError):
            return None
        return bool(status.get("alive")) and bool(status.get("present"))

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

    async def attach_send_keys(
        self,
        short: str,
        frames: list[KeyFrame],
        *,
        settle: float = ATTACH_SETTLE_SECONDS,
        cols: int = 120,
        rows: int = 40,
        attach_id: str = DEFAULT_INJECT_ATTACH_ID,
    ) -> None:
        """Inject keystrokes into a job's PTY via a second attacher.

        One connection per call: attach handshake -> settle (lets the daemon
        finish replaying the screen before keys land) -> write each frame with
        its trailing delay -> close. The daemon supports multi-attach, so this
        coexists with a human terminal attached to the same job; injected
        bytes reach the worker's terminal input handler exactly like typed
        keys (live-verified 2026-07-06, protocol reference §1.6.6).

        The PTY output stream that follows the handshake is drained in the
        background and discarded — dialog-state verification happens over
        ``subscribe`` (needs/tempo), not by parsing ANSI here.
        """
        frames = [frame for frame in (frames or []) if frame[0]]
        if not frames:
            return
        reader, writer = await self._connect()
        try:
            request = {
                "proto": CLAUDE_DAEMON_PROTO,
                "op": "attach",
                "auth": self._auth(),
                "short": short,
                "cols": cols,
                "rows": rows,
                "attachId": attach_id,
            }
            try:
                writer.write(json.dumps(request).encode("utf-8") + b"\n")
                await writer.drain()
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=self.request_timeout)
                except asyncio.TimeoutError as exc:
                    raise TransportUnavailable("Claude daemon attach handshake timed out") from exc
                if not line:
                    raise TransportUnavailable("Claude daemon closed during attach handshake")
                _raise_on_error(_decode_line(line))
                drain_task = asyncio.create_task(self._drain_pty_stream(reader))
                try:
                    if settle > 0:
                        await asyncio.sleep(settle)
                    for data, delay in frames:
                        writer.write(data)
                        await writer.drain()
                        if delay > 0:
                            await asyncio.sleep(delay)
                finally:
                    drain_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await drain_task
            except OSError as exc:
                # BrokenPipe / ConnectionReset mid-write must degrade like any
                # other control-plane outage, not escape the injection guard.
                raise TransportUnavailable(
                    f"Claude daemon attach connection failed: {exc}"
                ) from exc
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def attach_observe(
        self,
        short: str,
        *,
        cols: int = OBSERVER_ATTACH_COLS,
        rows: int = OBSERVER_ATTACH_ROWS,
        attach_id: str = OBSERVER_ATTACH_ID,
    ) -> None:
        """Hold a read-only attach open until the connection drops.

        The daemon only publishes state patches (tempo/needs) while the job
        has at least one attacher (live-verified 2026-07-07): without this
        connection a Feishu-spawned or detached job renders its dialogs into
        the PTY but list/subscribe stay frozen, blinding the notify-gate
        probe. The PTY stream is drained and discarded; no bytes are written.
        Returns when the daemon closes the connection (job gone, daemon
        restart); raises TransportUnavailable/ClaudeDaemonError on handshake
        failure like every other control-plane call.
        """
        reader, writer = await self._connect()
        try:
            request = {
                "proto": CLAUDE_DAEMON_PROTO,
                "op": "attach",
                "auth": self._auth(),
                "short": short,
                "cols": cols,
                "rows": rows,
                "attachId": attach_id,
            }
            writer.write(json.dumps(request).encode("utf-8") + b"\n")
            await writer.drain()
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=self.request_timeout)
            except asyncio.TimeoutError as exc:
                raise TransportUnavailable("Claude daemon attach handshake timed out") from exc
            if not line:
                raise TransportUnavailable("Claude daemon closed during attach handshake")
            _raise_on_error(_decode_line(line))
            await self._drain_pty_stream(reader)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _drain_pty_stream(reader: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return

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
        # v3 notify-mode gates (ADR 0046 v3): rid -> {short, kind, tool_name,
        # tool_input, session_id}. Registered by the runtime drain after the
        # card posts (the pending file is deleted then — no hook is waiting),
        # consumed by keystroke injection. In-memory by design: a runtime
        # restart degrades to the needs reminder card, terminal unaffected.
        self._notify_gates: dict[str, dict[str, Any]] = {}
        # rids resolved on the terminal side; bounded FIFO of tombstones.
        self._resolved_notify_rids: dict[str, float] = {}
        # short -> last successful injection ts: lets the daemon watcher tell
        # "needs cleared because Feishu injected" from "terminal answered".
        self._recent_injections: dict[str, float] = {}
        # short -> persistent observer-attach task (ADR 0048): keeps ≥1
        # attacher on every watched job so the daemon publishes state patches.
        self._observers: dict[str, asyncio.Task[None]] = {}

    # -- persistent observer attach (ADR 0048) ------------------------------

    def ensure_observer(self, short: str) -> None:
        """Keep one persistent observer attach alive for this job."""
        short = claude_daemon_short_id(short)
        if not short:
            return
        task = self._observers.get(short)
        if task is not None and not task.done():
            return
        self._observers[short] = asyncio.get_running_loop().create_task(
            self._observe_job_forever(short),
            name=f"walkcode-claude-daemon-observer-{short}",
        )

    def stop_observer(self, short: str) -> None:
        task = self._observers.pop(claude_daemon_short_id(short), None)
        if task is not None and not task.done():
            task.cancel()

    def stop_all_observers(self) -> None:
        for short in list(self._observers):
            self.stop_observer(short)

    async def _observe_job_forever(self, short: str) -> None:
        """Reconnect loop for one job's observer attach; exits when it dies."""
        while True:
            try:
                await self.client.attach_observe(short)
            except asyncio.CancelledError:
                raise
            except (TransportUnavailable, ClaudeDaemonError):
                pass
            except Exception as exc:  # pragma: no cover - defensive
                print(
                    f"claude daemon observer error ({short}): "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            alive = await self.client.job_alive(short)
            if alive is not True:
                # Job gone (False) — done for good. Probe outage (None):
                # also stop; the runtime's watcher sync re-ensures the
                # observer on its next discovery pass, so a daemon blip
                # cannot leak a hot reconnect loop. The finished task stays
                # in the dict until ensure_observer replaces it — a done
                # entry is inert.
                return
            await asyncio.sleep(OBSERVER_RECONNECT_SECONDS)

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
            "Claude daemon transport cannot create structured sessions; daemon-native "
            "spawn goes through the runtime's spawn_bg_job path (ADR 0048)"
        )

    async def spawn_bg_job(
        self,
        cwd: str,
        *,
        settings: str = "",
        cli_path: str = "",
        bg_runner: Any = None,
        ready_timeout: float = SPAWN_BG_READY_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """Create a daemon-born session via the official `claude --bg` CLI surface.

        The daemon's own `dispatch` op is CLI-internal (its `d` spec is
        undocumented and Zod-validated); `claude --bg` is the stable way to get
        a job that is a daemon worker from birth (live-verified TTY-less on
        2026-07-07: prints 'backgrounded · <short>', job lands in `list` with
        the full sessionId and cwd). Returns {short, session_id, cwd} once the
        job is ready for `reply`. Raises TransportUnavailable on any failure so
        callers can fall back to the headless SDK spawn.
        """
        argv = [cli_path or "claude", "--bg"]
        if settings:
            argv += ["--settings", settings]
        env = dict(os.environ)
        # A nested-CLAUDE environment refuses --bg ("session persistence is
        # disabled"); the runtime itself is never nested, but a dev running
        # `walkcode native serve` from inside a Claude session would be.
        env.pop("CLAUDECODE", None)
        if self.config_dir:
            env["CLAUDE_CONFIG_DIR"] = self.config_dir
        runner = bg_runner or self._run_bg_cli
        try:
            output = await runner(argv, cwd=cwd, env=env)
        except TransportUnavailable:
            raise
        except Exception as exc:
            raise TransportUnavailable(f"claude --bg failed: {type(exc).__name__}: {exc}") from exc
        short = parse_backgrounded_short(output)
        if not short:
            raise TransportUnavailable(
                f"claude --bg output had no parseable short id: {output.strip()[:200]!r}"
            )
        # `claude --bg` has already created a live worker. From here EVERY exit
        # that is not a clean return must best-effort reap it — a list/ready
        # probe raising (socket blip, daemon restart, protocol drift) used to
        # leave an orphan worker that list adoption would later resurface as a
        # duplicate session (ADR 0048 review finding). One try/except covers
        # ready-timeout, probe errors, and unexpected failures alike.
        try:
            deadline = time.monotonic() + ready_timeout
            while time.monotonic() < deadline:
                jobs = await self.client.list_jobs()
                entry = next(
                    (job for job in jobs if claude_daemon_short_id(job.get("short")) == short),
                    None,
                )
                if entry is not None:
                    session_id = str(entry.get("sessionId", "") or "")
                    job_cwd = str(entry.get("cwd", "") or "")
                    if session_id and await self.client.job_ready(short):
                        return {"short": short, "session_id": session_id, "cwd": job_cwd or cwd}
                await asyncio.sleep(SPAWN_BG_POLL_SECONDS)
            reason = f"job {short} never became ready within {ready_timeout:.0f}s"
        except Exception as exc:
            reason = f"job {short} probe failed: {type(exc).__name__}: {exc}"
        await self._reap_orphan(short)
        raise TransportUnavailable(f"claude --bg {reason}")

    async def _reap_orphan(self, short: str) -> None:
        """Best-effort kill of a spawned-but-unusable job; kill failure logged."""
        try:
            await self.client.kill(short)
        except Exception as exc:
            # Not raised: the caller is already failing over to headless. But a
            # silent suppress would hide a leaked worker — make it greppable.
            print(
                f"walkcode degrade=claude_daemon_spawn_reap_failed short={short} "
                f"error={type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    @staticmethod
    async def _run_bg_cli(argv: list[str], *, cwd: str, env: dict[str, str]) -> str:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd or None,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            raw, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SPAWN_BG_CLI_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            raise TransportUnavailable(
                f"claude --bg timed out after {SPAWN_BG_CLI_TIMEOUT_SECONDS:.0f}s"
            ) from None
        output = raw.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise TransportUnavailable(
                f"claude --bg exited {proc.returncode}: {output.strip()[:200]}"
            )
        return output

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

    # -- v3 notify-gate registry (keystroke injection route) ------------------

    def register_notify_gate(
        self, rid: str, request: dict[str, Any], *, session_id: str = ""
    ) -> None:
        tool_input = request.get("tool_input")
        self._notify_gates[str(rid)] = {
            "short": str(request.get("daemon_short", "") or ""),
            "kind": str(request.get("kind", "") or ""),
            "tool_name": str(request.get("tool_name", "") or ""),
            "tool_input": tool_input if isinstance(tool_input, dict) else {},
            "session_id": str(session_id or ""),
        }

    def notify_gate(self, rid: str) -> dict[str, Any] | None:
        return self._notify_gates.get(str(rid))

    def has_notify_gate_for_short(self, short: str, *, tool_name: str = "") -> bool:
        return any(
            entry.get("short") == short
            and (not tool_name or entry.get("tool_name") == tool_name)
            for entry in self._notify_gates.values()
        )

    def recently_injected(self, short: str, *, window: float = 10.0) -> bool:
        ts = self._recent_injections.get(str(short))
        return bool(ts and (time.time() - ts) <= window)

    def resolve_notify_gates_for_short(self, short: str) -> list[str]:
        """Terminal side answered (needs cleared): drop this job's open notify
        gates and tombstone their rids so late card clicks flip honestly."""
        resolved = [
            rid for rid, entry in self._notify_gates.items() if entry.get("short") == short
        ]
        for rid in resolved:
            self._notify_gates.pop(rid, None)
            self._tombstone_notify_rid(rid)
        return resolved

    def _tombstone_notify_rid(self, rid: str) -> None:
        self._resolved_notify_rids[str(rid)] = time.time()
        while len(self._resolved_notify_rids) > _NOTIFY_TOMBSTONE_LIMIT:
            self._resolved_notify_rids.pop(next(iter(self._resolved_notify_rids)))

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        if self.gate_state_path is None:
            raise CapabilityUnsupported("Claude gate spool is not configured for this transport")
        action = str((decision or {}).get("action", "") or "deny")
        entry = self._notify_gates.pop(str(rid), None)
        if entry is not None:
            await self._inject_gate_decision(rid, entry, keys_for_permission(action))
            self._notify_gate_decision(
                rid,
                {
                    "kind": claude_gate.KIND_PERMISSION,
                    "action": action,
                    "tool_name": entry.get("tool_name", ""),
                    "session_id": entry.get("session_id", ""),
                },
            )
            return
        if str(rid) in self._resolved_notify_rids:
            raise claude_gate.GateInjectionFailed(
                "already_resolved", "gate was settled on the terminal side"
            )
        payload = {"kind": claude_gate.KIND_PERMISSION, "action": action}
        reason = str((decision or {}).get("reason", "") or "")
        if reason:
            payload["reason"] = reason
        self._deliver_gate_decision(rid, payload)

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        if self.gate_state_path is None:
            raise CapabilityUnsupported("Claude gate spool is not configured for this transport")
        cleaned = {
            str(key): value
            for key, value in (answers or {}).items()
            if not str(key).startswith("_")
        }
        entry = self._notify_gates.pop(str(rid), None)
        if entry is not None:
            frames = keys_for_ask_answer(entry.get("tool_input", {}) or {}, cleaned)
            await self._inject_gate_decision(rid, entry, frames)
            self._notify_gate_decision(
                rid,
                {
                    "kind": claude_gate.KIND_ASK_USER,
                    "action": "answers",
                    "answers": cleaned,
                    "session_id": entry.get("session_id", ""),
                },
            )
            return
        if str(rid) in self._resolved_notify_rids:
            raise claude_gate.GateInjectionFailed(
                "already_resolved", "gate was settled on the terminal side"
            )
        payload = {
            "kind": claude_gate.KIND_ASK_USER,
            "action": "answers",
            "answers": cleaned,
        }
        self._deliver_gate_decision(rid, payload)

    async def _inject_gate_decision(
        self, rid: str, entry: dict[str, Any], frames: list[KeyFrame] | None
    ) -> None:
        """Drive the native dialog for a notify-mode gate; raise on any miss.

        Failure is a routing verdict, not an error state: the dialog stays on
        screen and the terminal remains fully usable, so every miss maps to a
        "answer in the terminal" card. No retries — the injected digits are
        toggles/confirms and a double press would flip state.
        """
        short = str(entry.get("short", "") or "")
        if not short:
            raise claude_gate.GateInjectionFailed("inject_failed", "notify gate lost its job id")
        if not frames:
            claude_gate.trace("inject_not_mappable", rid=rid, kind=entry.get("kind"))
            raise claude_gate.GateInjectionFailed(
                "not_injectable", "answer shape is outside the verified keystroke matrix"
            )
        job = await self._gate_job_state(short)
        if job is _JOB_STATE_UNAVAILABLE:
            claude_gate.trace("inject_probe_unavailable", rid=rid, short=short)
            raise claude_gate.GateInjectionFailed(
                "inject_failed", "daemon state probe unavailable; not injecting blind"
            )
        needs_before = str((job or {}).get("needs", "") or "").strip() if isinstance(job, dict) else ""
        if not self._gate_dialog_matches(entry, job, needs_before):
            # Log only the dialog kind (text before the colon): the detail can
            # carry command lines / paths / question text (review finding).
            needs_kind = needs_before.split(":", 1)[0][:32]
            claude_gate.trace("inject_dialog_mismatch", rid=rid, short=short, needs_kind=needs_kind)
            self._tombstone_notify_rid(rid)
            raise claude_gate.GateInjectionFailed(
                "dialog_mismatch",
                f"dialog state does not match this request ({needs_kind or 'no dialog'})",
            )
        try:
            await self.client.attach_send_keys(short, frames)
        except (TransportUnavailable, ClaudeDaemonError) as exc:
            claude_gate.trace("inject_attach_failed", rid=rid, short=short, error=str(exc))
            raise claude_gate.GateInjectionFailed("inject_failed", str(exc)) from exc
        if not await self._gate_dialog_resolved(short, needs_before):
            claude_gate.trace("inject_not_cleared", rid=rid, short=short)
            raise claude_gate.GateInjectionFailed(
                "not_cleared", "dialog did not resolve within the verify window"
            )
        self._recent_injections[short] = time.time()
        while len(self._recent_injections) > _NOTIFY_TOMBSTONE_LIMIT:
            self._recent_injections.pop(next(iter(self._recent_injections)))
        claude_gate.trace("inject_ok", rid=rid, short=short, frames=len(frames))

    async def notify_dialog_waiting(self, request: dict[str, Any]) -> bool:
        """Is the native dialog for this notify pending rendered and waiting?

        The card must mirror a real dialog: Claude Code auto-approves some
        tool calls (safe read-only Bash, sandboxed commands), in which case
        no dialog ever renders and posting a card would leave dangling live
        buttons (live-E2E finding, 2026-07-06).
        """
        short = str(request.get("daemon_short", "") or "")
        if not short:
            return False
        entry = {
            "kind": str(request.get("kind", "") or ""),
            "tool_name": str(request.get("tool_name", "") or ""),
            "tool_input": request.get("tool_input") if isinstance(request.get("tool_input"), dict) else {},
        }
        job = await self._gate_job_state(short)
        if not isinstance(job, dict):
            # No job or probe outage: either way, no dialog is confirmed waiting.
            return False
        needs = str(job.get("needs", "") or "").strip()
        return self._gate_dialog_matches(entry, job, needs)

    async def _gate_job_state(self, short: str) -> Any:
        """Job dict, None (queried fine, job absent), or _JOB_STATE_UNAVAILABLE."""
        try:
            jobs = await self.client.list_jobs()
        except (TransportUnavailable, ClaudeDaemonError):
            return _JOB_STATE_UNAVAILABLE
        for job in jobs:
            if claude_daemon_short_id(job.get("short") or job.get("sessionId")) == short:
                return job
        return None

    @staticmethod
    def _gate_dialog_matches(entry: dict[str, Any], job: Any, needs: str) -> bool:
        # Pre-injection guard: the job must still be blocked on the dialog
        # this card mirrors. Needs formats (live-verified):
        #   permission: "approve <Tool>: <detail>"
        #   ask:        "answer: <question> (<label> · <label> ...)"
        # Matching is anchored and exact wherever possible: a fuzzy match here
        # is what would let a stale card drive the WRONG dialog.
        if not isinstance(job, dict):
            return False
        if str(job.get("tempo", "") or "") != "blocked" or not needs:
            return False
        if str(entry.get("kind", "")) == claude_gate.KIND_ASK_USER:
            if not needs.startswith("answer:"):
                return False
            tool_input = entry.get("tool_input", {}) or {}
            questions = tool_input.get("questions", [])
            first = questions[0] if isinstance(questions, list) and questions else {}
            question_text = str(
                (first or {}).get("question")
                or (first or {}).get("header")
                or (first or {}).get("prompt")
                or ""
            ).strip()
            if not question_text:
                # Nothing to bind the dialog to this request: do not inject.
                return False
            # Anchored prefix (tolerates daemon-side truncation of the tail).
            rendered = needs[len("answer:"):].strip()
            return rendered.startswith(question_text[:40])
        tool_name = str(entry.get("tool_name", "") or "")
        match = _APPROVE_NEEDS_RE.match(needs)
        if match is None or not tool_name:
            return False
        return match.group(1) == tool_name

    async def _gate_dialog_resolved(self, short: str, needs_before: str) -> bool:
        deadline = time.monotonic() + GATE_INJECT_VERIFY_TIMEOUT_SECONDS
        while True:
            await asyncio.sleep(GATE_INJECT_VERIFY_POLL_SECONDS)
            job = await self._gate_job_state(short)
            if job is _JOB_STATE_UNAVAILABLE:
                # Probe outage proves nothing — keep trying, then fail honest.
                if time.monotonic() >= deadline:
                    return False
                continue
            if job is None:
                return True  # queried fine, job left the list: dialog is gone
            needs = str(job.get("needs", "") or "").strip()
            if not needs or needs != needs_before:
                return True
            if str(job.get("tempo", "") or "") != "blocked":
                return True
            if time.monotonic() >= deadline:
                return False

    def _deliver_gate_decision(self, rid: str, payload: dict[str, Any]) -> None:
        # A decision only counts when a hook is still waiting for it AND the
        # write-once actually landed. A stale card click (hook timed out and
        # cleaned its pending, or the runtime restarted and lost the notify
        # registry) or a lost race must not leave orphan decision files, must
        # not feed always_allow via the observer — and must NOT read as
        # success to the caller, or the card flips to "allowed" while nothing
        # actually happened (deep-review finding, 6-dimension hit).
        if claude_gate.read_pending(self.gate_state_path, rid) is None:
            claude_gate.trace("decision_dropped_no_pending", rid=rid, action=payload.get("action"))
            raise claude_gate.GateInjectionFailed(
                "stale_gate",
                "no pending gate is waiting for this decision (request settled or runtime restarted)",
            )
        if not claude_gate.write_decision(self.gate_state_path, rid, payload):
            claude_gate.trace("decision_dropped_already_decided", rid=rid, action=payload.get("action"))
            raise claude_gate.GateInjectionFailed(
                "already_resolved", "another surface already decided this request"
            )
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
