import asyncio
import json
import threading
import unittest

from walkcode.channel_native import PermanentDeliveryError, TransientDeliveryError
from walkcode.channel_native.lark_live import (
    AckRegistry,
    LarkIngressBridge,
    LarkLiveCaller,
    SdkTransport,
    build_lark_live_api,
    build_operation,
    is_permanent_lark_code,
    normalize_card_action_event,
    normalize_message_event,
)


class BuildOperationTests(unittest.TestCase):
    def test_send_message_with_root_becomes_thread_reply(self):
        operation = build_operation(
            "sendMessage",
            {
                "chat_id": "oc_chat",
                "root_id": "om_root",
                "text": "hello",
                "view": {"type": "turn_completed", "message": "hello"},
            },
        )

        self.assertEqual(operation["kind"], "reply")
        self.assertEqual(operation["message_id"], "om_root")
        self.assertTrue(operation["reply_in_thread"])
        self.assertEqual(operation["msg_type"], "post")
        content = json.loads(operation["content"])
        self.assertEqual(content["zh_cn"]["content"][0][0]["text"], "hello")

    def test_send_card_without_root_creates_in_chat(self):
        operation = build_operation(
            "sendCard",
            {
                "chat_id": "oc_chat",
                "root_id": "",
                "text": "Permission requested: Bash",
                "view": {
                    "type": "permission_prompt",
                    "tool_name": "Bash",
                    "tool_input": {"command": "ls"},
                    "actions": [{"action": "allow_once", "label": "Allow once", "token": "t1"}],
                },
            },
        )

        self.assertEqual(operation["kind"], "create")
        self.assertEqual(operation["chat_id"], "oc_chat")
        self.assertEqual(operation["msg_type"], "interactive")
        content = json.loads(operation["content"])
        self.assertEqual(content["header"]["template"], "orange")

    def test_edit_card_becomes_patch(self):
        operation = build_operation(
            "editCard",
            {
                "message_id": "om_card",
                "text": "t",
                "view": {"type": "health", "status": "stopped", "title": "t", "session_id": "s",
                         "transport": "claude_headless", "elapsed": 1.0, "cwd": "/tmp"},
            },
        )

        self.assertEqual(operation["kind"], "patch")
        self.assertEqual(operation["message_id"], "om_card")
        self.assertIn("已结束", operation["content"])

    def test_download_resource_operation(self):
        operation = build_operation(
            "downloadResource",
            {"message_id": "om_msg", "file_key": "img_key", "type": "image"},
        )

        self.assertEqual(operation["kind"], "download")
        self.assertEqual(operation["file_key"], "img_key")
        self.assertEqual(operation["type"], "image")

    def test_unknown_method_is_permanent(self):
        with self.assertRaises(PermanentDeliveryError):
            build_operation("bogusMethod", {})


class _FakeResponse:
    def __init__(self, ok=True, code=0, msg=""):
        self.code = code
        self.msg = msg
        self._ok = ok

    def success(self):
        return self._ok


class ErrorClassificationTests(unittest.TestCase):
    def test_permanent_code_table(self):
        self.assertTrue(is_permanent_lark_code(230001))
        self.assertFalse(is_permanent_lark_code(99991663))
        self.assertFalse(is_permanent_lark_code(0))

    def test_check_raises_permanent_for_bad_payload_code(self):
        transport = SdkTransport("a", "s")

        with self.assertRaises(PermanentDeliveryError):
            transport._check(_FakeResponse(ok=False, code=230001, msg="bad param"), "send")

    def test_check_raises_transient_for_rate_limit_and_unknown(self):
        transport = SdkTransport("a", "s")

        for code in (99991400, 11232, 500):
            with self.assertRaises(TransientDeliveryError):
                transport._check(_FakeResponse(ok=False, code=code), "send")

    def test_check_passes_success_through(self):
        transport = SdkTransport("a", "s")
        resp = _FakeResponse(ok=True)

        self.assertIs(transport._check(resp, "send"), resp)


class AckRegistryTests(unittest.TestCase):
    def test_resolve_hits_registered_future(self):
        registry = AckRegistry()
        future = registry.register("evt-1")

        resolved = registry.resolve("evt-1", {"toast": {"type": "success", "content": "ok"}})

        self.assertTrue(resolved)
        self.assertEqual(future.result(timeout=0)["toast"]["content"], "ok")

    def test_resolve_strips_lark_prefix_from_adapter_event_id(self):
        registry = AckRegistry()
        future = registry.register("evt-2")

        self.assertTrue(registry.resolve("lark:evt-2", {"ok": True}))
        self.assertTrue(future.done())

    def test_resolve_unknown_or_replayed_event_is_noop(self):
        registry = AckRegistry()
        registry.register("evt-3")
        self.assertTrue(registry.resolve("evt-3", {}))

        self.assertFalse(registry.resolve("evt-3", {}))
        self.assertFalse(registry.resolve("never-registered", {}))


