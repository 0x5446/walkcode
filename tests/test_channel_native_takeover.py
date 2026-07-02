import unittest

from walkcode.channel_native import (
    ActorRef,
    ChannelBinding,
    SessionRegistry,
    TakeoverError,
    TakeoverPhase,
    TurnInput,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="u1", display_name="User")


def _binding() -> ChannelBinding:
    return ChannelBinding(
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        root_message_id="root",
    )


class TakeoverTransactionTests(unittest.TestCase):
    def test_takeover_without_resume_ref_becomes_manual_only(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_observed_session(
            session_id="observed-1",
            binding=_binding(),
            cwd="/tmp/project",
            external_ref={"pid": 123},
            owner=_actor(),
        )
        blocked = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="run it"),
            generation=session.generation,
        )

        tx = sessions.request_takeover(
            session.session_id,
            blocked.blocked_input_id,
            requested_by=_actor(),
            generation=session.generation,
        )
        tx = sessions.authorize_takeover(tx.takeover_id, approved_by=_actor(), resume_ref=None)

        self.assertEqual(tx.phase, TakeoverPhase.MANUAL_ONLY)
        self.assertEqual(sessions.get(session.session_id).writer_owner.kind, "external_tui")

    def test_successful_takeover_moves_writer_and_submits_blocked_input(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_observed_session(
            session_id="observed-1",
            binding=_binding(),
            cwd="/tmp/project",
            external_ref={"pid": 123},
            owner=_actor(),
        )
        blocked = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="run it"),
            generation=session.generation,
        )
        tx = sessions.request_takeover(
            session.session_id,
            blocked.blocked_input_id,
            requested_by=_actor(),
            generation=session.generation,
        )
        tx = sessions.authorize_takeover(
            tx.takeover_id,
            approved_by=_actor(),
            resume_ref={"kind": "claude_headless", "session_id": "claude-1"},
        )
        tx = sessions.complete_takeover(
            tx.takeover_id,
            transport_kind="claude_headless",
            transport_ref={"session_id": "claude-1"},
        )
        updated = sessions.get(session.session_id)

        self.assertEqual(tx.phase, TakeoverPhase.COMPLETED)
        self.assertEqual(updated.writer_owner.kind, "orchestrator")
        self.assertEqual(updated.transport_kind, "claude_headless")
        self.assertEqual(updated.generation, 1)
        self.assertEqual(updated.blocked_inputs[blocked.blocked_input_id].state, "submitted")

    def test_stale_takeover_request_is_rejected(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_observed_session(
            session_id="observed-1",
            binding=_binding(),
            cwd="/tmp/project",
            external_ref={"pid": 123},
            owner=_actor(),
        )
        blocked = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="run it"),
            generation=session.generation,
        )

        with self.assertRaises(TakeoverError):
            sessions.request_takeover(
                session.session_id,
                blocked.blocked_input_id,
                requested_by=_actor(),
                generation=session.generation - 1,
            )


class StructuredToExternalHandoffTests(unittest.TestCase):
    def test_external_tui_claim_moves_structured_session_to_readonly(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_structured_session(
            binding=_binding(),
            transport_kind="claude_headless",
            transport_ref={"agent_session_id": "claude-1"},
            cwd="/tmp/project",
            owner=_actor(),
        )

        result = sessions.handoff_to_external_tui(
            session.session_id,
            generation=session.generation,
            owner=_actor(),
            resume_ref={"transport_kind": "claude_headless", "agent_session_id": "claude-1"},
            external_ref={
                "terminate_ref": {
                    "controller_kind": "process",
                    "process_ref": {"pid": 123},
                },
            },
        )
        updated = sessions.get(session.session_id)

        self.assertTrue(result.accepted)
        self.assertEqual(updated.generation, 1)
        self.assertEqual(updated.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
        self.assertEqual(updated.writer_owner.kind, "external_tui")
        self.assertIsNone(updated.writer_lease)
        self.assertEqual(updated.transport_kind, "external_tui")
        self.assertEqual(updated.transport_ref["resume_ref"]["agent_session_id"], "claude-1")
        self.assertEqual(updated.transport_ref["terminate_ref"]["process_ref"]["pid"], 123)

    def test_im_input_after_external_tui_claim_is_blocked(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_structured_session(
            binding=_binding(),
            transport_kind="claude_headless",
            transport_ref={"agent_session_id": "claude-1"},
            cwd="/tmp/project",
            owner=_actor(),
        )
        sessions.handoff_to_external_tui(
            session.session_id,
            generation=session.generation,
            owner=_actor(),
            resume_ref={"transport_kind": "claude_headless", "agent_session_id": "claude-1"},
            external_ref={},
        )

        result = sessions.block_input(
            session.session_id,
            actor=_actor(),
            turn=TurnInput(text="from IM"),
            generation=sessions.get(session.session_id).generation,
        )

        self.assertEqual(result.reason, "external_tui_readonly")
        self.assertTrue(result.blocked_input_id)
        self.assertEqual(
            sessions.get(session.session_id).blocked_inputs[result.blocked_input_id].text,
            "from IM",
        )

    def test_stale_external_tui_claim_is_rejected(self):
        sessions = SessionRegistry(now=_Clock())
        session = sessions.create_structured_session(
            binding=_binding(),
            transport_kind="claude_headless",
            transport_ref={"agent_session_id": "claude-1"},
            cwd="/tmp/project",
            owner=_actor(),
        )

        result = sessions.handoff_to_external_tui(
            session.session_id,
            generation=session.generation + 1,
            owner=_actor(),
            resume_ref={"transport_kind": "claude_headless", "agent_session_id": "claude-1"},
            external_ref={},
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "stale_generation")
        self.assertEqual(sessions.get(session.session_id).writer_owner.kind, "orchestrator")

