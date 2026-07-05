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


def _ask_event(questions=None) -> AgentEvent:
    return AgentEvent(
        "ask_user.requested",
        {
            "rid": "ask-1",
            "questions": questions
            or [{"prompt": "Pick one", "options": ["A", "B"], "allow_other": True}],
        },
    )


def _callback(token: str, *, actor_id: str = "owner") -> InboundEvent:
    return InboundEvent(
        event_id=f"cb-{actor_id}-{token[:4]}",
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


def _text(text: str, *, actor_id: str = "owner") -> InboundEvent:
    return InboundEvent(
        event_id=f"txt-{actor_id}",
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id=f"m-text-{actor_id}",
        root_message_id="root",
        sender_id=actor_id,
        sender_display=actor_id.title(),
        text=text,
    )


def _token_for(view: dict, label: str) -> str:
    return next(item["token"] for item in view["actions"] if item["label"] == label)


def _form_callback(token: str, form: dict, *, actor_id: str = "owner") -> InboundEvent:
    event = _callback(token, actor_id=actor_id)
    return InboundEvent(
        **{
            **event.__dict__,
            "callback": {"token": token, "form": form},
        }
    )


class AskUserQuestionRoundTripTests(unittest.TestCase):
    def test_single_simple_question_finalizes_on_one_click(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_ask_event([{"prompt": "Pick one", "options": ["A", "B"]}])]
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
        self.assertEqual(prompt["type"], "ask_user_question")
        self.assertEqual(prompt["questions"][0]["prompt"], "Pick one")
        self.assertIsNone(prompt["submit"])

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(prompt, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "A"})])

    def test_multi_question_delivers_only_after_final_answer(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[
                _ask_event(
                    [
                        {"prompt": "First", "options": ["A", "B"]},
                        {"prompt": "Second", "options": ["X", "Y"]},
                    ]
                )
            ]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]
        # both questions live in one card; selecting each just updates
        set_a = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(set_a.accepted)
        self.assertEqual(transport.question_answer_calls, [])
        set_y = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "Y"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(set_y.accepted)
        self.assertEqual(transport.question_answer_calls, [])

        submit_token = card["submit"]["token"]
        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(submit_token, actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(final.accepted)
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "A", 1: "Y"})])

    def test_other_text_answer_is_delivered_before_agent_input(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_ask_event()]
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
        other = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(prompt, "Other"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(other.accepted)

        answered = asyncio.run(
            orchestrator.handle_inbound_event(
                _text("custom", actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(answered.accepted)
        # free text fills the answer; batch still needs an explicit submit
        self.assertEqual(transport.question_answer_calls, [])

        submit_token = channel.sent_views[0]["view"]["submit"]["token"]
        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(submit_token, actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(final.accepted)
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "custom"})])
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run"])

    def test_reviewer_cannot_answer_question_and_token_remains_open(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_ask_event()]
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
        token = _token_for(prompt, "A")

        denied = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, actor_id="reviewer"),
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
        submitted = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(prompt["submit"]["token"], actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(denied.accepted)
        self.assertEqual(denied.reason, BlockedReason.UNAUTHORIZED)
        self.assertTrue(accepted.accepted)
        self.assertTrue(submitted.accepted)
        self.assertEqual(len(transport.question_answer_calls), 1)

    def test_form_submit_delivers_all_answers_in_one_callback(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[
                _ask_event(
                    [
                        {"prompt": "First", "options": ["A", "B"], "allow_other": True},
                        {"prompt": "Second", "options": ["X", "Y"], "allow_multiple": True},
                    ]
                )
            ]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]
        submit_token = card["submit"]["token"]

        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _form_callback(
                    submit_token,
                    {"q0": "1", "q0_other": "", "q1": ["0", "1"]},
                    actor_id="owner",
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(final.accepted)
        self.assertEqual(
            transport.question_answer_calls, [("ask-1", {0: "B", 1: ["X", "Y"]})]
        )

    def test_form_other_text_overrides_selected_option(self):
        orchestrator, transport, channel, _interactions, session = _orchestrator(
            scripted_events=[_ask_event()]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]

        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _form_callback(
                    card["submit"]["token"],
                    {"q0": "0", "q0_other": "  custom text  "},
                    actor_id="owner",
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(final.accepted)
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "custom text"})])

    def test_submit_without_answers_reports_incomplete_and_keeps_card_open(self):
        orchestrator, transport, channel, interactions, session = _orchestrator(
            scripted_events=[_ask_event()]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]
        submit_token = card["submit"]["token"]

        incomplete = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(submit_token, actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(incomplete.accepted)
        self.assertEqual(transport.question_answer_calls, [])
        self.assertIsNone(interactions.get(card["interaction_id"]).decision)
        notes = [
            item["view"].get("text", "")
            for item in channel.sent_views
            if item["view"].get("type") == "text"
        ]
        self.assertTrue(any("未回答" in note for note in notes))

        # answering then reusing the same submit token settles the batch
        asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(submit_token, actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(final.accepted)
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "A"})])

    def test_plain_message_after_submit_goes_to_agent_not_swallowed(self):
        orchestrator, transport, channel, interactions, session = _orchestrator(
            scripted_events=[_ask_event()]
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]
        # user clicks "其他", changes their mind, picks an option, submits
        asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "Other"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        final = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(card["submit"]["token"], actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(final.accepted)
        self.assertEqual(interactions.awaiting_other_count(), 0)

        # the next plain message must reach the agent, not be swallowed as a
        # stale "other" answer for the settled question
        followup = asyncio.run(
            orchestrator.handle_inbound_event(
                _text("next task", actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        self.assertTrue(followup.accepted)
        self.assertEqual(
            [turn.text for turn in transport.submitted_turns], ["run", "next task"]
        )
        self.assertEqual(transport.question_answer_calls, [("ask-1", {0: "A"})])

    def test_update_edit_returning_false_falls_back_to_new_card(self):
        class _EditFailChannel(FakeChannelAdapter):
            async def edit_view(self, binding, message_id, view_model):
                await super().edit_view(binding, message_id, view_model)
                return False

        clock = _Clock()
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                _ask_event([{"prompt": "Pick", "options": ["A", "B"], "allow_multiple": True}])
            ],
        )
        channel = _EditFailChannel("telegram", _channel_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor("owner"))
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        card = channel.sent_views[0]["view"]

        toggled = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(card, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(toggled.accepted)
        # edit reported failure (False) → a fresh ask card must be sent
        # (FakeChannelAdapter records edits into sent_views too — exclude them)
        ask_cards = [
            item
            for item in channel.sent_views
            if item["view"].get("type") == "ask_user_question" and not item.get("edited")
        ]
        self.assertEqual(len(ask_cards), 2)

    def test_disabled_question_capability_does_not_consume_token(self):
        orchestrator, transport, _channel, interactions, session = _orchestrator(
            caps=_transport_caps(ask_user_question=False)
        )
        ctx = interactions.register_ask_user_question(
            session_id=session.session_id,
            generation=session.generation,
            questions=[{"prompt": "Pick", "options": ["A"]}],
            transport_request_id="ask-disabled",
        )
        view = ViewModelFactory(interactions).ask_user_question_prompt(ctx)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(view, "A"), actor_id="owner"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertIsNone(interactions.get(ctx.interaction_id).decision)
        self.assertEqual(transport.question_answer_calls, [])

    def test_claude_headless_delegates_question_answer_to_injected_client(self):
        class Client:
            def __init__(self):
                self.calls = []

            async def answer_user_question(self, rid: str, answers: dict):
                self.calls.append((rid, answers))

            async def events(self):
                return []

            async def submit(self, _turn):
                return None

        client = Client()
        transport = ClaudeHeadlessTransport(client_factory=lambda _spec: client)
        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        asyncio.run(transport.answer_user_question(handle, "ask-1", {0: "A"}))

        self.assertEqual(client.calls, [("ask-1", {0: "A"})])


if __name__ == "__main__":
    unittest.main()
