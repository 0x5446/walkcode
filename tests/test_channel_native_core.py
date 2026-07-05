import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DeliveryStatus,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _binding() -> ChannelBinding:
    return ChannelBinding(
        channel_kind="fake",
        account_id="bot",
        chat_id="chat-1",
        thread_id="topic-1",
        root_message_id="root-1",
    )


def _actor() -> ActorRef:
    return ActorRef(channel_kind="fake", actor_id="user-1", display_name="User One")


def _channel_caps(**overrides) -> ChannelCapabilities:
    data = {
        "thread_context": True,
        "editable_message": True,
        "interactive_message": True,
        "interactive_update": True,
        "private_callback_ack": True,
        "toast_or_ephemeral_notice": True,
        "force_reply": True,
        "attachment_download": True,
        "forum_or_topic": True,
        "max_text_chars": 4096,
        "max_callback_payload_bytes": 64,
    }
    data.update(overrides)
    return ChannelCapabilities(**data)


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


class _SlowStatusChannel(FakeChannelAdapter):
    def __init__(self, kind: str, capabilities: ChannelCapabilities, clock: _Clock):
        super().__init__(kind, capabilities)
        self.clock = clock

    async def send_view(self, binding: ChannelBinding, view_model: dict) -> str:
        if view_model.get("type") == "health":
            self.clock.now += 31.0
        return await super().send_view(binding, view_model)


