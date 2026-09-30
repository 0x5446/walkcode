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
    LarkBotApi,
    LarkChannelAdapter,
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


def _channel_caps(**overrides) -> ChannelCapabilities:
    data = {
        "editable_message": True,
        "private_callback_ack": True,
        "attachment_download": True,
    }
    data.update(overrides)
    return ChannelCapabilities(**data)


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
    def test_lark_callback_is_acknowledged_before_invalid_token_result(self):
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
        event = channel.parse_event(
            {
                "event_id": "evt-cb-1",
                "event": {
                    "open_id": "ou_owner",
                    "chat_id": "oc_chat",
                    "message_id": "om_card",
                    "action": {"value": {"token": "missing-token"}},
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
        self.assertEqual(api.calls[0][0], "ackCallback")
        self.assertEqual(api.calls[0][1]["event_id"], "lark:evt-cb-1")
        self.assertEqual(api.calls[0][1]["token"], "missing-token")

    def test_fake_channel_records_callback_ack_when_capability_enabled(self):
        channel = FakeChannelAdapter("lark", _channel_caps(private_callback_ack=True))
        event = InboundEvent(
            event_id="evt-callback",
            channel_kind="lark",
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
        channel = FakeChannelAdapter("lark", _channel_caps(private_callback_ack=False))
        event = InboundEvent(
            event_id="evt-callback",
            channel_kind="lark",
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
