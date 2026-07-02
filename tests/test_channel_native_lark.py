import asyncio
import unittest

from walkcode.channel_native import (
    AgentEvent,
    AgentEventType,
    DurableOutbox,
    FakeAgentTransport,
    InteractionStore,
    LarkBotApi,
    LarkChannelAdapter,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeLarkApi(LarkBotApi):
    def __init__(self):
        self.calls = []
        super().__init__(caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "data": {"message_id": f"lark-msg-{len(self.calls)}"}}


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


class LarkAdapterTests(unittest.TestCase):
    def test_parse_thread_text_message(self):
        adapter = LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))
        event = adapter.parse_event(
            {
                "event_id": "evt-1",
                "event": {
                    "message": {
                        "message_id": "om_msg",
                        "root_id": "om_root",
                        "chat_id": "oc_chat",
                        "content": "{\"text\":\"hello lark\"}",
                    },
                    "sender": {
                        "sender_id": {"open_id": "ou_user"},
                        "sender_type": "user",
                    },
                },
            }
        )

        self.assertEqual(event.event_id, "lark:evt-1")
        self.assertEqual(event.channel_kind, "lark")
        self.assertEqual(event.chat_id, "oc_chat")
        self.assertEqual(event.thread_id, "om_root")
        self.assertEqual(event.root_message_id, "om_root")
        self.assertEqual(event.message_id, "om_msg")
        self.assertEqual(event.sender_id, "ou_user")
        self.assertEqual(event.text, "hello lark")

    def test_parse_card_callback_short_token(self):
        adapter = LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))
        event = adapter.parse_event(
            {
                "event_id": "evt-2",
                "event": {
                    "message_id": "om_card",
                    "chat_id": "oc_chat",
                    "open_id": "ou_user",
                    "action": {"value": {"token": "short-token", "action": "allow"}},
                },
            }
        )

        self.assertEqual(event.callback["token"], "short-token")
        self.assertEqual(event.callback["action"], "allow")
        self.assertEqual(event.message_id, "om_card")

    def test_send_interaction_view_uses_card_call(self):
        api = _FakeLarkApi()
        adapter = LarkChannelAdapter(api)

        message_id = asyncio.run(
            adapter.send_view(
                binding=adapter.binding_for("oc_chat", "om_root"),
                view_model={"type": "permission_prompt", "text": "Approve?"},
            )
        )

        self.assertEqual(message_id, "lark-msg-1")
        self.assertEqual(api.calls[0][0], "sendCard")
        self.assertEqual(api.calls[0][1]["chat_id"], "oc_chat")
        self.assertEqual(api.calls[0][1]["root_id"], "om_root")


class LarkOrchestratorTests(unittest.TestCase):
    def test_thread_text_creates_session_and_submits_to_agent_transport(self):
        clock = _Clock()
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"lark": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        event = channel.parse_event(
            {
                "event_id": "evt-1",
                "event": {
                    "message": {
                        "message_id": "om_msg",
                        "root_id": "om_root",
                        "chat_id": "oc_chat",
                        "content": "{\"text\":\"run tests\"}",
                    },
                    "sender": {"sender_id": {"open_id": "ou_user"}},
                },
            }
        )

        result = asyncio.run(
            orchestrator.handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        self.assertIn("done", channel.rendered_text())
