import asyncio
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundLedger,
    InteractionStore,
    JsonFileStateStore,
    Orchestrator,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


def _binding(root: str = "root", *, chat: str = "chat", thread: str = "topic") -> ChannelBinding:
    return ChannelBinding("telegram", "bot", chat, thread, root)


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


def _orchestrator():
    clock = _Clock()
    authz = AuthorizationStore(now=clock)
    transport = FakeAgentTransport("fake-transport", _transport_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
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
    return orchestrator, transport, session, clock


class SessionListingTests(unittest.TestCase):
    def test_list_sessions_filters_by_channel_chat_and_thread(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        first = sessions.create_structured_session(
            session_id="s1",
            binding=_binding("root-1"),
            transport_kind="fake-transport",
            transport_ref={"handle_id": "h1"},
            cwd="/tmp/project",
            owner=_actor("owner"),
        )
        sessions.create_structured_session(
            session_id="s2",
            binding=_binding("root-2", chat="other-chat"),
            transport_kind="fake-transport",
            transport_ref={"handle_id": "h2"},
            cwd="/tmp/project",
            owner=_actor("owner"),
        )

        summaries = sessions.list_sessions(
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
        )

        self.assertEqual([summary.session_id for summary in summaries], [first.session_id])
        self.assertEqual(summaries[0].root_message_id, "root-1")
        self.assertEqual(summaries[0].status, "running")

    def test_archive_running_session_is_rejected_without_stopping_transport(self):
        orchestrator, transport, session, _clock = _orchestrator()

        result = asyncio.run(
            orchestrator.archive_session(
                session.session_id,
                actor=_actor("owner"),
                reason="done",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.SESSION_RUNNING)
        self.assertEqual(transport.shutdown_calls, [])
        self.assertEqual(orchestrator.sessions.get(session.session_id).archived_at, 0.0)

    def test_owner_or_admin_can_archive_stopped_session_and_default_list_hides_it(self):
        orchestrator, _transport, session, _clock = _orchestrator()
        asyncio.run(orchestrator.close_session(session.session_id, actor=_actor("owner"), reason="done"))

        denied = asyncio.run(
            orchestrator.archive_session(
                session.session_id,
                actor=_actor("reviewer"),
                reason="hide",
            )
        )
        archived = asyncio.run(
            orchestrator.archive_session(
                session.session_id,
                actor=_actor("admin"),
                reason="hide",
            )
        )

        self.assertFalse(denied.accepted)
        self.assertEqual(denied.reason, BlockedReason.UNAUTHORIZED)
        self.assertTrue(archived.accepted)
        self.assertEqual(orchestrator.sessions.list_sessions(chat_id="chat"), [])
        included = orchestrator.sessions.list_sessions(chat_id="chat", include_archived=True)
        self.assertEqual([summary.session_id for summary in included], [session.session_id])
        self.assertEqual(included[0].archived_by, "admin")

    def test_command_menu_offers_archive_for_stopped_session(self):
        orchestrator, _transport, session, _clock = _orchestrator()
        asyncio.run(orchestrator.close_session(session.session_id, actor=_actor("owner"), reason="done"))

        menu = orchestrator.command_menu_for_session(session.session_id, actor=_actor("owner"))

        self.assertEqual([action["action"] for action in menu["actions"]], ["archive"])

    def test_archived_metadata_round_trips_through_json_state(self):
        orchestrator, _transport, session, clock = _orchestrator()
        asyncio.run(orchestrator.close_session(session.session_id, actor=_actor("owner"), reason="done"))
        asyncio.run(orchestrator.archive_session(session.session_id, actor=_actor("owner"), reason="hide"))

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json", now=clock)
            store.save(
                sessions=orchestrator.sessions,
                interactions=orchestrator.interactions,
                outbox=orchestrator.outbox,
                authz=orchestrator.authz,
                inbound_ledger=InboundLedger(now=clock),
            )

            loaded = store.load().sessions

        summaries = loaded.list_sessions(chat_id="chat", include_archived=True)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].archived_at, 1000.0)
        self.assertEqual(summaries[0].archived_by, "owner")


if __name__ == "__main__":
    unittest.main()
