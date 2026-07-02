import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    CodexAppServerTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    FakeExternalTuiController,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    SessionRole,
    TakeoverPhase,
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
        "external_tui_takeover": True,
    }
    data.update(overrides)
    return TransportCapabilities(**data)


def _callback(token: str) -> InboundEvent:
    return InboundEvent(
        event_id="cb-1",
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


def _setup(transport):
    clock = _Clock()
    sessions = SessionRegistry(now=clock)
    interactions = InteractionStore(now=clock)
    authz = AuthorizationStore(now=clock)
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=sessions,
        interactions=interactions,
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports={"fake-transport": transport},
        external_tui_controllers={"fake-process": FakeExternalTuiController("fake-process")},
        authz=authz,
        now=clock,
    )
    session = sessions.create_observed_session(
        session_id="observed-1",
        binding=_binding(),
        cwd="/tmp/project",
        external_ref={
            "resume_ref": {
                "transport_kind": "fake-transport",
                "session_id": "native-1",
            },
            "terminate_ref": {
                "controller_kind": "fake-process",
                "process_ref": {"pid": 123},
            },
        },
        owner=_actor("owner"),
    )
    authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
    return orchestrator, channel, session


def _takeover_token(channel: FakeChannelAdapter) -> str:
    view = channel.sent_views[-1]["view"]
    return next(action["token"] for action in view["actions"] if action["action"] == "takeover_and_send")


def _confirmation_token(channel: FakeChannelAdapter) -> str:
    view = channel.sent_views[-1]["view"]
    return next(action["token"] for action in view["actions"] if action["action"] == "confirm_takeover")


class ResumeBoundaryTests(unittest.TestCase):
    def test_takeover_resumes_transport_before_submitting_blocked_input(self):
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator, channel, session = _setup(transport)
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_takeover_token(channel)),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([spec.resume_ref["session_id"] for spec in transport.resume_specs], ["native-1"])
        self.assertEqual(transport.call_log, ["resume", "submit_turn"])
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_takeover_resume_failure_does_not_complete_or_submit(self):
        class FailingResumeTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec):
                self.resume_specs.append(spec)
                self.call_log.append("resume")
                raise RuntimeError("resume failed")

        transport = FailingResumeTransport("fake-transport", _transport_caps())
        orchestrator, channel, session = _setup(transport)
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_takeover_token(channel)),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        blocked = next(iter(updated.blocked_inputs.values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "resume_failed")
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_progress")
        self.assertEqual(channel.sent_views[-1]["view"]["phase"], "failed")
        self.assertEqual(tx["phase"], TakeoverPhase.FAILED)
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(blocked.state, "blocked")
        self.assertEqual(transport.submitted_turns, [])

    def test_codex_generic_resume_uses_thread_resume(self):
        class Client:
            def __init__(self):
                self.requests = []

            async def request(self, method, params):
                self.requests.append((method, params))
                return {"threadId": params["threadId"]}

            async def events(self, thread_id):
                return []

        client = Client()
        transport = CodexAppServerTransport(client=client)

        handle = asyncio.run(
            transport.resume(
                ResumeSpec(
                    cwd="/tmp/project",
                    session_id="s1",
                    resume_ref={"thread_id": "thread-2"},
                )
            )
        )

        self.assertEqual(handle.ref["thread_id"], "thread-2")
        self.assertEqual(client.requests, [("thread/resume", {"threadId": "thread-2", "cwd": "/tmp/project"})])

    def test_claude_generic_resume_delegates_to_injected_client(self):
        class Client:
            def __init__(self):
                self.resumed = []

            async def resume(self, resume_ref):
                self.resumed.append(dict(resume_ref))

            async def submit(self, turn):
                pass

            async def events(self):
                return []

        client = Client()
        transport = ClaudeHeadlessTransport(client_factory=lambda _spec: client)

        handle = asyncio.run(
            transport.resume(
                ResumeSpec(
                    cwd="/tmp/project",
                    session_id="s1",
                    resume_ref={"session_id": "claude-1"},
                )
            )
        )

        self.assertEqual(client.resumed, [{"session_id": "claude-1"}])
        self.assertEqual(handle.ref["session_id"], "claude-1")


if __name__ == "__main__":
    unittest.main()
