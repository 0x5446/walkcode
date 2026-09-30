"""Decision half of the Claude PreToolUse gate (ADR 0046 v2, ADR 0068).

TUI-observed Claude sessions have no structured transport of their own
(``transport_kind == "external_tui"``). Their permission / AskUserQuestion
cards are produced from the gate spool (``claude_gate``), and a card click
has to end up as ``decisions/<rid>.json`` so the blocking hook can return it
to Claude Code. ``ClaudeGateTransport`` is that write side: the orchestrator's
``_interaction_transport`` routes TUI HITL decisions here.

It used to live inside the Claude daemon transport (ADR 0046); ADR 0068
retired the daemon and kept only this part. It does not talk to any Claude
process: it only writes decision files, so it works whether or not a daemon
(or anything else) is running.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any, Callable

from . import (
    CapabilityUnsupported,
    ControlResult,
    LaunchSpec,
    ResumeSpec,
    TransportCapabilities,
    TransportHandle,
    TurnInput,
)
from . import claude_gate

CLAUDE_GATE_TRANSPORT_KEY = "claude_gate"


class ClaudeGateTransport:
    kind = CLAUDE_GATE_TRANSPORT_KEY

    def __init__(self, *, gate_state_path: str | Path):
        self.gate_state_path = Path(gate_state_path)
        # Runtime-installed observer: (rid, decision) after a decision file
        # lands, so the runtime can learn session-scoped always_allow.
        self.on_gate_decision: Callable[[str, dict[str, Any]], None] | None = None

    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            structured_input=False,
            structured_output=False,
            permission_callback=True,
            ask_user_question=True,
            set_model=False,
            resume_after_complete=False,
            external_tui_takeover=False,
        )

    async def launch(self, spec: LaunchSpec) -> TransportHandle:
        raise CapabilityUnsupported("the Claude gate transport only carries HITL decisions")

    async def resume(self, spec: ResumeSpec) -> TransportHandle:
        raise CapabilityUnsupported("the Claude gate transport only carries HITL decisions")

    async def submit_turn(
        self,
        handle: TransportHandle,
        turn: TurnInput,
        idempotency_key: str,
    ) -> None:
        raise CapabilityUnsupported("the Claude gate transport only carries HITL decisions")

    async def approve_permission(
        self,
        handle: TransportHandle,
        rid: str,
        decision: dict[str, Any],
    ) -> None:
        action = str((decision or {}).get("action", "") or "deny")
        payload = {"kind": claude_gate.KIND_PERMISSION, "action": action}
        reason = str((decision or {}).get("reason", "") or "")
        if reason:
            payload["reason"] = reason
        self._deliver(rid, payload)

    async def answer_user_question(
        self,
        handle: TransportHandle,
        rid: str,
        answers: dict[str, Any],
    ) -> None:
        cleaned = {
            str(key): value
            for key, value in (answers or {}).items()
            if not str(key).startswith("_")
        }
        self._deliver(
            rid,
            {"kind": claude_gate.KIND_ASK_USER, "action": "answers", "answers": cleaned},
        )

    def _deliver(self, rid: str, payload: dict[str, Any]) -> None:
        # A decision only counts when a hook is still waiting for it AND the
        # write-once actually landed. A stale card click (the hook timed out
        # and cleaned its pending, or the runtime restarted) or a lost race
        # must not leave orphan decision files, must not feed always_allow via
        # the observer — and must NOT read as success to the caller, or the
        # card flips to "allowed" while nothing actually happened.
        if claude_gate.read_pending(self.gate_state_path, rid) is None:
            claude_gate.trace("decision_dropped_no_pending", rid=rid, action=payload.get("action"))
            raise claude_gate.GateDecisionFailed(
                "stale_gate",
                "no pending gate is waiting for this decision (request settled or runtime restarted)",
            )
        if not claude_gate.write_decision(self.gate_state_path, rid, payload):
            claude_gate.trace("decision_dropped_already_decided", rid=rid, action=payload.get("action"))
            raise claude_gate.GateDecisionFailed(
                "already_resolved", "another surface already decided this request"
            )
        if self.on_gate_decision is not None:
            with contextlib.suppress(Exception):
                self.on_gate_decision(rid, dict(payload))

    async def shutdown(self, handle: TransportHandle, mode: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_gate")

    async def set_model(self, handle: TransportHandle, model: str) -> ControlResult:
        return ControlResult(False, "unsupported_by_claude_gate")

    def events(self, handle: TransportHandle) -> list[Any]:
        return []
