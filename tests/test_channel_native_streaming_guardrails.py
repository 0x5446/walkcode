import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeChannelAdapter,
    InteractionStore,
    LarkBotApi,
    LarkChannelAdapter,
    LaunchSpec,
    Orchestrator,
    SessionRegistry,
    TelegramChannelAdapter,
    TransportCapabilities,
    TransportHandle,
    TurnInput,
    render_view_text,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="owner", display_name="Owner")


def _binding(kind: str = "telegram") -> ChannelBinding:
    return ChannelBinding(kind, "bot", "chat", "topic", "root")


def _channel_caps() -> ChannelCapabilities:
    return ChannelCapabilities(
        thread_context=True,
        editable_message=True,
        interactive_message=True,
        interactive_update=True,
        private_callback_ack=True,
        toast_or_ephemeral_notice=True,
        force_reply=True,
        attachment_download=True,
        forum_or_topic=True,
        max_text_chars=4096,
        max_callback_payload_bytes=64,
    )


def _transport_caps() -> TransportCapabilities:
    return TransportCapabilities(
        structured_input=True,
        structured_output=True,
        permission_callback=False,
        ask_user_question=False,
        interrupt=False,
        set_model=False,
        set_permission_mode=False,
        checkpoint_rewind=False,
        resume_after_complete=True,
        resume_active_turn=False,
        multi_client_observe=False,
        multi_client_write=False,
        external_tui_takeover=False,
    )


class StreamingEventBoundaryTests(unittest.TestCase):
    def test_orchestrator_drains_async_event_stream(self):
        class _StreamingTransport:
            kind = "streaming"

            def __init__(self):
                self.submitted = []

            def capabilities(self):
                return _transport_caps()

            async def launch(self, spec: LaunchSpec):
                return TransportHandle("h1", self.kind, {"session_id": spec.session_id, "cwd": spec.cwd})

            async def submit_turn(self, handle, turn, idempotency_key):
                self.submitted.append(turn.text)

            def events(self, handle):
                async def stream():
                    yield AgentEvent(AgentEventType.TURN_DELTA, {"text": "hello"})
                    yield AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})

                return stream()

        channel = FakeChannelAdapter("telegram", _channel_caps())
        transport = _StreamingTransport()
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"streaming": transport},
            now=_Clock(),
        )

        session = asyncio.run(orchestrator.start_session(_binding("telegram"), "streaming", "/tmp/project", _actor()))
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="go"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("hello", channel.rendered_text())
        self.assertIn("done", channel.rendered_text())


class RegistryGuardrailTests(unittest.TestCase):
    def test_block_input_rejects_structured_sessions(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_structured_session(
            session_id="s1",
            binding=_binding("telegram"),
            transport_kind="claude_headless",
            transport_ref={"session_id": "claude-1"},
            cwd="/tmp/project",
            owner=_actor(),
        )

        result = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="should not block"),
            generation=session.generation,
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.NOT_EXTERNAL_TUI)
        self.assertEqual(session.blocked_inputs, {})


class NeutralViewTextTests(unittest.TestCase):
    def test_lark_does_not_depend_on_telegram_text_helper(self):
        calls = []

        async def lark_caller(method, payload):
            calls.append((method, payload))
            return {"data": {"message_id": "om_1"}}

        original = TelegramChannelAdapter._text_from_view
        TelegramChannelAdapter._text_from_view = staticmethod(
            lambda _view: (_ for _ in ()).throw(AssertionError("telegram helper used"))
        )
        try:
            lark = LarkChannelAdapter(LarkBotApi(caller=lark_caller))
            view = {"type": "error", "code": "bad", "message": "failed"}
            msg_id = asyncio.run(lark.send_view(_binding("lark"), view))
        finally:
            TelegramChannelAdapter._text_from_view = staticmethod(original)

        self.assertEqual(msg_id, "om_1")
        self.assertEqual(calls[0][1]["text"], render_view_text({"type": "error", "code": "bad", "message": "failed"}))
