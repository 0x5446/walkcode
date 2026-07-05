import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
    TurnInput,
    ViewModelFactory,
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


def _orchestrator(*, caps=None, scripted_events=None):
    clock = _Clock()
    authz = AuthorizationStore(now=clock)
    transport = FakeAgentTransport(
        "fake-transport",
        caps or _transport_caps(),
        scripted_events=list(scripted_events or []),
    )
    channel = FakeChannelAdapter("telegram", _channel_caps())
    interactions = InteractionStore(now=clock)
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=interactions,
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
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
    return orchestrator, transport, channel, interactions, session


def _permission_event(*, high_risk: bool = True) -> AgentEvent:
    return AgentEvent(
        AgentEventType.PERMISSION_REQUESTED,
        {
            "rid": "perm-1",
            "tool_name": "Bash",
            "tool_input": {"cmd": "rm -rf build"},
            "actions": ["allow_once", "deny"],
            "high_risk": high_risk,
        },
    )


def _callback(token: str, *, actor_id: str = "owner") -> InboundEvent:
    return InboundEvent(
        event_id=f"cb-{actor_id}",
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id=f"m-{actor_id}",
        root_message_id="root",
        sender_id=actor_id,
        sender_display=actor_id.title(),
        text=f"cb:{token}",
        callback={"token": token},
    )


def _token_for(view: dict, action: str) -> str:
    return next(item["token"] for item in view["actions"] if item["action"] == action)


class PermissionRoundTripTests(unittest.TestCase):
    def test_permission_event_renders_prompt_and_owner_approval_calls_transport(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_permission_event(high_risk=True)]
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        prompt = channel.sent_views[0]["view"]
        self.assertEqual(prompt["type"], "permission_prompt")
        self.assertEqual(prompt["tool_name"], "Bash")

        callback = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(prompt, "allow_once"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(callback.accepted)
        self.assertEqual(
            transport.permission_approval_calls,
            [("perm-1", {"action": "allow_once"})],
        )

    def test_collaborator_cannot_approve_high_risk_permission_and_token_remains_open(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_permission_event(high_risk=True)]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        prompt = channel.sent_views[0]["view"]
        token = _token_for(prompt, "allow_once")

        denied = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, actor_id="collab"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        accepted = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(denied.accepted)
        self.assertEqual(denied.reason, BlockedReason.UNAUTHORIZED)
        self.assertTrue(accepted.accepted)
        self.assertEqual(len(transport.permission_approval_calls), 1)

    def test_collaborator_can_approve_low_risk_permission(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_permission_event(high_risk=False)]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        prompt = channel.sent_views[0]["view"]

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(prompt, "allow_once"), actor_id="collab"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            transport.permission_approval_calls,
            [("perm-1", {"action": "allow_once"})],
        )

    def test_disabled_permission_callback_does_not_consume_token_or_call_transport(self):
        orchestrator, transport, _channel, interactions, session = _orchestrator(
            caps=_transport_caps(permission_callback=False)
        )
        ctx = interactions.register_permission(
            session_id=session.session_id,
            generation=session.generation,
            tool_name="Read",
            tool_input={"file": "README.md"},
            actions=["allow_once"],
            transport_request_id="perm-disabled",
        )
        view = ViewModelFactory(interactions).permission_prompt(ctx)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(view, "allow_once"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertIsNone(interactions.get(ctx.interaction_id).decision)
        self.assertEqual(transport.permission_approval_calls, [])

    def test_claude_headless_delegates_permission_approval_to_injected_client(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def approve_permission(self, rid: str, decision: dict):
                self.calls.append((rid, decision))

            async def events(self):
                return []

            async def submit(self, _turn):
                return None

        client = Client()
        transport = ClaudeHeadlessTransport(client_factory=lambda _spec: client)
        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        asyncio.run(transport.approve_permission(handle, "perm-1", {"action": "allow_once"}))

        self.assertEqual(client.calls, [("perm-1", {"action": "allow_once"})])


if __name__ == "__main__":
    unittest.main()