class ChannelNativeCoreContractTests(unittest.TestCase):
    def test_fake_channel_transport_turn_flow(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "hello "}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )

        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="fake-transport",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="build it"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["build it"])
        self.assertIn("hello ", channel.rendered_text())
        self.assertIn("done", channel.rendered_text())

    def test_status_card_is_created_once_and_edited_on_progress(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "working"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        binding = _binding()
        binding.capabilities["status_card"] = True

        session = asyncio.run(
            orchestrator.start_session(
                binding=binding,
                transport_kind="fake-transport",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="build it"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        created_cards = [
            item for item in channel.sent_views
            if item["view"].get("type") == "health" and not item.get("edited")
        ]
        edited_cards = [
            item for item in channel.sent_views
            if item["view"].get("type") == "health" and item.get("edited")
        ]
        self.assertEqual(len(created_cards), 1)
        self.assertGreaterEqual(len(edited_cards), 1)
        self.assertEqual(session.channel_binding.health_message_id, "msg-1")

    def test_initial_turn_is_submitted_before_slow_status_card_can_expire_lease(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = _SlowStatusChannel("fake", _channel_caps(), clock)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        binding = _binding()
        binding.capabilities["status_card"] = True

        session = asyncio.run(
            orchestrator.start_session(
                binding=binding,
                transport_kind="fake-transport",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="build it"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["build it"])
        self.assertEqual(sessions.get(session.session_id).lifecycle_state, "IDLE")

    def test_completed_turn_releases_lease_and_persists_agent_session_id(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "claude_headless",
            _transport_caps(),
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {"message": "done", "session_id": "agent-session-1"},
                )
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"claude_headless": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor())
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="first"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = sessions.get(session.session_id)
        self.assertTrue(result.accepted)
        self.assertEqual(updated.lifecycle_state, "IDLE")
        self.assertIsNone(updated.writer_lease)
        self.assertEqual(updated.transport_ref["agent_session_id"], "agent-session-1")

    def test_incomplete_event_stream_releases_lease_as_recoverable(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "partial"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="first"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = sessions.get(session.session_id)
        self.assertTrue(result.accepted)
        self.assertEqual(updated.lifecycle_state, "ERROR_RECOVERABLE")
        self.assertEqual(updated.last_progress_event, "turn.event_stream_incomplete")
        self.assertIsNone(updated.writer_lease)

    def test_completed_turn_does_not_send_duplicate_final_text_after_delta(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "same answer"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "same answer"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="hello"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(channel.sent_views), 1)
        self.assertEqual(channel.sent_views[0]["view"], {"type": "turn_delta", "text": "same answer"})

    def test_empty_completed_turn_updates_state_without_sending_blank_message(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "codex_app_server",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "OK"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "", "thread_id": "thread-1"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"codex_app_server": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "codex_app_server", "/tmp/project", _actor())
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="hello"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = sessions.get(session.session_id)
        self.assertTrue(result.accepted)
        self.assertEqual(updated.lifecycle_state, "IDLE")
        self.assertEqual(updated.transport_ref["thread_id"], "thread-1")
        self.assertEqual(len(channel.sent_views), 1)
        self.assertEqual(channel.sent_views[0]["view"], {"type": "turn_delta", "text": "OK"})

    def test_idle_session_reacquires_writer_before_followup_submit(self):
        class BatchedTransport(FakeAgentTransport):
            def __init__(self):
                super().__init__("claude_headless", _transport_caps())
                self.event_batches = [
                    [
                        AgentEvent(
                            AgentEventType.TURN_COMPLETED,
                            {"message": "first", "session_id": "agent-session-1"},
                        )
                    ],
                    [
                        AgentEvent(
                            AgentEventType.TURN_COMPLETED,
                            {"message": "second", "session_id": "agent-session-1"},
                        )
                    ],
                ]

            async def events(self, handle):
                return self.event_batches.pop(0)

        clock = _Clock()
        sessions = SessionRegistry(now=clock, lease_ttl=10.0)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = BatchedTransport()
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"claude_headless": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor())
        )
        first = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="first"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        clock.now += 60.0

        second = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="second"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = sessions.get(session.session_id)
        self.assertTrue(first.accepted)
        self.assertTrue(second.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["first", "second"])
        self.assertEqual(transport.call_log, ["submit_turn", "resume", "submit_turn"])
        self.assertEqual(transport.resume_specs[0].resume_ref["agent_session_id"], "agent-session-1")
        self.assertEqual(updated.lifecycle_state, "IDLE")
        self.assertIsNone(updated.writer_lease)

    def test_error_recoverable_session_reacquires_writer_before_followup_submit(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock, lease_ttl=10.0)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "claude_headless",
            _transport_caps(),
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {"message": "recovered", "session_id": "agent-session-1"},
                )
            ],
        )
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"claude_headless": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor())
        )
        session.transport_ref["agent_session_id"] = "agent-session-1"
        session.lifecycle_state = "ERROR_RECOVERABLE"
        session.last_progress_event = AgentEventType.SESSION_ERROR
        session.writer_lease = None
        clock.now += 60.0

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="try again"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = sessions.get(session.session_id)
        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["try again"])
        self.assertEqual(transport.call_log, ["resume", "submit_turn"])
        self.assertEqual(transport.resume_specs[0].resume_ref["agent_session_id"], "agent-session-1")
        self.assertEqual(updated.lifecycle_state, "IDLE")
        self.assertEqual(updated.last_progress_event, AgentEventType.TURN_COMPLETED)

    def test_interaction_decision_is_write_once_and_stale_generation_is_rejected(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_permission(
            session_id="s1",
            generation=3,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
            actions=["allow", "deny"],
        )
        allow = store.create_callback_token(ctx.interaction_id, "allow", generation=3)
        deny = store.create_callback_token(ctx.interaction_id, "deny", generation=3)

        first = store.decide_from_token(allow, actor=_actor(), current_generation=3)
        second = store.decide_from_token(deny, actor=_actor(), current_generation=3)

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, BlockedReason.ALREADY_DECIDED)
        self.assertEqual(store.get(ctx.interaction_id).decision["action"], "allow")

        stale_ctx = store.register_permission(
            session_id="s1",
            generation=2,
            tool_name="Read",
            tool_input={"file": "x"},
            actions=["allow"],
        )
        stale = store.create_callback_token(stale_ctx.interaction_id, "allow", generation=2)
        stale_result = store.decide_from_token(stale, actor=_actor(), current_generation=3)
        self.assertFalse(stale_result.accepted)
        self.assertEqual(stale_result.reason, BlockedReason.STALE_GENERATION)

    def test_durable_outbox_retries_transient_and_drops_permanent_failure(self):
        outbox = DurableOutbox(now=_Clock())
        item = outbox.enqueue(
            channel_binding_key=("fake", "bot", "chat-1", "topic-1", "root-1"),
            view_model={"type": "text", "text": "hello"},
            idempotency_key="k1",
        )

        self.assertEqual(outbox.pending_count(), 1)
        outbox.record_result(item.delivery_id, DeliveryStatus.TRANSIENT_FAILURE)
        self.assertEqual(outbox.pending_count(), 1)
        self.assertEqual(outbox.get(item.delivery_id).attempt_count, 1)

        outbox.record_result(item.delivery_id, DeliveryStatus.PERMANENT_FAILURE)
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.dead_count(), 1)

    def test_pending_binding_can_be_committed_to_session(self):
        sessions = SessionRegistry(now=_Clock())
        pending_key = sessions.add_pending_binding(
            pending_key="launch-1",
            binding=_binding(),
            cwd="/tmp/project",
        )

        self.assertEqual(sessions.resolve_pending_by_binding(_binding().key()), pending_key)

        session = sessions.commit_pending(
            pending_key,
            session_id="sess-1",
            transport_kind="fake-transport",
            transport_ref={"handle_id": "h1"},
            owner=_actor(),
        )

        self.assertEqual(session.session_id, "sess-1")
        self.assertEqual(sessions.resolve_binding(_binding().key()), "sess-1")
        self.assertIsNone(sessions.resolve_pending_by_binding(_binding().key()))

    def test_unknown_event_falls_back_to_text_rendering(self):
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent("vendor.future_event", {"x": 1})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="go"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertIn("vendor.future_event", channel.rendered_text())

    def test_capability_disabled_blocks_submit(self):
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(structured_input=False),
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
        )
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="go"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(transport.submitted_turns, [])

    def test_writer_lease_and_generation_gate_submits(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock, lease_ttl=10.0)
        channel = FakeChannelAdapter("fake", _channel_caps())
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
        )

        stale = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="old"),
                actor=_actor(),
                generation=session.generation - 1,
            )
        )
        self.assertFalse(stale.accepted)
        self.assertEqual(stale.reason, BlockedReason.STALE_GENERATION)

        clock.now += 11
        expired = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="after expiry"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        self.assertFalse(expired.accepted)
        self.assertEqual(expired.reason, BlockedReason.LEASE_EXPIRED)

    def test_external_observed_session_blocks_input(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_observed_session(
            session_id="observed-1",
            binding=_binding(),
            cwd="/tmp/project",
            external_ref={"pid": 123},
            owner=_actor(),
        )

        result = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="please run this"),
            generation=session.generation,
        )

        self.assertTrue(result.blocked_input_id)
        self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        self.assertEqual(
            sessions.get(session.session_id).blocked_inputs[result.blocked_input_id].text,
            "please run this",
        )


class ChannelNativeInboundTests(unittest.TestCase):
    def test_inbound_event_resolves_binding_key(self):
        event = InboundEvent(
            event_id="e1",
            channel_kind="fake",
            account_id="bot",
            chat_id="chat-1",
            thread_id="topic-1",
            message_id="m1",
            root_message_id="root-1",
            sender_id="user-1",
            sender_display="User One",
            text="hello",
        )

        self.assertEqual(event.binding_key(), ("fake", "bot", "chat-1", "topic-1", "root-1"))
