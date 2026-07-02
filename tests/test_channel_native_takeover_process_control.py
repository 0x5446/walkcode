import asyncio
import subprocess
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ControlResult,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    FakeExternalTuiController,
    InboundEvent,
    InteractionStore,
    LocalProcessController,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    SessionRole,
    TakeoverPhase,
    TransportCapabilities,
    TransportHandle,
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


def _transport_caps() -> TransportCapabilities:
    return TransportCapabilities(
        structured_input=True,
        structured_output=True,
        permission_callback=True,
        ask_user_question=True,
        interrupt=True,
        set_model=True,
        set_permission_mode=True,
        checkpoint_rewind=True,
        resume_after_complete=True,
        resume_active_turn=False,
        multi_client_observe=False,
        multi_client_write=False,
        external_tui_takeover=True,
    )


def _callback(token: str, *, event_id: str = "cb-1") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id="m-cb",
        root_message_id="root",
        sender_id="owner",
        sender_display="Owner",
        text=f"cb:{token}",
        callback={"token": token},
    )


def _action_token(channel: FakeChannelAdapter, action: str) -> str:
    view = channel.sent_views[-1]["view"]
    return next(item["token"] for item in view["actions"] if item["action"] == action)


def _setup(*, terminate_ref=None, controller=None, transport=None):
    clock = _Clock()
    sessions = SessionRegistry(now=clock)
    interactions = InteractionStore(now=clock)
    outbox = DurableOutbox(now=clock)
    authz = AuthorizationStore(now=clock)
    channel = FakeChannelAdapter("telegram", _channel_caps())
    transport = transport or FakeAgentTransport("fake-transport", _transport_caps())
    controller = controller if controller is not None else FakeExternalTuiController("fake-process")
    controllers = {controller.kind: controller} if controller is not None else {}
    orchestrator = Orchestrator(
        sessions=sessions,
        interactions=interactions,
        outbox=outbox,
        channels={"telegram": channel},
        transports={"fake-transport": transport},
        external_tui_controllers=controllers,
        authz=authz,
        now=clock,
    )
    external_ref = {
        "resume_ref": {
            "transport_kind": "fake-transport",
            "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
        },
    }
    if terminate_ref is not None:
        external_ref["terminate_ref"] = terminate_ref
    session = sessions.create_observed_session(
        session_id="observed-1",
        binding=_binding(),
        cwd="/tmp/project",
        external_ref=external_ref,
        owner=_actor("owner"),
    )
    authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
    return orchestrator, channel, transport, controller, session


class TakeoverProcessControlTests(unittest.TestCase):
    def test_local_process_controller_terminates_authorized_process(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            controller = LocalProcessController(timeout=2.0)
            result = asyncio.run(
                controller.terminate(
                    {"pid": proc.pid, "allow_terminate": True},
                    reason="test",
                )
            )

            self.assertTrue(result.accepted)
            proc.wait(timeout=2.0)
            self.assertIsNotNone(proc.returncode)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_local_process_controller_refuses_unauthorized_process(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            controller = LocalProcessController(timeout=0.2)
            result = asyncio.run(controller.terminate({"pid": proc.pid}, reason="test"))

            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, "termination_not_authorized")
            self.assertIsNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_takeover_resumes_before_terminating_external_tui_and_submit(self):
        order = []

        class OrderedController(FakeExternalTuiController):
            async def terminate(self, ref: dict, reason: str) -> ControlResult:
                order.append("terminate")
                return await super().terminate(ref, reason)

        class OrderedTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec) -> TransportHandle:
                order.append("resume")
                return await super().resume(spec)

            async def submit_turn(self, handle, turn, idempotency_key):
                order.append("submit_turn")
                await super().submit_turn(handle, turn, idempotency_key)

        orchestrator, channel, transport, controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=OrderedController("fake-process"),
            transport=OrderedTransport("fake-transport", _transport_caps()),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(order, ["resume", "terminate", "submit_turn"])
        self.assertEqual(controller.terminate_calls[0]["ref"], {"pid": 123})
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_resume_failure_does_not_terminate_external_tui(self):
        class FailingResumeTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec) -> TransportHandle:
                self.resume_specs.append(spec)
                self.call_log.append("resume")
                raise RuntimeError("resume failed")

        controller = FakeExternalTuiController("fake-process")
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=controller,
            transport=FailingResumeTransport("fake-transport", _transport_caps()),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "resume_failed")
        self.assertEqual(controller.terminate_calls, [])
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.submitted_turns, [])

    def test_missing_terminate_ref_becomes_manual_only_even_with_resume_ref(self):
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref=None,
            controller=FakeExternalTuiController("fake-process"),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "manual_only")
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(tx["phase"], TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(transport.resume_specs, [])
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(transport.submitted_turns, [])

    def test_process_terminate_ref_without_explicit_authorization_is_manual_only(self):
        controller = FakeExternalTuiController("process")
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "process", "process_ref": {"pid": 123}},
            controller=controller,
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "manual_only")
        self.assertEqual(controller.terminate_calls, [])
        self.assertEqual(transport.resume_specs, [])
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(transport.submitted_turns, [])

    def test_termination_failure_rolls_back_resumed_handle_without_submit_or_transfer(self):
        controller = FakeExternalTuiController("fake-process", accepted=False)
        orchestrator, channel, transport, _controller, session = _setup(
            terminate_ref={"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            controller=controller,
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_action_token(channel, "takeover_and_send")),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        blocked = next(iter(updated.blocked_inputs.values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "external_tui_termination_failed")
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_progress")
        self.assertEqual(channel.sent_views[-1]["view"]["phase"], "failed")
        self.assertEqual(tx["phase"], TakeoverPhase.FAILED)
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.shutdown_calls, ["takeover_rollback"])
        self.assertEqual(transport.submitted_turns, [])


if __name__ == "__main__":
    unittest.main()
