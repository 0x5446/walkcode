import asyncio
import unittest

from walkcode.channel_native import (
    BlockedReason,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
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


class _FakeTelegramApi(TelegramBotApi):
    def __init__(self):
        self.calls = []
        super().__init__(token="fake", caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, payload))
        return {"ok": True, "result": {"message_id": len(self.calls)}}


def _channel_caps(**overrides) -> ChannelCapabilities:
    data = {
        "thread_context": True,
        "editable_message": True,
        "interactive_message": True,
        "interactive_update": True,
        "private_callback_ack": True,
        "toast_or_ephemeral_notice": True,
        "force_reply": True,
        "attachment_download": True,
        "forum_or_topic": True,
        "max_text_chars": 4096,
        "max_callback_payload_bytes": 64,
    }
    data.update(overrides)
    return ChannelCapabilities(**data)


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


def _orchestrator(channel) -> Orchestrator:
    clock = _Clock()
    return Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={channel.kind: channel},
        transports={"fake-transport": FakeAgentTransport("fake-transport", _transport_caps())},
        now=clock,
    )


class CallbackAckTests(unittest.TestCase):
    def test_telegram_callback_is_acknowledged_before_invalid_token_result(self):
        api = _FakeTelegramApi()
        channel = TelegramChannelAdapter(api)
        event = channel.parse_update(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": "owner", "first_name": "Ada"},
                    "data": "cb:missing-token",
                    "message": {
                        "message_id": 10,
                        "chat": {"id": 100, "type": "private"},
                    },
                },
            }
        )

        result = asyncio.run(
            _orchestrator(channel).handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.INVALID_TOKEN)
        self.assertEqual(api.calls[0][0], "answerCallbackQuery")
        self.assertEqual(api.calls[0][1]["callback_query_id"], "cb-1")

    def test_fake_channel_records_callback_ack_when_capability_enabled(self):
        channel = FakeChannelAdapter("telegram", _channel_caps(private_callback_ack=True))
        event = InboundEvent(
            event_id="evt-callback",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="",
            message_id="msg",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="cb:missing-token",
            callback={"token": "missing-token", "callback_query_id": "cb-1"},
        )

        result = asyncio.run(
            _orchestrator(channel).handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(channel.acknowledged_callbacks, ["evt-callback"])

    def test_callback_ack_capability_disabled_does_not_block_decision(self):
        channel = FakeChannelAdapter("telegram", _channel_caps(private_callback_ack=False))
        event = InboundEvent(
            event_id="evt-callback",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="",
            message_id="msg",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text="cb:missing-token",
            callback={"token": "missing-token", "callback_query_id": "cb-1"},
        )

        result = asyncio.run(
            _orchestrator(channel).handle_inbound_event(
                event,
                agent_transport_kind="fake-transport",
                cwd="/tmp/project",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.INVALID_TOKEN)
        self.assertEqual(channel.acknowledged_callbacks, [])


if __name__ == "__main__":
    unittest.main()
