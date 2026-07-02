import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
    TurnInput,
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


class SessionControlTests(unittest.TestCase):
    def test_owner_can_interrupt_when_transport_supports_it(self):
        orchestrator, transport, session = _orchestrator()

        result = asyncio.run(
            orchestrator.interrupt_session(
                session.session_id,
                actor=_actor("owner"),
                reason="user requested",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.interrupt_calls, ["user requested"])
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.lifecycle_state, "INTERRUPTED")
        self.assertEqual(updated.interrupt_reason, "user requested")

    def test_collaborator_and_reviewer_cannot_interrupt_or_close(self):
        orchestrator, transport, session = _orchestrator()

        interrupt = asyncio.run(
            orchestrator.interrupt_session(
                session.session_id,
                actor=_actor("collab"),
                reason="stop",
            )
        )
        close = asyncio.run(
            orchestrator.close_session(
                session.session_id,
                actor=_actor("reviewer"),
                reason="done",
            )
        )

        self.assertFalse(interrupt.accepted)
        self.assertEqual(interrupt.reason, BlockedReason.UNAUTHORIZED)
        self.assertFalse(close.accepted)
        self.assertEqual(close.reason, BlockedReason.UNAUTHORIZED)
        self.assertEqual(transport.interrupt_calls, [])
        self.assertEqual(transport.shutdown_calls, [])

    def test_interrupt_capability_disabled_does_not_call_transport(self):
        orchestrator, transport, session = _orchestrator(caps=_transport_caps(interrupt=False))

        result = asyncio.run(
            orchestrator.interrupt_session(
                session.session_id,
                actor=_actor("owner"),
                reason="stop",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(transport.interrupt_calls, [])

    def test_close_marks_session_stopped_and_blocks_future_submits(self):
        orchestrator, transport, session = _orchestrator()

        result = asyncio.run(
            orchestrator.close_session(
                session.session_id,
                actor=_actor("admin"),
                reason="finished",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.shutdown_calls, ["graceful"])
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.status, "stopped")
        self.assertEqual(updated.lifecycle_state, "STOPPED")
        self.assertEqual(updated.stop_reason, "finished")

        submit = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="should not run"),
                actor=_actor("owner"),
                generation=updated.generation,
            )
        )

        self.assertFalse(submit.accepted)
        self.assertEqual(submit.reason, BlockedReason.SESSION_STOPPED)

    def test_command_menu_reflects_role_and_transport_capabilities(self):
        orchestrator, _transport, session = _orchestrator()

        owner_menu = orchestrator.command_menu_for_session(session.session_id, actor=_actor("owner"))
        collab_menu = orchestrator.command_menu_for_session(session.session_id, actor=_actor("collab"))

        self.assertEqual([action["action"] for action in owner_menu["actions"]], ["interrupt", "close"])
        self.assertEqual(collab_menu["actions"], [])

        no_interrupt, _transport, no_interrupt_session = _orchestrator(caps=_transport_caps(interrupt=False))
        menu = no_interrupt.command_menu_for_session(no_interrupt_session.session_id, actor=_actor("owner"))
        self.assertEqual([action["action"] for action in menu["actions"]], ["close"])


if __name__ == "__main__":
    unittest.main()
