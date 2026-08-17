import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    Session,
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
    render_view_text,
)
from walkcode.channel_native import _agent_session_identity


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="owner", display_name="Owner")


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
        external_tui_takeover=False,
    )


def _orchestrator(clock: _Clock, transport: FakeAgentTransport):
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports={"fake-transport": transport},
        now=clock,
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
    )
    return orchestrator, channel, session


class HealthWatchdogTests(unittest.TestCase):
    def test_progress_events_update_health_metadata_and_idle_state(self):
        clock = _Clock()
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TURN_DELTA, {"text": "working"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"}),
            ],
        )
        orchestrator, _channel, session = _orchestrator(clock, transport)
        clock.now = 1005.0

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertTrue(result.accepted)
        self.assertEqual(updated.last_event_seq, 2)
        self.assertEqual(updated.last_progress_event, AgentEventType.TURN_COMPLETED)
        self.assertEqual(updated.last_progress_at, 1005.0)
        self.assertEqual(updated.lifecycle_state, "IDLE")

    def test_permission_event_sets_waiting_permission_health(self):
        clock = _Clock()
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(
                    AgentEventType.PERMISSION_REQUESTED,
                    {
                        "rid": "perm-1",
                        "tool_name": "Bash",
                        "tool_input": {"cmd": "pwd"},
                        "actions": ["allow_once", "deny"],
                    },
                )
            ],
        )
        orchestrator, _channel, session = _orchestrator(clock, transport)
        clock.now = 1007.0

        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.last_progress_event, AgentEventType.PERMISSION_REQUESTED)
        self.assertEqual(updated.lifecycle_state, "WAITING_PERMISSION")

    def test_watchdog_marks_stale_without_calling_transport_controls(self):
        clock = _Clock()
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator, _channel, session = _orchestrator(clock, transport)
        clock.now = 1040.0

        health = orchestrator.check_session_health(session.session_id, progress_timeout=30.0)

        self.assertTrue(health.stale)
        self.assertEqual(health.status, "stale")
        self.assertEqual(health.reason, "progress_timeout")
        self.assertEqual(health.view_model["type"], "health")
        self.assertEqual(health.view_model["status"], "stale")
        self.assertEqual(transport.interrupt_calls, [])
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(orchestrator.sessions.get(session.session_id).status, "running")

    def test_progress_metadata_survives_registry_round_trip(self):
        clock = _Clock()
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
        )
        orchestrator, _channel, session = _orchestrator(clock, transport)
        clock.now = 1010.0
        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        restored = SessionRegistry.from_dict(orchestrator.sessions.to_dict(), now=clock)
        restored_session = restored.get(session.session_id)

        self.assertEqual(restored_session.last_progress_at, 1010.0)
        self.assertEqual(restored_session.last_progress_event, AgentEventType.TURN_COMPLETED)

    def test_model_and_usage_flow_into_session_and_health_view(self):
        clock = _Clock()
        usage = {
            "input_tokens": 1_200,
            "cache_read_input_tokens": 80_000,
            "cache_creation_input_tokens": 2_800,
            "output_tokens": 6_000,
        }
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_DELTA,
                    {"text": "working", "model": "claude-opus-4-8-20260610"},
                ),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done", "usage": usage}),
            ],
        )
        orchestrator, _channel, session = _orchestrator(clock, transport)

        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.model, "claude-opus-4-8-20260610")
        self.assertEqual(updated.last_usage, usage)

        view = orchestrator.check_session_health(
            session.session_id, progress_timeout=0
        ).view_model
        self.assertEqual(view["model"], "claude-opus-4-8-20260610")
        self.assertEqual(view["context_used"], 90_000)
        self.assertEqual(view["context_limit"], 200_000)

        # model / last_usage must survive a state save+load round trip.
        restored = SessionRegistry.from_dict(orchestrator.sessions.to_dict(), now=clock)
        self.assertEqual(restored.get(session.session_id).model, "claude-opus-4-8-20260610")
        self.assertEqual(restored.get(session.session_id).last_usage, usage)

    def test_flip_decided_card_retries_transient_edit_failures(self):
        from walkcode.channel_native import InboundEvent, TransientDeliveryError

        clock = _Clock()
        transport = FakeAgentTransport("fake-transport", _transport_caps(), scripted_events=[])
        orchestrator, channel, _session = _orchestrator(clock, transport)

        attempts = []
        original_edit = channel.edit_view

        async def flaky_edit(binding, message_id, view):
            attempts.append(1)
            if len(attempts) < 3:
                raise TransientDeliveryError("Lark reply failed: 2200 Internal Error")
            return await original_edit(binding, message_id, view)

        channel.edit_view = flaky_edit
        inbound = InboundEvent(
            event_id="evt-flip",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-perm",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="",
            callback={"token": "t"},
        )

        asyncio.run(
            orchestrator._flip_decided_card(
                inbound, kind="permission", tool_name="WebFetch", action="allow_once"
            )
        )

        # Two transient 2200s then success: the settled card must not keep
        # live buttons just because the first patch attempt failed.
        self.assertEqual(len(attempts), 3)

    def test_context_limit_upgrades_when_usage_exceeds_default_window(self):
        # A [1m] session's marker is lost once the dated live id overwrites
        # session.model; the display limit must not read as >100%.
        clock = _Clock()
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {
                        "message": "done",
                        "model": "claude-opus-4-6-20260610",
                        "usage": {"input_tokens": 5_000, "cache_read_input_tokens": 400_000},
                    },
                ),
            ],
        )
        orchestrator, _channel, session = _orchestrator(clock, transport)

        asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        view = orchestrator.check_session_health(
            session.session_id, progress_timeout=0
        ).view_model
        self.assertEqual(view["context_used"], 405_000)
        self.assertEqual(view["context_limit"], 1_000_000)


