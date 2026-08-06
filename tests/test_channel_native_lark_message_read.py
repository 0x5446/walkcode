"""Reading Lark messages back: merge_forward expansion and thread history.

The Lark adapter could only ever WRITE (send / edit / react / ack) plus pull
attachment bytes. Two things fall through that gap:

- a merge_forward message's content is just ``{"title", "message_id_list"}``,
  so the inbound parser lands on the title and the agent receives
  "群聊的聊天记录" with none of the conversation;
- a topic the bot is @-ed into has a history the bot cannot see.

Both need the same missing capability — reading messages — which is what
these tests pin down.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ChannelNativeConfig,
    FakeAgentTransport,
    LarkBotApi,
    LarkChannelAdapter,
    TransportCapabilities,
)
from walkcode.channel_native.lark_live import build_operation, message_item_to_dict
from walkcode.channel_native_runtime import ChannelNativeRuntime


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


def _text_item(message_id: str, sender: str, text: str, **extra) -> dict:
    item = {
        "message_id": message_id,
        "msg_type": "text",
        "sender_id": sender,
        "content": json.dumps({"text": text}, ensure_ascii=False),
        "deleted": False,
    }
    item.update(extra)
    return item


class _ReadingLarkApi(LarkBotApi):
    """Fake API that answers the two new read methods."""

    def __init__(self, *, get_items=None, thread_pages=None, fail_get=False):
        self.calls = []
        self.get_items = list(get_items or [])
        self.thread_pages = list(thread_pages or [])
        self.fail_get = fail_get
        super().__init__(caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "getMessage":
            if self.fail_get:
                raise RuntimeError("lark read blew up")
            return {"ok": True, "data": {"items": self.get_items}}
        if method == "listThreadMessages":
            page = self.thread_pages.pop(0) if self.thread_pages else {"items": []}
            return {"ok": True, "data": page}
        return {"ok": True, "data": {"message_id": f"lark-msg-{len(self.calls)}"}}


class BuildOperationTests(unittest.TestCase):
    def test_get_message_operation(self):
        self.assertEqual(
            build_operation("getMessage", {"message_id": "om_forward"}),
            {"kind": "get_message", "message_id": "om_forward"},
        )

    def test_list_thread_operation_clamps_page_size(self):
        op = build_operation("listThreadMessages", {"container_id": "om_root", "page_size": 500})
        self.assertEqual(op["kind"], "list_thread")
        self.assertEqual(op["container_id"], "om_root")
        # Lark rejects page_size > 50.
        self.assertEqual(op["page_size"], 50)
        # 0 reads as "unspecified" and takes the default; a negative value is a
        # bad caller and gets clamped into Lark's 1..50 range.
        self.assertEqual(
            build_operation("listThreadMessages", {"container_id": "om_root", "page_size": 0})[
                "page_size"
            ],
            50,
        )
        self.assertEqual(
            build_operation("listThreadMessages", {"container_id": "om_root", "page_size": -5})[
                "page_size"
            ],
            1,
        )

    def test_message_item_to_dict_flattens_sdk_objects(self):
        class _Sender:
            id = "ou_alpha"
            sender_name = "Alpha Tian"
            sender_type = "user"

        class _Body:
            content = '{"text":"hi"}'

        class _Item:
            message_id = "om_1"
            msg_type = "text"
            chat_id = "oc_chat"
            root_id = "om_root"
            parent_id = ""
            create_time = "1785700000000"
            deleted = False
            sender = _Sender()
            body = _Body()
            mentions = ()

        self.assertEqual(
            message_item_to_dict(_Item()),
            {
                "message_id": "om_1",
                "msg_type": "text",
                "chat_id": "oc_chat",
                "root_id": "om_root",
                "parent_id": "",
                "create_time": "1785700000000",
                "deleted": False,
                "sender_id": "ou_alpha",
                "sender_name": "Alpha Tian",
                "sender_type": "user",
                "content": '{"text":"hi"}',
                "mentions": [],
            },
        )


class AdapterReadTests(unittest.TestCase):
    def test_fetch_message_returns_items(self):
        api = _ReadingLarkApi(get_items=[_text_item("om_1", "ou_a", "hello")])
        adapter = LarkChannelAdapter(api)

        items = asyncio.run(adapter.fetch_message("om_forward"))

        self.assertEqual(len(items), 1)
        self.assertEqual(api.calls[0][0], "getMessage")
        self.assertEqual(api.calls[0][1]["message_id"], "om_forward")

    def test_fetch_message_ignores_blank_id(self):
        api = _ReadingLarkApi()
        adapter = LarkChannelAdapter(api)

        self.assertEqual(asyncio.run(adapter.fetch_message("  ")), [])
        self.assertEqual(api.calls, [])

    def test_fetch_thread_messages_follows_pages_until_limit(self):
        api = _ReadingLarkApi(
            thread_pages=[
                {"items": [_text_item("om_1", "ou_a", "一")], "has_more": True, "page_token": "p2"},
                {"items": [_text_item("om_2", "ou_b", "二")], "has_more": False, "page_token": ""},
            ]
        )
        adapter = LarkChannelAdapter(api)

        items = asyncio.run(adapter.fetch_thread_messages("om_root", limit=10))

        self.assertEqual([item["message_id"] for item in items], ["om_1", "om_2"])
        self.assertEqual(api.calls[1][1]["page_token"], "p2")

    def test_fetch_thread_messages_stops_at_limit(self):
        api = _ReadingLarkApi(
            thread_pages=[
                {
                    "items": [_text_item(f"om_{i}", "ou_a", str(i)) for i in range(5)],
                    "has_more": True,
                    "page_token": "p2",
                },
                {"items": [_text_item("om_late", "ou_a", "late")], "has_more": False},
            ]
        )
        adapter = LarkChannelAdapter(api)

        items = asyncio.run(adapter.fetch_thread_messages("om_root", limit=3))

        self.assertEqual(len(items), 3)
        # Limit reached inside the first page — no second request.
        self.assertEqual(len(api.calls), 1)

    def test_fetch_thread_messages_stops_when_page_token_missing(self):
        # has_more without a token would otherwise loop forever.
        api = _ReadingLarkApi(
            thread_pages=[{"items": [_text_item("om_1", "ou_a", "一")], "has_more": True}]
        )
        adapter = LarkChannelAdapter(api)

        items = asyncio.run(adapter.fetch_thread_messages("om_root", limit=50))

        self.assertEqual(len(items), 1)
        self.assertEqual(len(api.calls), 1)


class RenderMessageLogTests(unittest.TestCase):
    def test_renders_speaker_and_text(self):
        log = LarkChannelAdapter.render_message_log(
            [
                _text_item("om_1", "ou_a", "TTS 那边是期望变成一个正常文案的消息吧？"),
                _text_item("om_2", "ou_b", "加个提示音好一些"),
            ]
        )
        self.assertEqual(
            log,
            "ou_a: TTS 那边是期望变成一个正常文案的消息吧？\nou_b: 加个提示音好一些",
        )

    def test_drops_container_deleted_and_empty(self):
        log = LarkChannelAdapter.render_message_log(
            [
                {"message_id": "om_fwd", "msg_type": "merge_forward", "content": '{"title":"聊天记录"}'},
                _text_item("om_del", "ou_a", "撤回的", deleted=True),
                _text_item("om_empty", "ou_a", "   "),
                _text_item("om_ok", "ou_b", "留下的"),
            ],
            skip_message_id="om_skip",
        )
        self.assertEqual(log, "ou_b: 留下的")

    def test_skips_the_requested_message_id(self):
        log = LarkChannelAdapter.render_message_log(
            [_text_item("om_self", "ou_a", "自己"), _text_item("om_other", "ou_b", "别人")],
            skip_message_id="om_self",
        )
        self.assertEqual(log, "ou_b: 别人")

    def test_caps_message_count_and_says_so(self):
        items = [_text_item(f"om_{i}", "ou_a", f"第{i}条") for i in range(70)]
        log = LarkChannelAdapter.render_message_log(items)
        lines = log.splitlines()
        self.assertEqual(len(lines), LarkChannelAdapter._MAX_RENDERED_MESSAGES + 1)
        # Truncation is stated, not silent.
        self.assertIn("超出上限未展开", lines[-1])

    def test_clips_a_long_message(self):
        long_text = "字" * 2000
        log = LarkChannelAdapter.render_message_log([_text_item("om_1", "ou_a", long_text)])
        self.assertTrue(log.endswith("…"))
        self.assertLess(len(log), LarkChannelAdapter._MAX_RENDERED_CHARS_PER_MESSAGE + 60)


class MergeForwardExpansionTests(unittest.TestCase):
    """End-to-end through the runtime's inbound path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, api):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_STATE_PATH": str(Path(self._tmp.name) / "state.json"),
                "WALKCODE_CWD": self._tmp.name,
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
            }
        )
        return ChannelNativeRuntime.from_config(
            cfg,
            lark_api=api,
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

    @staticmethod
    def _forward_payload(message_id="om_fwd"):
        return {
            "event_id": "evt-fwd",
            "event": {
                "message": {
                    "message_id": message_id,
                    "root_id": "",
                    "chat_id": "oc_chat",
                    "message_type": "merge_forward",
                    "content": json.dumps(
                        {"title": "群聊的聊天记录", "message_id_list": ["om_a", "om_b"]},
                        ensure_ascii=False,
                    ),
                },
                "sender": {"sender_id": {"open_id": "ou_user"}},
            },
        }

    def test_forward_reaches_the_agent_as_its_contents(self):
        api = _ReadingLarkApi(
            get_items=[
                {"message_id": "om_fwd", "msg_type": "merge_forward", "content": '{"title":"群聊的聊天记录"}'},
                _text_item("om_a", "ou_feng", "TTS 那边是期望变成一个正常文案的消息吧？"),
                _text_item("om_b", "ou_tiger", "加个提示音好一些"),
            ]
        )
        runtime = self._runtime(api)

        asyncio.run(runtime.process_lark_event(self._forward_payload()))

        submitted = runtime.transports["claude_headless"].submitted_turns
        self.assertEqual(len(submitted), 1)
        text = submitted[0].text
        self.assertIn("[合并转发：群聊的聊天记录]", text)
        self.assertIn("ou_feng: TTS 那边是期望变成一个正常文案的消息吧？", text)
        self.assertIn("ou_tiger: 加个提示音好一些", text)

    def test_fetch_failure_degrades_to_the_old_behaviour(self):
        # Losing the read must not lose the turn: the title-only text is
        # exactly what shipped before this feature.
        api = _ReadingLarkApi(fail_get=True)
        runtime = self._runtime(api)

        result = asyncio.run(runtime.process_lark_event(self._forward_payload()))

        self.assertTrue(result.accepted)
        submitted = runtime.transports["claude_headless"].submitted_turns
        self.assertEqual(submitted[0].text, "群聊的聊天记录")

    def test_plain_text_message_is_not_read_back(self):
        api = _ReadingLarkApi()
        runtime = self._runtime(api)
        payload = self._forward_payload()
        payload["event"]["message"]["message_type"] = "text"
        payload["event"]["message"]["content"] = json.dumps({"text": "普通消息"}, ensure_ascii=False)

        asyncio.run(runtime.process_lark_event(payload))

        self.assertNotIn("getMessage", [method for method, _ in api.calls])
        self.assertEqual(runtime.transports["claude_headless"].submitted_turns[0].text, "普通消息")

    def test_unauthorized_sender_never_spends_a_read_call(self):
        api = _ReadingLarkApi(get_items=[_text_item("om_a", "ou_x", "secret")])
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_STATE_PATH": str(Path(self._tmp.name) / "state2.json"),
                "WALKCODE_CWD": self._tmp.name,
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
                "LARK_ALLOWED_OPEN_IDS": "ou_someone_else",
            }
        )
        runtime = ChannelNativeRuntime.from_config(
            cfg,
            lark_api=api,
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

        result = asyncio.run(runtime.process_lark_event(self._forward_payload()))

        self.assertFalse(result.accepted)
        self.assertEqual(api.calls, [])


class ThreadContextSeedingTests(unittest.TestCase):
    """Being @-ed into somebody else's topic should carry the discussion in."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._state = 0

    def _runtime(self, api):
        self._state += 1
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_STATE_PATH": str(Path(self._tmp.name) / f"state{self._state}.json"),
                "WALKCODE_CWD": self._tmp.name,
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
            }
        )
        return ChannelNativeRuntime.from_config(
            cfg,
            lark_api=api,
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

    @staticmethod
    def _reply_payload(event_id="evt-1", message_id="om_mention", text="帮我看看这个"):
        return {
            "event_id": event_id,
            "event": {
                "message": {
                    "message_id": message_id,
                    "root_id": "om_topic_root",
                    "chat_id": "oc_chat",
                    "message_type": "text",
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
                "sender": {"sender_id": {"open_id": "ou_user"}},
            },
        }

    def test_first_mention_carries_the_topic_history(self):
        api = _ReadingLarkApi(
            thread_pages=[
                {
                    "items": [
                        _text_item("om_root", "ou_alpha", "server.error 协议不动"),
                        _text_item("om_1", "ou_feng", "TTS 那边是期望变成一个正常文案的消息吧？"),
                        _text_item("om_2", "ou_tiger", "加个提示音好一些"),
                        _text_item("om_mention", "ou_user", "帮我看看这个"),
                    ],
                    "has_more": False,
                }
            ]
        )
        runtime = self._runtime(api)

        asyncio.run(runtime.process_lark_event(self._reply_payload()))

        text = runtime.transports["claude_headless"].submitted_turns[0].text
        self.assertIn("[话题已有讨论]", text)
        self.assertIn("ou_feng: TTS 那边是期望变成一个正常文案的消息吧？", text)
        self.assertIn("ou_tiger: 加个提示音好一些", text)
        self.assertIn("[本次请求]\n帮我看看这个", text)
        # The mention itself is not duplicated into the history block.
        self.assertEqual(text.count("帮我看看这个"), 1)
        self.assertEqual(api.calls[0][0], "listThreadMessages")
        self.assertEqual(api.calls[0][1]["container_id"], "om_topic_root")

    def test_second_message_in_our_own_thread_is_not_reseeded(self):
        api = _ReadingLarkApi(
            thread_pages=[
                {"items": [_text_item("om_1", "ou_feng", "既有讨论")], "has_more": False},
                {"items": [_text_item("om_1", "ou_feng", "既有讨论")], "has_more": False},
            ]
        )
        runtime = self._runtime(api)

        asyncio.run(runtime.process_lark_event(self._reply_payload()))
        reads_after_first = len([m for m, _ in api.calls if m == "listThreadMessages"])
        asyncio.run(
            runtime.process_lark_event(
                self._reply_payload(event_id="evt-2", message_id="om_mention2", text="继续")
            )
        )

        self.assertEqual(reads_after_first, 1)
        self.assertEqual(len([m for m, _ in api.calls if m == "listThreadMessages"]), 1)
        second = runtime.transports["claude_headless"].submitted_turns[-1].text
        self.assertNotIn("[话题已有讨论]", second)
        self.assertEqual(second, "继续")

    def test_thread_read_failure_degrades_to_the_mention(self):
        class _FailingThreadApi(_ReadingLarkApi):
            async def _call(self, method, payload):
                self.calls.append((method, dict(payload)))
                if method == "listThreadMessages":
                    raise RuntimeError("bot is not in that chat")
                return {"ok": True, "data": {"message_id": f"lark-msg-{len(self.calls)}"}}

        api = _FailingThreadApi()
        runtime = self._runtime(api)

        result = asyncio.run(runtime.process_lark_event(self._reply_payload()))

        self.assertTrue(result.accepted)
        self.assertEqual(
            runtime.transports["claude_headless"].submitted_turns[0].text, "帮我看看这个"
        )

    def test_non_reply_message_does_not_read_a_thread(self):
        api = _ReadingLarkApi()
        runtime = self._runtime(api)
        payload = self._reply_payload()
        payload["event"]["message"]["root_id"] = ""

        asyncio.run(runtime.process_lark_event(payload))

        self.assertNotIn("listThreadMessages", [method for method, _ in api.calls])


if __name__ == "__main__":
    unittest.main()
