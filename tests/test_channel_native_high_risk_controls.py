import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


def _binding() -> ChannelBinding:
    return ChannelBinding("telegram", "bot", "chat", "topic", "root")


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


def _transport_caps(**overrides) -> TransportCapabilities:
    data = {
        "structured_input": True,
        "structured_output": True,
        "permission_callback": True,
        "ask_user_question": True,
        "interrupt": True,
        "set_model": True,
        "set_permission_mode": True,
        "checkpoint_rewind": True,
        "resume_after_complete": True,
        "resume_active_turn": False,
        "multi_client_observe": False,
        "multi_client_write": False,
        "external_tui_takeover": False,
    }
    data.update(overrides)
    return TransportCapabilities(**data)


def _orchestrator(*, caps=None):
    clock = _Clock()
    authz = AuthorizationStore(now=clock)
    transport = FakeAgentTransport("fake-transport", caps or _transport_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
        transports={"fake-transport": transport},
        authz=authz,
        now=clock,
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor("owner"))
    )
    authz.grant(session.session_id, _actor("collab"), SessionRole.COLLABORATOR)
    authz.grant(session.session_id, _actor("reviewer"), SessionRole.REVIEWER)
    authz.grant(session.session_id, _actor("admin"), SessionRole.ADMIN)
    return orchestrator, transport, session


class HighRiskTransportControlTests(unittest.TestCase):
    def test_owner_can_call_supported_high_risk_controls(self):
        orchestrator, transport, session = _orchestrator()

        model = asyncio.run(
            orchestrator.set_session_model(session.session_id, actor=_actor("owner"), model="sonnet")
        )
        permission = asyncio.run(
            orchestrator.set_session_permission_mode(
                session.session_id,
                actor=_actor("admin"),
                mode="plan",
            )
        )
        rewind = asyncio.run(
            orchestrator.rewind_session_checkpoint(
                session.session_id,
                actor=_actor("owner"),
                checkpoint_id="checkpoint-1",
            )
        )

        self.assertTrue(model.accepted)
        self.assertTrue(permission.accepted)
        self.assertTrue(rewind.accepted)
        self.assertEqual(transport.model_calls, ["sonnet"])
        self.assertEqual(transport.permission_mode_calls, ["plan"])
        self.assertEqual(transport.rewind_calls, ["checkpoint-1"])

    def test_collaborator_and_reviewer_cannot_call_high_risk_controls(self):
        orchestrator, transport, session = _orchestrator()

        model = asyncio.run(
            orchestrator.set_session_model(session.session_id, actor=_actor("collab"), model="opus")
        )
        rewind = asyncio.run(
            orchestrator.rewind_session_checkpoint(
                session.session_id,
                actor=_actor("reviewer"),
                checkpoint_id="checkpoint-1",
            )
        )

        self.assertFalse(model.accepted)
        self.assertEqual(model.reason, BlockedReason.UNAUTHORIZED)
        self.assertFalse(rewind.accepted)
        self.assertEqual(rewind.reason, BlockedReason.UNAUTHORIZED)
        self.assertEqual(transport.model_calls, [])
        self.assertEqual(transport.rewind_calls, [])

    def test_capability_disabled_does_not_call_transport(self):
        orchestrator, transport, session = _orchestrator(
            caps=_transport_caps(set_model=False, set_permission_mode=False, checkpoint_rewind=False)
        )

        model = asyncio.run(
            orchestrator.set_session_model(session.session_id, actor=_actor("owner"), model="opus")
        )
        permission = asyncio.run(
            orchestrator.set_session_permission_mode(
                session.session_id,
                actor=_actor("owner"),
                mode="acceptEdits",
            )
        )
        rewind = asyncio.run(
            orchestrator.rewind_session_checkpoint(
                session.session_id,
                actor=_actor("owner"),
                checkpoint_id="checkpoint-1",
            )
        )

        self.assertEqual(model.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(permission.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(rewind.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(transport.model_calls, [])
        self.assertEqual(transport.permission_mode_calls, [])
        self.assertEqual(transport.rewind_calls, [])

    def test_stopped_session_rejects_high_risk_controls(self):
        orchestrator, transport, session = _orchestrator()
        asyncio.run(orchestrator.close_session(session.session_id, actor=_actor("owner"), reason="done"))

        result = asyncio.run(
            orchestrator.set_session_model(session.session_id, actor=_actor("owner"), model="opus")
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.SESSION_STOPPED)
        self.assertEqual(transport.model_calls, [])

    def test_claude_headless_delegates_controls_to_injected_client(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def set_model(self, model: str):
                self.calls.append(("set_model", model))

            async def set_permission_mode(self, mode: str):
                self.calls.append(("set_permission_mode", mode))

            async def rewind_checkpoint(self, checkpoint_id: str):
                self.calls.append(("rewind_checkpoint", checkpoint_id))

            async def events(self):
                return []

            async def submit(self, _turn):
                return None

        client = Client()
        transport = ClaudeHeadlessTransport(client_factory=lambda _spec: client)
        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        model = asyncio.run(transport.set_model(handle, "sonnet"))
        permission = asyncio.run(transport.set_permission_mode(handle, "plan"))
        rewind = asyncio.run(transport.rewind_checkpoint(handle, "checkpoint-1"))

        self.assertTrue(model.accepted)
        self.assertTrue(permission.accepted)
        self.assertTrue(rewind.accepted)
        self.assertEqual(
            client.calls,
            [
                ("set_model", "sonnet"),
                ("set_permission_mode", "plan"),
                ("rewind_checkpoint", "checkpoint-1"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