class AgentSessionIdentityTests(unittest.TestCase):
    """/status must show the id the AGENT answers to, not WalkCode's key.

    ``sess-<uuid4 hex>`` is a ledger key; pasting it into ``codex resume``
    returns "no such session", which is exactly how this surfaced.
    """

    @staticmethod
    def _session(transport_kind: str, transport_ref: dict) -> Session:
        return Session(
            schema_version=1,
            session_id="sess-abc",
            transport_kind=transport_kind,
            transport_ref=transport_ref,
            cwd="/tmp/project",
        )

    def test_codex_identity_is_the_thread_id(self):
        session = self._session(
            "codex_app_server",
            {"handle_id": "codex-1", "thread_id": "01a00de8-62bc-73e3"},
        )

        self.assertEqual(_agent_session_identity(session), "01a00de8-62bc-73e3")

    def test_claude_identity_is_the_agent_session_id(self):
        session = self._session(
            "claude_headless",
            {
                "handle_id": "claude-1",
                "agent_session_id": "c5b03e87-9ca0-48af",
                "session_id": "sess-abc",
            },
        )

        self.assertEqual(_agent_session_identity(session), "c5b03e87-9ca0-48af")

    def test_walkcode_key_parked_in_the_generic_slot_is_not_an_agent_id(self):
        session = self._session("claude_headless", {"session_id": "sess-abc"})

        self.assertEqual(_agent_session_identity(session), "")

    def test_legacy_claude_record_falls_back_to_the_generic_slot(self):
        session = self._session("claude_headless", {"session_id": "7a440930-364f"})

        self.assertEqual(_agent_session_identity(session), "7a440930-364f")

    def test_tui_observed_session_reads_the_nested_resume_ref(self):
        session = self._session(
            "external_tui",
            {
                "agent": "codex",
                "resume_ref": {
                    "thread_id": "019ff42d-b4be",
                    "transport_kind": "codex_app_server",
                },
            },
        )

        self.assertEqual(_agent_session_identity(session), "019ff42d-b4be")

    def test_health_view_carries_both_ids(self):
        clock = _Clock()
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator, _channel, session = _orchestrator(clock, transport)
        session.transport_kind = "codex_app_server"
        session.transport_ref = {"thread_id": "01a00de8"}
        orchestrator.transports["codex_app_server"] = transport

        view = orchestrator.check_session_health(
            session.session_id, progress_timeout=0
        ).view_model

        self.assertEqual(view["agent_session_id"], "01a00de8")
        self.assertEqual(view["session_id"], session.session_id)
        text = render_view_text(view)
        self.assertIn("Session: 01a00de8", text)
        self.assertIn(f"WalkCode: {session.session_id}", text)


if __name__ == "__main__":
    unittest.main()
