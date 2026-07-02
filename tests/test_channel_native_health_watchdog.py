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
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
)


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


if __name__ == "__main__":
    unittest.main()
