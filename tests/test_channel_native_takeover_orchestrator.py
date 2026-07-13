import asyncio
import unittest

from walkcode.channel_native import (
    HANDOFF_CONTINUE_PROMPT,
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    FakeExternalTuiController,
    InboundEvent,
    InteractionStore,
    Orchestrator,
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


def _setup(*, resume_ref=None, caps=None, transport=None, handoff_continue="off"):
    clock = _Clock()
    sessions = SessionRegistry(now=clock)
    interactions = InteractionStore(now=clock)
    outbox = DurableOutbox(now=clock)
    authz = AuthorizationStore(now=clock)
    channel = FakeChannelAdapter("telegram", _channel_caps())
    transport = transport or FakeAgentTransport("fake-transport", caps or _transport_caps())
    orchestrator = Orchestrator(
        sessions=sessions,
        interactions=interactions,
        outbox=outbox,
        channels={"telegram": channel},
        transports={"fake-transport": transport},
        external_tui_controllers={"fake-process": FakeExternalTuiController("fake-process")},
        authz=authz,
        handoff_continue=handoff_continue,
        now=clock,
    )
    external_ref = {}
    if resume_ref is not None:
        external_ref["resume_ref"] = resume_ref
        external_ref["terminate_ref"] = {
            "controller_kind": "fake-process",
            "process_ref": {"pid": 123},
        }
    session = sessions.create_observed_session(
        session_id="observed-1",
        binding=_binding(),
        cwd="/tmp/project",
        external_ref=external_ref,
        owner=_actor("owner"),
    )
    authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
    authz.grant(session.session_id, _actor("admin"), SessionRole.ADMIN)
    authz.grant(session.session_id, _actor("collab"), SessionRole.COLLABORATOR)
    authz.grant(session.session_id, _actor("reviewer"), SessionRole.REVIEWER)
    return orchestrator, channel, transport, session


def _takeover_token(channel: FakeChannelAdapter) -> str:
    view = channel.sent_views[-1]["view"]
    return next(action["token"] for action in view["actions"] if action["action"] == "takeover_and_send")


def _confirmation_token(channel: FakeChannelAdapter) -> str:
    view = channel.sent_views[-1]["view"]
    return next(action["token"] for action in view["actions"] if action["action"] == "confirm_takeover")


def _callback(token: str, *, sender_id: str = "owner", event_id: str = "cb-1") -> InboundEvent:
    return InboundEvent(
        event_id=event_id,
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id="m-cb",
        root_message_id="root",
        sender_id=sender_id,
        sender_display=sender_id.title(),
        text=f"cb:{token}",
        callback={"token": token},
    )


class TakeoverOrchestratorTests(unittest.TestCase):
    def test_blocked_observed_input_renders_takeover_prompt(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        self.assertTrue(result.blocked_input_id)
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_prompt")
        self.assertIn("takeover_id", channel.sent_views[-1]["view"])
        self.assertEqual(
            [action["action"] for action in channel.sent_views[-1]["view"]["actions"]],
            ["takeover_and_send"],
        )
        self.assertTrue(_takeover_token(channel))

    def test_blocked_readonly_topic_input_keeps_user_message_and_renders_takeover_prompt(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        session.channel_binding.capabilities["readonly_topic"] = True
        event = InboundEvent(
            event_id="msg-1",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-readonly",
            root_message_id="",
            sender_id="owner",
            sender_display="Owner",
            text="run tests",
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(channel.deleted_messages, [])
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_prompt")

    def test_stopped_observed_topic_input_can_take_over_without_terminating_tui(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        session.status = "stopped"
        session.lifecycle_state = "STOPPED"
        session.writer_owner = None
        session.writer_lease = None
        session.stop_reason = "external_tui_stop"
        session.transport_ref.pop("terminate_ref", None)
        event = InboundEvent(
            event_id="msg-stopped",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-stopped",
            root_message_id="",
            sender_id="owner",
            sender_display="Owner",
            text="continue from telegram",
        )

        blocked = asyncio.run(
            orchestrator.handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        token = _takeover_token(channel)
        confirmed = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, event_id="cb-stopped-1"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(blocked.accepted)
        self.assertEqual(blocked.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        self.assertTrue(confirmed.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["continue from telegram"])
        self.assertEqual(orchestrator.external_tui_controllers["fake-process"].terminate_calls, [])
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.status, "running")
        self.assertEqual(updated.writer_owner.kind, "orchestrator")

    def test_status_card_takeover_button_runs_takeover_without_input(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        session.channel_binding.capabilities["status_card"] = True
        asyncio.run(orchestrator.refresh_session_status_card(session))

        card = channel.sent_views[-1]["view"]
        self.assertEqual(card["type"], "health")
        self.assertEqual(card["actions"][0]["action"], "request_takeover")
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                InboundEvent(
                    event_id="cb-status",
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    thread_id="topic",
                    message_id="m-cb",
                    root_message_id="root",
                    sender_id="owner",
                    sender_display="Owner",
                    text="request_takeover",
                    callback={"token": "request_takeover", "data": "request_takeover"},
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(orchestrator.sessions.get(session.session_id).writer_owner.kind, "orchestrator")

    def test_repeated_status_card_takeover_after_failure_does_not_create_duplicate_progress(self):
        class FailingResumeTransport(FakeAgentTransport):
            async def resume(self, spec):
                self.resume_specs.append(spec)
                self.call_log.append("resume")
                raise RuntimeError("resume failed")

        transport = FailingResumeTransport("fake-transport", _transport_caps())
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            transport=transport,
        )
        session.channel_binding.capabilities["status_card"] = True
        asyncio.run(orchestrator.refresh_session_status_card(session))
        callback = InboundEvent(
            event_id="cb-status-1",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-cb",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="request_takeover",
            callback={"token": "request_takeover", "data": "request_takeover"},
        )

        first = asyncio.run(
            orchestrator.handle_inbound_event(
                callback,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        second = asyncio.run(
            orchestrator.handle_inbound_event(
                InboundEvent(
                    event_id="cb-status-2",
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    thread_id="topic",
                    message_id="m-cb-2",
                    root_message_id="root",
                    sender_id="owner",
                    sender_display="Owner",
                    text="request_takeover",
                    callback={"token": "request_takeover", "data": "request_takeover"},
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        takeovers = [
            tx
            for tx in orchestrator.sessions.to_dict()["takeovers"].values()
            if tx["session_id"] == session.session_id
        ]
        progress_views = [
            item["view"]
            for item in channel.sent_views
            if item["view"].get("type") == "takeover_progress"
        ]
        self.assertFalse(first.accepted)
        self.assertEqual(first.reason, "resume_failed")
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, "resume_failed")
        self.assertEqual(len(takeovers), 1)
        self.assertEqual([view["phase"] for view in progress_views], ["resuming_structured", "failed"])
        self.assertEqual(transport.call_log, ["resume"])

    def test_status_card_takeover_completion_renders_writable_confirmation_without_submitting_input(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        session.channel_binding.capabilities["status_card"] = True
        session.channel_binding.capabilities["static_status_card"] = True
        asyncio.run(orchestrator.refresh_session_status_card(session))

        asyncio.run(
            orchestrator.handle_inbound_event(
                InboundEvent(
                    event_id="cb-status",
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    thread_id="topic",
                    message_id="m-cb",
                    root_message_id="root",
                    sender_id="owner",
                    sender_display="Owner",
                    text="request_takeover",
                    callback={"token": "request_takeover", "data": "request_takeover"},
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(transport.call_log, ["resume"])
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(updated.writer_owner.kind, "orchestrator")
        completed = next(
            item["view"]
            for item in reversed(channel.sent_views)
            if item["view"].get("type") == "takeover_progress" and item["view"].get("phase") == "completed"
        )
        self.assertEqual(completed["summary"], "")

    def test_first_takeover_click_runs_takeover_and_submits(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        self.assertNotIn("Confirm takeover", channel.rendered_text())

    def test_reviewer_takeover_callback_does_not_consume_token(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        denied = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, sender_id="reviewer"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        first_owner_click = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, sender_id="owner", event_id="cb-2"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(denied.accepted)
        self.assertEqual(denied.reason, BlockedReason.UNAUTHORIZED)
        self.assertTrue(first_owner_click.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_missing_resume_ref_becomes_manual_only_without_submitting_input(self):
        orchestrator, channel, transport, session = _setup(resume_ref=None)
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "manual_only")
        self.assertEqual(tx["phase"], TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertEqual(transport.submitted_turns, [])

    def test_disabled_takeover_capability_does_not_consume_token(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            caps=_transport_caps(external_tui_takeover=False),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.CAPABILITY_DISABLED)
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_progress")
        self.assertEqual(channel.sent_views[-1]["view"]["phase"], "failed")
        self.assertEqual(transport.submitted_turns, [])

    def test_successful_takeover_submits_blocked_input_once(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        first = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, event_id="cb-confirm"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )
        second = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, event_id="cb-2"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertTrue(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual(second.reason, BlockedReason.STALE_GENERATION)
        self.assertEqual(updated.writer_owner.kind, "orchestrator")
        self.assertEqual(updated.transport_kind, "fake-transport")
        self.assertEqual(updated.generation, 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        self.assertEqual(channel.sent_views[-1]["view"]["type"], "takeover_progress")
        self.assertEqual(channel.sent_views[-1]["view"]["phase"], "submitting_blocked_input")

    def test_successful_takeover_marks_old_generation_hitl_stale(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        hitl = orchestrator.hitls.register_request(
            session_id=session.session_id,
            generation=session.generation,
            transport_kind=session.transport_kind,
            transport_request_id="perm-1",
            native_method="permission.requested",
            native_params={"prompt": "Allow shell?"},
            prompt_kind="permission",
            channel_binding_key=session.channel_binding.key(),
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, event_id="cb-hitl-stale"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(orchestrator.hitls.get(hitl.hitl_request_id).status, "stale")
        stale_views = [
            item["view"]
            for item in channel.sent_views
            if item["view"].get("type") == "hitl_stale"
        ]
        self.assertEqual(len(stale_views), 1)
        self.assertEqual(stale_views[0]["hitl_request_id"], hitl.hitl_request_id)

    @staticmethod
    def _register_pending_hitl(orchestrator, session):
        return orchestrator.hitls.register_request(
            session_id=session.session_id,
            generation=session.generation,
            transport_kind=session.transport_kind,
            transport_request_id="perm-1",
            native_method="permission.requested",
            native_params={"prompt": "Allow shell?"},
            prompt_kind="permission",
            channel_binding_key=session.channel_binding.key(),
        )

    def _status_card_takeover(self, orchestrator, channel, session):
        session.channel_binding.capabilities["status_card"] = True
        asyncio.run(orchestrator.refresh_session_status_card(session))
        return asyncio.run(
            orchestrator.handle_inbound_event(
                InboundEvent(
                    event_id="cb-status",
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    thread_id="topic",
                    message_id="m-cb",
                    root_message_id="root",
                    sender_id="owner",
                    sender_display="Owner",
                    text="request_takeover",
                    callback={"token": "request_takeover", "data": "request_takeover"},
                ),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

    def test_takeover_only_with_pending_hitl_injects_handoff_continue(self):
        # ADR 0051: takeover-only handoff + orphaned prompt + auto → the
        # agent is re-driven with the invisible continue turn so it re-asks.
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            handoff_continue="auto",
        )
        hitl = self._register_pending_hitl(orchestrator, session)

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        self.assertEqual(
            [turn.text for turn in transport.submitted_turns],
            [HANDOFF_CONTINUE_PROMPT],
        )
        self.assertEqual(orchestrator.hitls.get(hitl.hitl_request_id).status, "stale")

    def test_takeover_only_with_pending_hitl_default_off_does_not_inject(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
        )
        hitl = self._register_pending_hitl(orchestrator, session)

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        self.assertEqual(transport.submitted_turns, [])
        # The stale sweep still runs regardless of the injection flag.
        self.assertEqual(orchestrator.hitls.get(hitl.hitl_request_id).status, "stale")

    def test_takeover_only_without_pending_hitl_does_not_inject(self):
        # No orphaned prompt → nothing to re-ask → no synthetic turn even
        # with the flag on (an idle takeover must stay silent).
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            handoff_continue="auto",
        )

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        self.assertEqual(transport.submitted_turns, [])

    def test_handoff_continue_uses_stable_idempotency_key(self):
        # Replays/retries must not double-inject: the synthetic turn rides
        # a takeover-scoped idempotency key, not a random one.
        class _KeyCapturingTransport(FakeAgentTransport):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.submit_keys = []

            async def submit_turn(self, handle, turn, idempotency_key):
                self.submit_keys.append(idempotency_key)
                await super().submit_turn(handle, turn, idempotency_key)

        transport = _KeyCapturingTransport("fake-transport", _transport_caps())
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            transport=transport,
            handoff_continue="auto",
        )
        self._register_pending_hitl(orchestrator, session)

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.submit_keys), 1)
        self.assertRegex(transport.submit_keys[0], r"^handoff_continue:")

    def test_handoff_continue_submit_failure_keeps_takeover_success(self):
        # The injection is best-effort: a failing transport must not turn a
        # completed takeover into a failure (the user can still type).
        class _FailingSubmitTransport(FakeAgentTransport):
            async def submit_turn(self, handle, turn, idempotency_key):
                raise RuntimeError("worker rejected the synthetic turn")

        transport = _FailingSubmitTransport("fake-transport", _transport_caps())
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            transport=transport,
            handoff_continue="auto",
        )
        hitl = self._register_pending_hitl(orchestrator, session)

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.writer_owner.kind, "orchestrator")
        self.assertEqual(orchestrator.hitls.get(hitl.hitl_request_id).status, "stale")

    def test_takeover_sweep_clears_awaiting_other_wait(self):
        # An AskUserQuestion parked in the free-text "Other" wait must not
        # keep swallowing plain topic messages after the handoff retired it.
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
        )
        self._register_pending_hitl(orchestrator, session)
        ctx = orchestrator.interactions.register_ask_user_question(
            session_id=session.session_id,
            generation=session.generation,
            questions=[{"question": "Which env?", "options": ["dev", "prod"]}],
        )
        binding_key = session.channel_binding.key()
        orchestrator.interactions.begin_awaiting_other(
            ctx.interaction_id, binding_key, question_index=0
        )
        self.assertIsNotNone(orchestrator.interactions.awaiting_context_for_binding(binding_key))

        result = self._status_card_takeover(orchestrator, channel, session)

        self.assertTrue(result.accepted)
        self.assertIsNone(orchestrator.interactions.awaiting_context_for_binding(binding_key))

    def test_takeover_with_blocked_input_does_not_inject_handoff_continue(self):
        # A takeover carrying user text lets that text drive the
        # continuation — injecting continue as well would double-prompt.
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            },
            handoff_continue="auto",
        )
        self._register_pending_hitl(orchestrator, session)
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token, event_id="cb-confirm"),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_stale_takeover_callback_does_not_change_transaction(self):
        orchestrator, channel, transport, session = _setup(
            resume_ref={
                "transport_kind": "fake-transport",
                "transport_ref": {"handle_id": "resume-h", "session_id": "native-1"},
            }
        )
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run tests"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        token = _takeover_token(channel)
        orchestrator.sessions.get(session.session_id).generation += 1

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(token),
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        tx = next(iter(orchestrator.sessions.to_dict()["takeovers"].values()))
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.STALE_GENERATION)
        self.assertEqual(tx["phase"], TakeoverPhase.PROMPTED)
        self.assertEqual(transport.submitted_turns, [])


if __name__ == "__main__":
    unittest.main()
