import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    BlockedReason,
    ChannelBinding,
    DurableOutbox,
    FakeAgentTransport,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TelegramBotApi,
    TelegramChannelAdapter,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name="Owner")


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


def _binding(root: str, *, chat: str = "100", thread: str = "") -> ChannelBinding:
    return ChannelBinding(
        channel_kind="telegram",
        account_id="bot",
        chat_id=chat,
        thread_id=thread,
        root_message_id=root,
    )


class ChannelRoutingTests(unittest.TestCase):
    def test_telegram_private_followup_continues_single_active_session(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        first = channel.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "first",
                },
            }
        )
        second = channel.parse_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "second",
                },
            }
        )

        self.assertTrue(
            asyncio.run(
                orchestrator.handle_inbound_event(first, agent_transport_kind="fake-transport", cwd="/tmp/p")
            ).accepted
        )
        self.assertTrue(
            asyncio.run(
                orchestrator.handle_inbound_event(second, agent_transport_kind="fake-transport", cwd="/tmp/p")
            ).accepted
        )

        self.assertEqual(len(transport.handles), 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["first", "second"])

    def test_telegram_forum_topic_followup_continues_single_active_topic_session(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        first = channel.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "message_thread_id": 77,
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "topic first",
                },
            }
        )
        second = channel.parse_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 11,
                    "message_thread_id": 77,
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "topic second",
                },
            }
        )

        asyncio.run(orchestrator.handle_inbound_event(first, agent_transport_kind="fake-transport", cwd="/tmp/p"))
        result = asyncio.run(
            orchestrator.handle_inbound_event(second, agent_transport_kind="fake-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.handles), 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["topic first", "topic second"])

    def test_telegram_forum_topic_reply_to_non_root_still_routes_by_topic(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=_Clock(),
        )

        first = channel.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "message_thread_id": 77,
                    "chat": {"id": -100, "type": "supergroup"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "topic first",
                },
            }
        )
        reply_to_later_message = channel.parse_update(
            {
                "update_id": 2,
                "message": {
                    "message_id": 12,
                    "message_thread_id": 77,
                    "chat": {"id": -100, "type": "supergroup"},
                    "reply_to_message": {"message_id": 11},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "reply inside same topic",
                },
            }
        )

        asyncio.run(orchestrator.handle_inbound_event(first, agent_transport_kind="fake-transport", cwd="/tmp/p"))
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                reply_to_later_message,
                agent_transport_kind="fake-transport",
                cwd="/tmp/p",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.handles), 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["topic first", "reply inside same topic"])

    def test_reply_to_root_keeps_exact_binding_priority(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        first_transport = FakeAgentTransport("first-transport", _transport_caps())
        second_transport = FakeAgentTransport("second-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"first-transport": first_transport, "second-transport": second_transport},
            now=_Clock(),
        )
        asyncio.run(orchestrator.start_session(_binding("10"), "first-transport", "/tmp/p", _actor()))
        asyncio.run(orchestrator.start_session(_binding("20"), "second-transport", "/tmp/p", _actor()))
        reply = channel.parse_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 30,
                    "chat": {"id": 100, "type": "supergroup"},
                    "reply_to_message": {"message_id": 10},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "reply to first",
                },
            }
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(reply, agent_transport_kind="first-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in first_transport.submitted_turns], ["reply to first"])
        self.assertEqual(second_transport.submitted_turns, [])

    def test_rootless_stopped_session_does_not_capture_new_general_message(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        old_transport = FakeAgentTransport("old-transport", _transport_caps())
        new_transport = FakeAgentTransport("new-transport", _transport_caps())
        sessions = SessionRegistry(now=_Clock())
        orchestrator = Orchestrator(
            sessions=sessions,
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"old-transport": old_transport, "new-transport": new_transport},
            now=_Clock(),
        )
        old = sessions.create_structured_session(
            binding=ChannelBinding(
                channel_kind="telegram",
                account_id="bot",
                chat_id="100",
                thread_id="",
                root_message_id="",
            ),
            transport_kind="old-transport",
            transport_ref={"handle_id": "old"},
            cwd="/tmp/p",
            owner=_actor(),
        )
        old.status = "stopped"
        old.lifecycle_state = "STOPPED"
        old.writer_owner = None
        old.writer_lease = None
        general_message = channel.parse_update(
            {
                "update_id": 3,
                "message": {
                    "message_id": 30,
                    "chat": {"id": 100, "type": "supergroup"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "new task",
                },
            }
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(general_message, agent_transport_kind="new-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(old_transport.submitted_turns, [])
        self.assertEqual([turn.text for turn in new_transport.submitted_turns], ["new task"])

    def test_rootless_message_with_multiple_active_candidates_renders_session_chooser(self):
        channel = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        first_transport = FakeAgentTransport("first-transport", _transport_caps())
        second_transport = FakeAgentTransport("second-transport", _transport_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"telegram": channel},
            transports={"first-transport": first_transport, "second-transport": second_transport},
            now=_Clock(),
        )
        asyncio.run(orchestrator.start_session(_binding("10"), "first-transport", "/tmp/p", _actor()))
        asyncio.run(orchestrator.start_session(_binding("20"), "second-transport", "/tmp/p", _actor()))
        rootless = channel.parse_update(
            {
                "update_id": 4,
                "message": {
                    "message_id": 40,
                    "chat": {"id": 100, "type": "supergroup"},
                    "from": {"id": "owner", "first_name": "Ada"},
                    "text": "where should this go",
                },
            }
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(rootless, agent_transport_kind="first-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, BlockedReason.AMBIGUOUS_SESSION)
        self.assertEqual(first_transport.submitted_turns, [])
        self.assertEqual(second_transport.submitted_turns, [])
        self.assertIn("Multiple active sessions match this chat.", channel.rendered_text())
        self.assertIn("first-transport", channel.rendered_text())
        self.assertIn("second-transport", channel.rendered_text())


if __name__ == "__main__":
    unittest.main()
