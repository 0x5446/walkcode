"""In-memory fakes of the channel, TUI controller and agent transport, used by tests."""

from __future__ import annotations

import uuid

from typing import Any

from .models import (
    AgentEvent,
    AttachmentRef,
    ChannelBinding,
    ChannelCapabilities,
    ControlResult,
    InboundEvent,
    LaunchSpec,
    ResumeSpec,
    TransportCapabilities,
    TransportHandle,
    TurnInput,
)
from .views import render_view_text


class FakeChannelAdapter:
    def __init__(self, kind: str, capabilities: ChannelCapabilities):
        self.kind = kind
        self._capabilities = capabilities
        self.sent_views: list[dict[str, Any]] = []
        self.downloaded_attachments: list[str] = []
        self.acknowledged_callbacks: list[str] = []
        self.deleted_messages: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def react_to_message(self, binding: ChannelBinding, message_id: str, emoji: str = "DONE") -> bool:
        self.reactions.append({"binding": binding.key(), "message_id": str(message_id), "emoji": emoji})
        return True

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
        self.shutdown_calls: list[str] = []
        self.model_calls: list[str] = []
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

    async def events(self, handle: TransportHandle) -> list[AgentEvent]:
        events = self._scripted_events
        self._scripted_events = []
        return events
