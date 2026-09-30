import asyncio
import json
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
    FakeChannelAdapter,
    InboundEvent,
    LarkBotApi,
    LarkChannelAdapter,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="lark", actor_id=actor_id, display_name="Owner")


def _transport_caps() -> TransportCapabilities:
    return TransportCapabilities(
        structured_input=True,
        structured_output=True,
        permission_callback=True,
        ask_user_question=True,
        set_model=True,
        resume_after_complete=True,
        external_tui_takeover=False,
    )


def _binding(root: str, *, chat: str = "oc_chat") -> ChannelBinding:
    # A Lark thread is identified by its root message: thread_id == root.
    return ChannelBinding(
        channel_kind="lark",
        account_id="bot",
        chat_id=chat,
        thread_id=root,
        root_message_id=root,
    )


def _lark_channel() -> LarkChannelAdapter:
    return LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))


def _lark_message(message_id: str, text: str, *, root_id: str = "", parent_id: str = "") -> dict:
    message = {
        "message_id": message_id,
        "chat_id": "oc_chat",
        "message_type": "text",
        "content": json.dumps({"text": text}),
    }
    if root_id:
        message["root_id"] = root_id
    if parent_id:
        message["parent_id"] = parent_id
    return {
        "event_id": f"evt-{message_id}",
        "event": {"message": message, "sender": {"sender_id": {"open_id": "ou_owner"}}},
    }


def _rootless_inbound(message_id: str, text: str) -> InboundEvent:
    # Rootless (thread="") bindings still exist on Lark: a TUI-observed
    # session starts rootless when its root card cannot be sent.
    return InboundEvent(
        event_id=f"lark:evt-{message_id}",
        channel_kind="lark",
        account_id="bot",
        chat_id="oc_chat",
        thread_id="",
        message_id=message_id,
        root_message_id="",
        sender_id="owner",
        sender_display="Ada",
        text=text,
    )


def _orchestrator(channel, transports, sessions=None) -> Orchestrator:
    return Orchestrator(
        sessions=sessions or SessionRegistry(now=_Clock()),
        interactions=InteractionStore(now=_Clock()),
        outbox=DurableOutbox(now=_Clock()),
        channels={"lark": channel},
        transports=transports,
        now=_Clock(),
    )


class ChannelRoutingTests(unittest.TestCase):
    def test_rootless_followup_continues_single_active_rootless_session(self):
        # Core rule, independent of how a channel places sessions: a rootless
        # message continues the single active rootless session in the chat.
        channel = FakeChannelAdapter("lark", _lark_channel().capabilities())
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = _orchestrator(channel, {"fake-transport": transport})

        self.assertTrue(
            asyncio.run(
                orchestrator.handle_inbound_event(
                    _rootless_inbound("om_1", "first"), agent_transport_kind="fake-transport", cwd="/tmp/p"
                )
            ).accepted
        )
        self.assertTrue(
            asyncio.run(
                orchestrator.handle_inbound_event(
                    _rootless_inbound("om_2", "second"), agent_transport_kind="fake-transport", cwd="/tmp/p"
                )
            ).accepted
        )

        self.assertEqual(len(transport.handles), 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["first", "second"])

    def test_lark_thread_followup_continues_single_active_thread_session(self):
        channel = _lark_channel()
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = _orchestrator(channel, {"fake-transport": transport})

        first = channel.parse_event(_lark_message("om_1", "topic first"))
        second = channel.parse_event(_lark_message("om_2", "topic second", root_id="om_1", parent_id="om_1"))

        asyncio.run(orchestrator.handle_inbound_event(first, agent_transport_kind="fake-transport", cwd="/tmp/p"))
        result = asyncio.run(
            orchestrator.handle_inbound_event(second, agent_transport_kind="fake-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(transport.handles), 1)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["topic first", "topic second"])

    def test_lark_reply_to_non_root_still_routes_by_thread(self):
        channel = _lark_channel()
        transport = FakeAgentTransport("fake-transport", _transport_caps())
        orchestrator = _orchestrator(channel, {"fake-transport": transport})

        first = channel.parse_event(_lark_message("om_1", "topic first"))
        reply_to_later_message = channel.parse_event(
            _lark_message("om_3", "reply inside same topic", root_id="om_1", parent_id="om_2")
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
        channel = _lark_channel()
        first_transport = FakeAgentTransport("first-transport", _transport_caps())
        second_transport = FakeAgentTransport("second-transport", _transport_caps())
        orchestrator = _orchestrator(
            channel, {"first-transport": first_transport, "second-transport": second_transport}
        )
        asyncio.run(orchestrator.start_session(_binding("om_10"), "first-transport", "/tmp/p", _actor()))
        asyncio.run(orchestrator.start_session(_binding("om_20"), "second-transport", "/tmp/p", _actor()))
        reply = channel.parse_event(_lark_message("om_30", "reply to first", root_id="om_10"))

        result = asyncio.run(
            orchestrator.handle_inbound_event(reply, agent_transport_kind="first-transport", cwd="/tmp/p")
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in first_transport.submitted_turns], ["reply to first"])
        self.assertEqual(second_transport.submitted_turns, [])

    def test_rootless_stopped_session_does_not_capture_new_general_message(self):
        channel = FakeChannelAdapter("lark", _lark_channel().capabilities())
        old_transport = FakeAgentTransport("old-transport", _transport_caps())
        new_transport = FakeAgentTransport("new-transport", _transport_caps())
        sessions = SessionRegistry(now=_Clock())
        orchestrator = _orchestrator(
            channel, {"old-transport": old_transport, "new-transport": new_transport}, sessions
        )
        old = sessions.create_structured_session(
            binding=ChannelBinding(
                channel_kind="lark",
                account_id="bot",
                chat_id="oc_chat",
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

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _rootless_inbound("om_30", "new task"), agent_transport_kind="new-transport", cwd="/tmp/p"
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(old_transport.submitted_turns, [])
        self.assertEqual([turn.text for turn in new_transport.submitted_turns], ["new task"])

    def test_rootless_message_with_multiple_active_candidates_renders_session_chooser(self):
        channel = FakeChannelAdapter("lark", _lark_channel().capabilities())
        first_transport = FakeAgentTransport("first-transport", _transport_caps())
        second_transport = FakeAgentTransport("second-transport", _transport_caps())
        orchestrator = _orchestrator(
            channel, {"first-transport": first_transport, "second-transport": second_transport}
        )
        # Two rootless-thread sessions in the same chat (distinct roots, no
        # thread), so a rootless message cannot pick one.
        for root, kind in (("om_10", "first-transport"), ("om_20", "second-transport")):
            asyncio.run(
                orchestrator.start_session(
                    ChannelBinding("lark", "bot", "oc_chat", "", root), kind, "/tmp/p", _actor()
                )
            )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _rootless_inbound("om_40", "where should this go"),
                agent_transport_kind="first-transport",
                cwd="/tmp/p",
            )
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
