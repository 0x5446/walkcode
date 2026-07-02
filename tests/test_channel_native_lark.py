import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    AgentEvent,
    AgentEventType,
    BlockedReason,
    ChannelNativeConfig,
    DurableOutbox,
    FakeAgentTransport,
    InteractionStore,
    LarkBotApi,
    LarkChannelAdapter,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime


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


class LarkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, api=None, env_extra=None, scripted_events=None):
        env = {
            "WALKCODE_CHANNEL": "lark",
            "LARK_APP_ID": "app-id",
            "LARK_APP_SECRET": "secret",
            "WALKCODE_AGENT": "claude",
            "WALKCODE_PROFILE": "work",
            "WALKCODE_STATE_PATH": str(Path(self._tmp.name) / "state.json"),
            "WALKCODE_CWD": self._tmp.name,
        }
        env.update(env_extra or {})
        api = api or _FakeLarkApi()
        transport = FakeAgentTransport(
            "claude_headless",
            _transport_caps(),
            scripted_events=scripted_events
            or [AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
        )
        runtime = ChannelNativeRuntime.from_config(
            ChannelNativeConfig.from_env(env),
            lark_api=api,
            transports={"claude_headless": transport},
        )
        return runtime, api, transport

    @staticmethod
    def _message_payload(event_id="evt-1", chat_id="oc_chat", text="run tests", root_id="", sender="ou_user", message_id="om_msg"):
        return {
            "event_id": event_id,
            "event": {
                "message": {
                    "message_id": message_id,
                    "root_id": root_id,
                    "chat_id": chat_id,
                    "content": json.dumps({"text": text}),
                },
                "sender": {"sender_id": {"open_id": sender}},
            },
        }

    def test_plain_message_creates_session_and_submits(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(runtime.process_lark_event(self._message_payload()))

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        sessions = runtime.state.sessions.list_sessions(channel_kind="lark")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root_message_id, "om_msg")

    def test_chat_allowlist_blocks_unknown_chat(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_CHAT_IDS": "oc_allowed"}
        )

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(chat_id="oc_other"))
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.UNAUTHORIZED)
        self.assertEqual(transport.submitted_turns, [])

    def test_sender_allowlist_blocks_unknown_open_id(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_OPEN_IDS": "ou_owner"}
        )

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(sender="ou_stranger"))
        )

        self.assertFalse(result.accepted)
        self.assertEqual(transport.submitted_turns, [])

    def test_e2e_lark_chat_id_restricts_runtime_by_default(self):
        runtime, api, transport = self._runtime(
            env_extra={"WALKCODE_E2E_LARK_CHAT_ID": "oc_e2e"}
        )

        blocked = asyncio.run(runtime.process_lark_event(self._message_payload(chat_id="oc_other")))
        allowed = asyncio.run(
            runtime.process_lark_event(self._message_payload(event_id="evt-2", chat_id="oc_e2e"))
        )

        self.assertFalse(blocked.accepted)
        self.assertTrue(allowed.accepted)

    def test_status_command_outside_session_reports_runtime_status(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/status"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.submitted_turns, [])
        self.assertEqual(runtime.state.sessions.list_sessions(channel_kind="lark"), [])
        self.assertTrue(api.calls)
        sent_view = api.calls[-1][1]["view"]
        self.assertIn("Active sessions", sent_view.get("text", ""))

    def test_unknown_slash_outside_session_gets_error_text(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/compact"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "lark_unknown_slash_command")
        self.assertEqual(transport.submitted_turns, [])

    def test_agent_selector_command_is_rejected(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/codex do this"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "agent_selector_rejected")
        self.assertEqual(transport.submitted_turns, [])

    def test_serve_lark_ws_consumes_bridge_queue(self):
        runtime, api, transport = self._runtime()
        payload = self._message_payload()

        class _FakeBridge:
            def __init__(self, *, loop, queue, ack_registry):
                self.queue = queue

            def start(self):
                self.queue.put_nowait(payload)

        asyncio.run(
            runtime.serve_lark_ws(
                max_events=1,
                retry_delay=0,
                bridge_factory=lambda **kwargs: _FakeBridge(**kwargs),
            )
        )

        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])

    def test_describe_reports_lark_websocket_ingress_and_domain(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_OPENAPI_DOMAIN": "https://open.larksuite.com"}
        )

        status = runtime.describe()

        self.assertEqual(status["channel"]["live_ingress"], "websocket")
        self.assertEqual(status["channel"]["openapi_domain"], "https://open.larksuite.com")
        self.assertEqual(status["channel"]["app_id_prefix"], "app-id")
        self.assertEqual(status["profile"], "work")


if __name__ == "__main__":
    unittest.main()
