"""ADR 0054: a channel message revives an involuntarily-stopped structured session.

Every runtime restart sweeps headless sessions to "stopped"; a later Feishu
message used to dead-end at 会话已结束 even though the transcript and resume
credentials were intact (live incident 2026-07-19 17:58, after the v0.14.3
upgrade). Revival is takeover minus the kill: bump generation, reset to IDLE,
let the normal resume-for-submit machinery spawn a fresh worker and deliver
the message.

Scope contract under test:
- only involuntary stops revive (runtime_restart, revive_failed retry);
- an explicit close keeps blocking submits (existing contract);
- external-TUI takeover candidates keep the consent-based takeover prompt;
- a failed revival reverts to stopped (no phantom-running record) and the
  next message retries.
"""

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
    FakeExternalTuiController,
    InteractionStore,
    Orchestrator,
    ResumeSpec,
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
        "external_tui_takeover": True,
    }
    data.update(overrides)
    return TransportCapabilities(**data)


def _headless_orchestrator(transport=None):
    clock = _Clock()
    authz = AuthorizationStore(now=clock)
    transport = transport or FakeAgentTransport("claude_headless", _transport_caps())
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports={"claude_headless": transport},
        authz=authz,
        now=clock,
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor("owner"))
    )
    return orchestrator, transport, channel, session


def _sweep_to_stopped(session, *, stop_reason: str = "runtime_restart"):
    """Model what _settle_orphan_headless_sessions_once leaves behind."""
    session.status = "stopped"
    session.stop_reason = stop_reason
    session.lifecycle_state = "STOPPED"
    session.writer_owner = None
    session.writer_lease = None
    session.transport_ref = {"handle_id": "h-old", "agent_session_id": "agent-sess-1"}


class ChannelRevivalTests(unittest.TestCase):
    def test_message_revives_swept_headless_session_and_submits(self):
        orchestrator, transport, _channel, session = _headless_orchestrator()
        _sweep_to_stopped(session)
        old_generation = session.generation

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="continue please"),
                actor=_actor("owner"),
                generation=old_generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("resume", transport.call_log)
        self.assertIn("submit_turn", transport.call_log)
        self.assertEqual(
            [spec.resume_ref.get("agent_session_id") for spec in transport.resume_specs],
            ["agent-sess-1"],
        )
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["continue please"])
        updated = orchestrator.sessions.get(session.session_id)
        self.assertEqual(updated.status, "running")
        self.assertEqual(updated.stop_reason, "")
        self.assertGreater(updated.generation, old_generation)
        self.assertEqual(updated.writer_owner.kind, "orchestrator")

    def test_explicit_close_still_blocks_submits(self):
        orchestrator, transport, _channel, session = _headless_orchestrator()
        asyncio.run(
            orchestrator.close_session(session.session_id, actor=_actor("owner"), reason="finished")
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="should stay closed"),
                actor=_actor("owner"),
                generation=orchestrator.sessions.get(session.session_id).generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.SESSION_STOPPED)
        self.assertNotIn("resume", transport.call_log)

    def test_swept_session_without_resume_ref_stays_dead(self):
        orchestrator, transport, _channel, session = _headless_orchestrator()
        _sweep_to_stopped(session)
        session.transport_ref = {"handle_id": "h-old"}  # no agent_session_id

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="hello?"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.SESSION_STOPPED)
        self.assertNotIn("resume", transport.call_log)

    def test_failed_revival_reverts_to_stopped_and_next_message_retries(self):
        class FailingResumeTransport(FakeAgentTransport):
            async def resume(self, spec: ResumeSpec):
                self.resume_specs.append(spec)
                self.call_log.append("resume")
                raise RuntimeError("resume failed")

        transport = FailingResumeTransport("claude_headless", _transport_caps())
        orchestrator, _transport, _channel, session = _headless_orchestrator(transport)
        _sweep_to_stopped(session)

        first = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="try one"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )
        self.assertFalse(first.accepted)
        updated = orchestrator.sessions.get(session.session_id)
        # No phantom-running record: reverted to stopped with a retryable reason.
        self.assertEqual(updated.status, "stopped")
        self.assertEqual(updated.stop_reason, "revive_failed")

        second = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="try two"),
                actor=_actor("owner"),
                generation=updated.generation,
            )
        )
        self.assertFalse(second.accepted)
        # revive_failed is in the allowlist: the second message attempted a
        # fresh resume instead of dead-ending.
        self.assertEqual(transport.call_log.count("resume"), 2)

    def test_stopped_external_tui_candidate_gets_takeover_prompt_not_revival(self):
        clock = _Clock()
        authz = AuthorizationStore(now=clock)
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        channel = FakeChannelAdapter("telegram", _channel_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            external_tui_controllers={"fake-process": FakeExternalTuiController("fake-process")},
            authz=authz,
            now=clock,
        )
        session = orchestrator.sessions.create_observed_session(
            session_id="observed-1",
            binding=_binding(),
            cwd="/tmp/project",
            external_ref={
                "resume_ref": {"transport_kind": "fake-transport", "session_id": "native-1"},
                "terminate_ref": {"controller_kind": "fake-process", "process_ref": {"pid": 123}},
            },
            owner=_actor("owner"),
        )
        authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
        session.status = "stopped"
        session.stop_reason = "runtime_restart"
        session.lifecycle_state = "STOPPED"

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="take it over"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        # Blocked into the consent-based takeover prompt, not silently revived.
        self.assertFalse(result.accepted)
        self.assertTrue(result.blocked_input_id)
        self.assertNotIn("resume", transport.call_log)
        prompt_views = [v["view"] for v in channel.sent_views if v["view"].get("type") == "takeover_prompt"]
        self.assertTrue(prompt_views)


if __name__ == "__main__":
    unittest.main()