class LarkLiveCallerTests(unittest.TestCase):
    def test_ack_callback_resolves_registry_without_transport(self):
        registry = AckRegistry()
        future = registry.register("evt-9")
        calls = []
        caller = LarkLiveCaller(lambda op: calls.append(op), ack_registry=registry)

        result = asyncio.run(caller("ackCallback", {"event_id": "lark:evt-9", "token": "t"}))

        self.assertTrue(result["resolved"])
        self.assertEqual(calls, [])
        self.assertEqual(future.result(timeout=0)["toast"]["type"], "success")

    def test_send_routes_through_transport(self):
        seen = []

        def transport(operation):
            seen.append(operation)
            return {"data": {"message_id": "om_new"}}

        caller = LarkLiveCaller(transport)
        result = asyncio.run(
            caller(
                "sendMessage",
                {"chat_id": "oc", "root_id": "", "text": "hi", "view": {"text": "hi"}},
            )
        )

        self.assertEqual(result["data"]["message_id"], "om_new")
        self.assertEqual(seen[0]["kind"], "create")

    def test_build_lark_live_api_returns_wired_bot_api(self):
        api = build_lark_live_api(
            {"app_id": "a", "app_secret": "s"},
            {"openapi_domain": "https://open.larksuite.com"},
            transport=lambda op: {"data": {"message_id": "om"}},
        )

        result = asyncio.run(api.call("sendMessage", {"chat_id": "oc", "text": "x", "view": {"text": "x"}}))

        self.assertEqual(result["data"]["message_id"], "om")


class NormalizeEventTests(unittest.TestCase):
    def test_message_event_dict_passthrough(self):
        payload = normalize_message_event(
            {
                "header": {"event_id": "evt-1"},
                "event": {"message": {"message_id": "om"}, "sender": {}},
            }
        )

        self.assertEqual(payload["event_id"], "evt-1")
        self.assertEqual(payload["event"]["message"]["message_id"], "om")

    def test_message_event_object_attributes_are_flattened(self):
        class Header:
            def __init__(self):
                self.event_id = "evt-2"

        class Message:
            def __init__(self):
                self.message_id = "om_x"
                self.chat_id = "oc_x"

        class Event:
            def __init__(self):
                self.message = Message()

        class Data:
            def __init__(self):
                self.header = Header()
                self.event = Event()

        payload = normalize_message_event(Data())

        self.assertEqual(payload["event_id"], "evt-2")
        self.assertEqual(payload["event"]["message"]["chat_id"], "oc_x")

    def test_card_action_event_lifts_context_ids(self):
        payload = normalize_card_action_event(
            {
                "header": {"event_id": "evt-3"},
                "event": {
                    "operator": {"open_id": "ou_user"},
                    "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
                    "action": {"value": {"token": "tok", "action": "allow_once"}},
                },
            }
        )

        self.assertEqual(payload["event"]["message_id"], "om_card")
        self.assertEqual(payload["event"]["chat_id"], "oc_chat")
        self.assertEqual(payload["event"]["action"]["value"]["token"], "tok")


class LarkIngressBridgeTests(unittest.TestCase):
    def _bridge(self, loop, queue, registry, ack_timeout=0.2):
        return LarkIngressBridge(
            {"app_id": "a", "app_secret": "s"},
            {"openapi_domain": "https://open.feishu.cn"},
            loop=loop,
            queue=queue,
            ack_registry=registry,
            ack_timeout=ack_timeout,
            ws_client_factory=lambda bridge: None,
        )

    def test_message_callback_enqueues_into_loop_queue(self):
        async def scenario():
            queue: asyncio.Queue = asyncio.Queue()
            bridge = self._bridge(asyncio.get_running_loop(), queue, AckRegistry())

            await asyncio.to_thread(
                bridge.on_message,
                {"header": {"event_id": "evt-1"}, "event": {"message": {"message_id": "om"}}},
            )
            return await asyncio.wait_for(queue.get(), timeout=1)

        payload = asyncio.run(scenario())
        self.assertEqual(payload["event_id"], "evt-1")

    def test_card_action_waits_for_ack_and_returns_it_inline(self):
        async def scenario():
            queue: asyncio.Queue = asyncio.Queue()
            registry = AckRegistry()
            bridge = self._bridge(asyncio.get_running_loop(), queue, registry, ack_timeout=2)

            def click():
                return bridge.on_card_action(
                    {
                        "header": {"event_id": "evt-2"},
                        "event": {"action": {"value": {"token": "tok"}}, "context": {}},
                    }
                )

            click_task = asyncio.create_task(asyncio.to_thread(click))
            payload = await asyncio.wait_for(queue.get(), timeout=1)
            registry.resolve(payload["event_id"], {"toast": {"type": "success", "content": "done"}})
            return await asyncio.wait_for(click_task, timeout=2)

        response = asyncio.run(scenario())
        self.assertEqual(response["toast"]["content"], "done")

    def test_card_action_times_out_to_neutral_toast(self):
        async def scenario():
            queue: asyncio.Queue = asyncio.Queue()
            bridge = self._bridge(asyncio.get_running_loop(), queue, AckRegistry(), ack_timeout=0.05)

            return await asyncio.to_thread(
                bridge.on_card_action,
                {"header": {"event_id": "evt-3"}, "event": {"action": {}, "context": {}}},
            )

        response = asyncio.run(scenario())
        self.assertEqual(response["toast"]["type"], "info")


if __name__ == "__main__":
    unittest.main()
