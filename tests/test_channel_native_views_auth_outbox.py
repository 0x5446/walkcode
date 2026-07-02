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
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InboundLedger,
    InteractionStore,
    LarkBotApi,
    LarkChannelAdapter,
    Orchestrator,
    OutboxDispatcher,
    PermanentDeliveryError,
    SessionRegistry,
    SessionRole,
    TelegramBotApi,
    TelegramChannelAdapter,
    TransientDeliveryError,
    TransportCapabilities,
    TurnInput,
    ViewModelFactory,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "u1") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=f"User {actor_id}")


def _binding(kind: str = "telegram") -> ChannelBinding:
    return ChannelBinding(
        channel_kind=kind,
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        root_message_id="root",
    )


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


class ViewModelRenderingTests(unittest.TestCase):
    def test_permission_prompt_renders_short_tokens_for_telegram_and_lark(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_permission(
            session_id="s1",
            generation=3,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
            actions=["allow_once", "deny"],
        )
        view = ViewModelFactory(store).permission_prompt(ctx)

        telegram_calls = []

        async def telegram_caller(method, payload):
            telegram_calls.append((method, payload))
            return {"result": {"message_id": 10}}

        telegram = TelegramChannelAdapter(TelegramBotApi("token", caller=telegram_caller))
        msg_id = asyncio.run(telegram.send_view(_binding("telegram"), view))

        self.assertEqual(msg_id, "10")
        self.assertEqual(telegram_calls[0][0], "sendMessage")
        buttons = telegram_calls[0][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([button["text"] for row in buttons for button in row], ["Allow once", "Deny"])
        for row in buttons:
            for button in row:
                self.assertLessEqual(len(button["callback_data"]), 64)
                self.assertTrue(button["callback_data"].startswith("cb:"))

        lark_calls = []

        async def lark_caller(method, payload):
            lark_calls.append((method, payload))
            return {"data": {"message_id": "om_1"}}

        lark = LarkChannelAdapter(LarkBotApi(caller=lark_caller))
        lark_id = asyncio.run(lark.send_view(_binding("lark"), view))

        self.assertEqual(lark_id, "om_1")
        self.assertEqual(lark_calls[0][0], "sendCard")
        self.assertEqual(lark_calls[0][1]["view"]["type"], "permission_prompt")
        self.assertNotIn("feishu_root_msg_id", lark_calls[0][1]["view"])

    def test_ask_user_question_other_uses_awaiting_state(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_ask_user_question(
            session_id="s1",
            generation=4,
            questions=[{"prompt": "Pick one", "options": ["A", "B"], "allow_other": True}],
        )
        view = ViewModelFactory(store).ask_user_question_prompt(ctx)

        labels = [action["label"] for action in view["actions"]]
        self.assertEqual(labels, ["A", "B", "Other"])

        store.begin_awaiting_other(ctx.interaction_id, _binding("telegram").key(), question_index=0)
        result = store.answer_awaiting_other(
            _binding("telegram").key(),
            actor=_actor(),
            text="custom answer",
            current_generation=4,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(store.get(ctx.interaction_id).answers[0], "custom answer")

    def test_health_error_command_and_takeover_views_are_platform_neutral(self):
        factory = ViewModelFactory(InteractionStore(now=_Clock()))

        health = factory.health_view(
            status="running",
            title="Build",
            session_id="s1",
            transport="claude_headless",
            elapsed=12.5,
            cwd="/tmp/project",
        )
        error = factory.error_view(code="transport_missing", message="SDK missing", retryable=False)
        menu = factory.command_menu([{"action": "interrupt", "label": "Interrupt"}])
        takeover = factory.takeover_prompt(
            takeover_id="t1",
            blocked_input_id="b1",
            recoverability="native_resume_available",
            summary="run tests",
        )

        for view in (health, error, menu, takeover):
            self.assertIsInstance(view, dict)
            self.assertIn("type", view)
            self.assertNotIn("telegram_html", view)
            self.assertNotIn("lark_card", view)


class AskUserQuestionStateMachineTests(unittest.TestCase):
    def test_multi_question_single_select_advances_then_finalizes(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_ask_user_question(
            session_id="s1",
            generation=2,
            questions=[
                {"prompt": "First", "options": ["A", "B"]},
                {"prompt": "Second", "options": ["X", "Y"]},
            ],
        )
        factory = ViewModelFactory(store)
        first = factory.ask_user_question_prompt(ctx)
        first_token = next(action["token"] for action in first["actions"] if action["label"] == "A")

        first_result = store.decide_from_token(
            first_token,
            actor=_actor("owner"),
            current_generation=2,
            binding_key=_binding().key(),
        )

        self.assertTrue(first_result.accepted)
        self.assertIsNone(store.get(ctx.interaction_id).decision)
        self.assertEqual(store.get(ctx.interaction_id).current_index, 1)
        self.assertEqual(store.get(ctx.interaction_id).answers[0], "A")

        second = factory.ask_user_question_prompt(ctx)
        self.assertEqual(second["prompt"], "Second")
        second_token = next(action["token"] for action in second["actions"] if action["label"] == "Y")
        second_result = store.decide_from_token(
            second_token,
            actor=_actor("owner"),
            current_generation=2,
            binding_key=_binding().key(),
        )

        self.assertTrue(second_result.accepted)
        self.assertEqual(
            store.get(ctx.interaction_id).decision,
            {"action": "answers", "answers": {0: "A", 1: "Y"}},
        )

    def test_multi_select_toggles_options_and_requires_submit(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_ask_user_question(
            session_id="s1",
            generation=2,
            questions=[{"prompt": "Pick", "options": ["A", "B"], "allow_multiple": True}],
        )
        factory = ViewModelFactory(store)
        view = factory.ask_user_question_prompt(ctx)
        self.assertEqual([action["label"] for action in view["actions"]], ["A", "B", "Submit"])

        a_token = next(action["token"] for action in view["actions"] if action["label"] == "A")
        self.assertTrue(
            store.decide_from_token(
                a_token,
                actor=_actor("owner"),
                current_generation=2,
                binding_key=_binding().key(),
            ).accepted
        )
        self.assertIsNone(store.get(ctx.interaction_id).decision)
        self.assertEqual(store.get(ctx.interaction_id).answers[0], ["A"])

        selected_view = factory.ask_user_question_prompt(ctx)
        self.assertEqual(selected_view["actions"][0]["label"], "[x] A")
        b_token = next(action["token"] for action in selected_view["actions"] if action["label"] == "B")
        submit_token = next(action["token"] for action in selected_view["actions"] if action["label"] == "Submit")
        store.decide_from_token(
            b_token,
            actor=_actor("owner"),
            current_generation=2,
            binding_key=_binding().key(),
        )
        submit_result = store.decide_from_token(
            submit_token,
            actor=_actor("owner"),
            current_generation=2,
            binding_key=_binding().key(),
        )

        self.assertTrue(submit_result.accepted)
        self.assertEqual(
            store.get(ctx.interaction_id).decision,
            {"action": "answers", "answers": {0: ["A", "B"]}},
        )

    def test_other_callback_enters_awaiting_state_and_text_finalizes(self):
        store = InteractionStore(now=_Clock())
        ctx = store.register_ask_user_question(
            session_id="s1",
            generation=2,
            questions=[{"prompt": "Pick", "options": ["A"], "allow_other": True}],
        )
        view = ViewModelFactory(store).ask_user_question_prompt(ctx)
        token = next(action["token"] for action in view["actions"] if action["label"] == "Other")

        result = store.decide_from_token(
            token,
            actor=_actor("owner"),
            current_generation=2,
            binding_key=_binding().key(),
        )

        self.assertTrue(result.accepted)
        self.assertEqual(store.awaiting_other_count(), 1)
        self.assertIsNone(store.get(ctx.interaction_id).decision)

        answered = store.answer_awaiting_other(
            _binding().key(),
            actor=_actor("owner"),
            text="custom",
            current_generation=2,
        )

        self.assertTrue(answered.accepted)
        self.assertEqual(
            store.get(ctx.interaction_id).decision,
            {"action": "answers", "answers": {0: "custom"}},
        )
        self.assertEqual(store.awaiting_other_count(), 0)

    def test_orchestrator_routes_other_callback_and_answer_text_before_agent_turn(self):
        clock = _Clock()
        interactions = InteractionStore(now=clock)
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=interactions,
            outbox=DurableOutbox(now=clock),
            channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding("telegram"), "fake-transport", "/tmp/project", _actor("owner"))
        )
        ctx = interactions.register_ask_user_question(
            session_id=session.session_id,
            generation=session.generation,
            questions=[{"prompt": "Pick", "options": ["A"], "allow_other": True}],
        )
        view = ViewModelFactory(interactions).ask_user_question_prompt(ctx)
        token = next(action["token"] for action in view["actions"] if action["label"] == "Other")

        callback = InboundEvent(
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
        callback_result = asyncio.run(
            orchestrator.handle_inbound_event(callback, agent_transport_kind="fake-transport", cwd="/tmp/project")
        )
        self.assertTrue(callback_result.accepted)
        self.assertEqual(interactions.awaiting_other_count(), 1)

        text = InboundEvent(
            event_id="txt-1",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-text",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="custom answer",
        )
        text_result = asyncio.run(
            orchestrator.handle_inbound_event(text, agent_transport_kind="fake-transport", cwd="/tmp/project")
        )

        self.assertTrue(text_result.accepted)
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(
            interactions.get(ctx.interaction_id).decision,
            {"action": "answers", "answers": {0: "custom answer"}},
        )


class AuthorizationTests(unittest.TestCase):
    def test_roles_gate_submit_high_risk_decision_and_takeover(self):
        authz = AuthorizationStore(now=_Clock())
        authz.grant("s1", _actor("owner"), SessionRole.OWNER)
        authz.grant("s1", _actor("collab"), SessionRole.COLLABORATOR)
        authz.grant("s1", _actor("reviewer"), SessionRole.REVIEWER)
        authz.grant("s1", _actor("admin"), SessionRole.ADMIN)

        self.assertTrue(authz.can_submit("s1", _actor("owner")).allowed)
        self.assertTrue(authz.can_submit("s1", _actor("collab")).allowed)
        self.assertFalse(authz.can_submit("s1", _actor("reviewer")).allowed)

        self.assertTrue(authz.can_decide_permission("s1", _actor("owner"), high_risk=True).allowed)
        self.assertFalse(authz.can_decide_permission("s1", _actor("collab"), high_risk=True).allowed)
        self.assertTrue(authz.can_takeover("s1", _actor("admin")).allowed)
        self.assertFalse(authz.can_takeover("s1", _actor("reviewer")).allowed)

    def test_orchestrator_denies_unprivileged_input_without_calling_transport(self):
        authz = AuthorizationStore(now=_Clock())
        sessions = SessionRegistry(now=_Clock())
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
            transports={"fake-transport": transport},
            authz=authz,
            now=_Clock(),
        )
        session = asyncio.run(
            orchestrator.start_session(
                _binding("telegram"),
                "fake-transport",
                "/tmp/project",
                _actor("owner"),
            )
        )
        authz.grant(session.session_id, _actor("reviewer"), SessionRole.REVIEWER)

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="should not run"),
                actor=_actor("reviewer"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.UNAUTHORIZED)
        self.assertEqual(transport.submitted_turns, [])


class OutboxAndInboundTests(unittest.TestCase):
    def test_inbound_ledger_prevents_duplicate_turns(self):
        ledger = InboundLedger(now=_Clock())
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
            transports={"fake-transport": transport},
            inbound_ledger=ledger,
            now=_Clock(),
        )
        inbound = InboundEvent(
            event_id="evt-1",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m1",
            root_message_id="",
            sender_id="owner",
            sender_display="Owner",
            text="run",
        )

        first = asyncio.run(orchestrator.handle_inbound_event(inbound, agent_transport_kind="fake-transport", cwd="/tmp/project"))
        second = asyncio.run(orchestrator.handle_inbound_event(inbound, agent_transport_kind="fake-transport", cwd="/tmp/project"))

        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, BlockedReason.DUPLICATE_INBOUND)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run"])

    def test_orchestrator_enqueues_output_before_dispatch(self):
        outbox = DurableOutbox(now=_Clock())
        channel = FakeChannelAdapter("telegram", _channel_caps())
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=outbox,
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        session = asyncio.run(orchestrator.start_session(_binding("telegram"), "fake-transport", "/tmp/project", _actor()))
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.sent_count(), 1)
        self.assertIn("done", channel.rendered_text())

    def test_outbox_dispatcher_maps_transient_and_permanent_failures(self):
        outbox = DurableOutbox(now=_Clock())
        transient = FakeChannelAdapter("telegram", _channel_caps())
        permanent = FakeChannelAdapter("lark", _channel_caps())

        async def transient_send(_binding, _view):
            raise TransientDeliveryError("rate limited")

        async def permanent_send(_binding, _view):
            raise PermanentDeliveryError("bad chat")

        transient.send_view = transient_send
        permanent.send_view = permanent_send
        outbox.enqueue(
            channel_binding_key=("telegram", "bot", "chat", "topic", "root"),
            view_model={"type": "text", "text": "retry"},
            idempotency_key="k1",
        )
        outbox.enqueue(
            channel_binding_key=("lark", "bot", "chat", "topic", "root"),
            view_model={"type": "text", "text": "dead"},
            idempotency_key="k2",
        )

        dispatcher = OutboxDispatcher(outbox, {"telegram": transient, "lark": permanent})
        asyncio.run(dispatcher.flush_once())

        self.assertEqual(outbox.pending_count(), 1)
        self.assertEqual(outbox.dead_count(), 1)
