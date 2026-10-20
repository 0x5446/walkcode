import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from walkcode.channel_native import (
    AgentEvent,
    AgentEventType,
    BlockedReason,
    ChannelConfigError,
    ChannelNativeConfig,
    FakeAgentTransport,
    TransportCapabilities,
)
from walkcode.channel_native.models import ChannelBinding, PermanentDeliveryError
from walkcode.channel_native.slack_adapter import SlackChannelAdapter
from walkcode.channel_native.slack_blocks import (
    MARKDOWN_MESSAGE_LIMIT,
    defuse_mentions,
    render_slack_messages,
    split_markdown,
)
from walkcode.channel_native.slack_live import (
    SlackIngressBridge,
    SlackUncertainDeliveryError,
    ingress_relevant,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime

TEAM = "T1"
BOT = "UBOT"
USER = "UOWNER"


class _FakeSlackApi:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.history: list[dict] = []
        self._seq = 0

    def identity(self):
        return TEAM, BOT

    async def call(self, method, payload):
        self.calls.append((method, dict(payload)))
        queued = self.failures.get(method)
        if queued:
            raise queued.pop(0)
        if method == "chat.postMessage":
            self._seq += 1
            ts = f"1700000000.{self._seq:06d}"
            self.history.append({"ts": ts, "bot_id": "B1", "text": payload.get("text", "")})
            return {"ok": True, "ts": ts}
        if method in {"conversations.replies", "conversations.history"}:
            return {"ok": True, "messages": list(self.history)}
        if method == "files.info":
            return {"ok": True, "file": {"url_private_download": "https://files/x", "name": "report.pdf", "mimetype": "application/pdf"}}
        return {"ok": True}

    async def download(self, url, destination):
        destination.write_bytes(b"%PDF-1.7")
        return 8

    def sent(self, method="chat.postMessage"):
        return [payload for name, payload in self.calls if name == method]


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


def _message(text, *, ts="1700000100.000100", thread_ts=None, channel="C1", channel_type="channel", user=USER, **event_extra):
    event = {"type": "message", "channel": channel, "channel_type": channel_type, "user": user, "text": text, "ts": ts, "team": TEAM}
    if thread_ts:
        event["thread_ts"] = thread_ts
    event.update(event_extra)
    return {
        "type": "events_api",
        "envelope_id": "env-1",
        "payload": {
            "team_id": TEAM,
            "authorizations": [{"team_id": TEAM, "user_id": BOT, "is_bot": True}],
            "event": event,
        },
    }


def _click(value, *, message_ts, thread_ts=None, user=USER, state=None, action_ts="1700000200.0001"):
    message = {"ts": message_ts}
    if thread_ts:
        message["thread_ts"] = thread_ts
    return {
        "type": "interactive",
        "envelope_id": "env-2",
        "payload": {
            "type": "block_actions",
            "team": {"id": TEAM},
            "user": {"id": user, "team_id": TEAM},
            "channel": {"id": "C1"},
            "message": message,
            "container": {"message_ts": message_ts, "channel_id": "C1"},
            "actions": [{"action_id": "walkcode:0:allow", "value": json.dumps(value), "action_ts": action_ts}],
            "state": state or {"values": {}},
        },
    }


class SlackConfigTests(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "WALKCODE_CHANNEL": "slack",
            "SLACK_BOT_TOKEN": "xoxb-1",
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_ALLOWED_USER_IDS": USER,
            "WALKCODE_AGENT": "claude",
            "WALKCODE_PROFILE": "work",
            "WALKCODE_STATE_PATH": "/tmp/walkcode-slack-test-state.json",
        }
        env.update(extra)
        return env

    def test_parses_tokens_and_shared_option_names(self):
        config = ChannelNativeConfig.from_env(self._env(SLACK_ALLOWED_CHANNEL_IDS="C1,C2", WALKCODE_SLACK_TUI_CHANNEL_ID="C9"))
        self.assertEqual(config.channel.kind, "slack")
        self.assertEqual(config.channel.options["allowed_chat_ids"], ("C1", "C2"))
        self.assertEqual(config.channel.options["allowed_sender_ids"], (USER,))
        self.assertEqual(config.channel.options["tui_chat_id"], "C9")

    def test_user_allowlist_is_required(self):
        with self.assertRaisesRegex(ChannelConfigError, "SLACK_ALLOWED_USER_IDS"):
            ChannelNativeConfig.from_env(self._env(SLACK_ALLOWED_USER_IDS=""))

    def test_rejects_swapped_tokens(self):
        with self.assertRaisesRegex(ChannelConfigError, "xoxb"):
            ChannelNativeConfig.from_env(self._env(SLACK_BOT_TOKEN="xapp-1"))
        with self.assertRaisesRegex(ChannelConfigError, "xapp"):
            ChannelNativeConfig.from_env(self._env(SLACK_APP_TOKEN="xoxb-1"))

    def test_telegram_still_names_adr_0069(self):
        with self.assertRaisesRegex(ChannelConfigError, "ADR 0069"):
            ChannelNativeConfig.from_env(self._env(WALKCODE_CHANNEL="telegram"))


class SlackIngressFilterTests(unittest.TestCase):
    def test_bot_own_and_edited_messages_are_dropped(self):
        self.assertFalse(ingress_relevant(_message(f"<@{BOT}> hi", user=BOT)))
        self.assertFalse(ingress_relevant(_message(f"<@{BOT}> hi", bot_id="B1")))
        self.assertFalse(ingress_relevant(_message(f"<@{BOT}> hi", subtype="message_changed")))

    def test_top_level_channel_chatter_without_mention_is_dropped(self):
        self.assertFalse(ingress_relevant(_message("lunch?")))
        self.assertTrue(ingress_relevant(_message(f"<@{BOT}> run tests")))
        self.assertTrue(ingress_relevant(_message("more", thread_ts="1700000000.000001")))
        self.assertTrue(ingress_relevant(_message("hi", channel="D1", channel_type="im")))
        self.assertTrue(ingress_relevant(_message("file", subtype="file_share", channel_type="im")))

    def test_other_organizations_are_dropped(self):
        self.assertFalse(ingress_relevant(_message(f"<@{BOT}> hi", team="T_OTHER")))
        click = _click({"action": "allow", "token": "t"}, message_ts="1")
        click["payload"]["user"]["team_id"] = "T_OTHER"
        self.assertFalse(ingress_relevant(click))
        self.assertTrue(ingress_relevant(_click({"action": "allow", "token": "t"}, message_ts="1")))


class SlackParseTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SlackChannelAdapter(_FakeSlackApi())

    def test_mention_is_stripped_and_addressed(self):
        inbound = self.adapter.parse_event(_message(f"<@{BOT}> fix <https://x.io/a|the bug> &amp; ship"))
        self.assertTrue(inbound.addressed)
        self.assertEqual(inbound.text, "fix the bug (https://x.io/a) & ship")
        self.assertEqual(inbound.event_id, f"slack:{TEAM}:C1:1700000100.000100")
        self.assertEqual((inbound.thread_id, inbound.root_message_id, inbound.message_id), ("1700000100.000100",) * 3)

    def test_message_and_app_mention_share_one_event_id(self):
        a = self.adapter.parse_event(_message(f"<@{BOT}> go"))
        mention = _message(f"<@{BOT}> go")
        mention["payload"]["event"]["type"] = "app_mention"
        b = self.adapter.parse_event(mention)
        self.assertEqual(a.event_id, b.event_id)

    def test_thread_reply_without_mention_is_not_addressed(self):
        inbound = self.adapter.parse_event(_message("more", thread_ts="1700000000.000001"))
        self.assertFalse(inbound.addressed)
        self.assertEqual(inbound.root_message_id, "1700000000.000001")

    def test_dm_is_addressed(self):
        self.assertTrue(self.adapter.parse_event(_message("hi", channel="D1", channel_type="im")).addressed)

    def test_bang_commands_become_slash_commands(self):
        self.assertEqual(self.adapter.parse_event(_message(f"<@{BOT}> !status")).text, "/status")
        self.assertEqual(self.adapter.parse_event(_message(f"<@{BOT}> !!compact")).text, "//compact")
        self.assertEqual(self.adapter.parse_event(_message(f"<@{BOT}> ! not a command")).text, "! not a command")

    def test_files_and_shared_messages(self):
        inbound = self.adapter.parse_event(
            _message(
                f"<@{BOT}> look",
                files=[{"id": "F1", "mimetype": "image/png"}],
                attachments=[{"is_share": True, "author_name": "Ann", "text": "the log says &lt;oops&gt;"}],
            )
        )
        self.assertEqual([a.source_id for a in inbound.attachments], ["F1"])
        self.assertIn("[转发消息] Ann: the log says <oops>", inbound.text)

    def test_block_actions_fold_state_into_form(self):
        state = {
            "values": {
                "q0": {"value": {"type": "static_select", "selected_option": {"value": "1"}}},
                "q1": {"value": {"type": "multi_static_select", "selected_options": [{"value": "0"}, {"value": "2"}]}},
                "q1_other": {"value": {"type": "plain_text_input", "value": "custom"}},
                "q2_other": {"value": {"type": "plain_text_input", "value": None}},
            }
        }
        inbound = self.adapter.parse_event(
            _click({"action": "submit_all", "token": "tok"}, message_ts="1700000000.5", thread_ts="1700000000.1", state=state)
        )
        self.assertEqual(inbound.callback["token"], "tok")
        self.assertEqual(inbound.callback["form"], {"q0": "1", "q1": ["0", "2"], "q1_other": "custom"})
        self.assertEqual((inbound.message_id, inbound.root_message_id), ("1700000000.5", "1700000000.1"))


class SlackRenderTests(unittest.TestCase):
    def test_long_markdown_splits_with_balanced_fences(self):
        text = "intro\n\n```python\n" + "print('x')\n" * 3000 + "```\nafter"
        chunks = split_markdown(text, 4000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)
            self.assertEqual(chunk.count("```") % 2, 0)
        self.assertEqual(len(render_slack_messages({"type": "text", "text": "y" * (MARKDOWN_MESSAGE_LIMIT * 2 + 5)})), 3)

    def test_agent_prose_cannot_ping_the_channel(self):
        self.assertEqual(defuse_mentions("hey <!channel> and <@U123|bob>"), "hey @channel and @U123")

    def test_permission_card_buttons_carry_token_and_confirm(self):
        message = render_slack_messages(
            {
                "type": "permission_prompt",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf <x>"},
                "actions": [
                    {"action": "allow", "token": "t1", "label": "允许"},
                    {"action": "always_allow", "token": "t2", "label": "始终允许"},
                    {"action": "deny", "token": "t3", "label": "拒绝"},
                ],
            }
        )[0]
        actions = [b for b in message["blocks"] if b["type"] == "actions"][0]["elements"]
        self.assertEqual(json.loads(actions[0]["value"]), {"action": "allow", "token": "t1"})
        self.assertEqual(actions[0]["style"], "primary")
        self.assertEqual(actions[2]["style"], "danger")
        self.assertIn("confirm", actions[1])
        self.assertIn("&lt;x&gt;", json.dumps(message["blocks"], ensure_ascii=False))

    def test_batch_question_uses_input_blocks_named_like_lark_fields(self):
        message = render_slack_messages(
            {
                "type": "ask_user_question",
                "questions": [
                    {"header": "Pick", "options": [{"label": "A"}, {"label": "B"}], "other": True},
                    {"header": "Many", "options": [{"label": "X"}], "allow_multiple": True},
                ],
                "submit": {"action": "submit_all", "token": "tok"},
            }
        )[0]
        inputs = {b["block_id"]: b["element"]["type"] for b in message["blocks"] if b["type"] == "input"}
        self.assertEqual(inputs, {"q0": "static_select", "q0_other": "plain_text_input", "q1": "multi_static_select"})

    def test_tool_progress_folds_long_bursts(self):
        lines = [{"tool_name": f"t{i}", "status": "completed"} for i in range(40)]
        message = render_slack_messages({"type": "tool_progress", "lines": lines})[0]
        body = json.dumps(message["blocks"], ensure_ascii=False)
        self.assertIn("已折叠前 10 行", body)
        self.assertLessEqual(len(message["text"]), 4000)


class SlackAdapterDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.api = _FakeSlackApi()
        self.adapter = SlackChannelAdapter(self.api)
        self.thread = ChannelBinding("slack", TEAM, "C1", "1700000000.000001", "1700000000.000001")

    def test_replies_go_into_the_thread_and_long_text_is_not_truncated(self):
        asyncio.run(self.adapter.send_view(self.thread, {"type": "text", "text": "z" * (MARKDOWN_MESSAGE_LIMIT + 10)}))
        posts = self.api.sent()
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(p["thread_ts"] == "1700000000.000001" for p in posts))

    def test_root_status_card_mirrors_native_agent_session(self):
        root = ChannelBinding("slack", TEAM, "C1", "", "")
        ts = asyncio.run(self.adapter.send_view(root, {"type": "health", "status": "running", "title": "fix bug"}))
        self.assertNotIn("thread_ts", self.api.sent()[0])
        status = self.api.sent("agents.sessions.setStatus")
        self.assertEqual(status, [{"channel_id": "C1", "thread_ts": ts, "status": "processing", "title": "fix bug"}])
        thread = ChannelBinding("slack", TEAM, "C1", ts, ts)
        asyncio.run(self.adapter.edit_view(thread, ts, {"type": "health", "status": "waiting_permission", "title": "fix bug"}))
        self.assertEqual(self.api.sent("agents.sessions.setStatus")[-1]["status"], "suspended")

    def test_native_session_sync_failure_never_fails_delivery(self):
        self.api.failures["agents.sessions.setStatus"] = [PermanentDeliveryError("not an agent app")]
        ts = asyncio.run(self.adapter.send_view(ChannelBinding("slack", TEAM, "C1", "", ""), {"type": "health", "status": "idle"}))
        self.assertTrue(ts)

    def test_uncertain_send_is_not_duplicated_on_retry(self):
        view = {"type": "text", "text": "answer"}
        self.api.failures["chat.postMessage"] = [SlackUncertainDeliveryError("timeout")]
        with self.assertRaises(SlackUncertainDeliveryError):
            asyncio.run(self.adapter.send_view(self.thread, view))
        # The timed-out send actually landed.
        self.api.history.append({"ts": "1700000000.900000", "bot_id": "B1", "text": "answer"})
        ts = asyncio.run(self.adapter.send_view(self.thread, view))
        self.assertEqual(ts, "1700000000.900000")
        self.assertEqual(len(self.api.sent()), 1)

    def test_download_uses_files_info_and_keeps_extension(self):
        from walkcode.channel_native.models import AttachmentRef

        ref = asyncio.run(self.adapter.download_attachment(AttachmentRef(source_id="F1", mime="", source_message_id="C1:1")))
        self.assertTrue(ref.local_path.endswith(".pdf"))
        self.assertEqual(Path(ref.local_path).read_bytes(), b"%PDF-1.7")
        Path(ref.local_path).unlink()


class SlackIngressBridgeTests(unittest.TestCase):
    class _Client:
        def __init__(self):
            self.acks = []

        def send_socket_mode_response(self, response):
            self.acks.append(response.envelope_id)

    class _Request:
        def __init__(self, envelope):
            self.type = envelope["type"]
            self.envelope_id = envelope["envelope_id"]
            self.payload = envelope["payload"]
            self.retry_attempt = None

    def _bridge(self, persist):
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return SlackIngressBridge({}, loop=loop, queue=asyncio.Queue(), persist_event=persist), loop

    def test_envelope_is_persisted_before_ack(self):
        order = []
        client = self._Client()

        def persist(envelope):
            order.append(("persist", envelope["envelope_id"], len(client.acks)))
            return Path("/tmp/x.json")

        bridge, loop = self._bridge(persist)
        bridge.on_request(client, self._Request(_message(f"<@{BOT}> go")))
        self.assertEqual(order, [("persist", "env-1", 0)])
        self.assertEqual(client.acks, ["env-1"])

    def test_failed_inbox_write_leaves_envelope_unacked(self):
        client = self._Client()

        def persist(envelope):
            raise OSError("disk full")

        bridge, loop = self._bridge(persist)
        bridge.on_request(client, self._Request(_message(f"<@{BOT}> go")))
        self.assertEqual(client.acks, [])

    def test_irrelevant_envelope_is_acked_without_persisting(self):
        client = self._Client()
        persisted = []
        bridge, loop = self._bridge(lambda e: persisted.append(e) or Path("/tmp/x"))
        bridge.on_request(client, self._Request(_message("lunch?")))
        self.assertEqual((persisted, client.acks), ([], ["env-1"]))


class SlackRuntimeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _runtime(self, events=None):
        env = {
            "WALKCODE_CHANNEL": "slack",
            "SLACK_BOT_TOKEN": "xoxb-1",
            "SLACK_APP_TOKEN": "xapp-1",
            "SLACK_ALLOWED_USER_IDS": USER,
            "WALKCODE_AGENT": "claude",
            "WALKCODE_PROFILE": "work",
            "WALKCODE_STATE_PATH": str(Path(self._tmp.name) / "state.json"),
            "WALKCODE_CWD": self._tmp.name,
        }
        api = _FakeSlackApi()
        transport = FakeAgentTransport(
            "claude_headless",
            _transport_caps(),
            scripted_events=events or [AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
        )
        runtime = ChannelNativeRuntime.from_config(
            ChannelNativeConfig.from_env(env), slack_api=api, transports={"claude_headless": transport}
        )
        return runtime, api, transport

    def test_mention_roots_a_session_on_a_status_card(self):
        runtime, api, transport = self._runtime()
        result = asyncio.run(runtime.process_channel_event(_message(f"<@{BOT}> run tests")))
        self.assertTrue(result.accepted)
        root, echo = api.sent()[:2]
        self.assertNotIn("thread_ts", root)
        self.assertEqual(echo["thread_ts"], api.history[0]["ts"])
        self.assertIn("run tests", echo["text"])
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        self.assertTrue(runtime._inbox_dir.name.endswith(".slack-inbox.d"))

    def test_unaddressed_thread_chatter_is_ignored_without_a_session(self):
        runtime, api, transport = self._runtime()
        result = asyncio.run(runtime.process_channel_event(_message("hmm", thread_ts="1600000000.000001")))
        self.assertEqual(result.reason, "not_addressed")
        self.assertEqual((api.calls, transport.submitted_turns), ([], []))

    def test_reply_in_the_bot_thread_continues_without_mention(self):
        runtime, api, transport = self._runtime(
            events=[
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "one", "agent_session_id": "a1"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "two", "agent_session_id": "a1"}),
            ]
        )
        asyncio.run(runtime.process_channel_event(_message(f"<@{BOT}> first")))
        root_ts = api.history[0]["ts"]
        asyncio.run(runtime.process_channel_event(_message("second", ts="1700000300.000100", thread_ts=root_ts)))
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["first", "second"])

    def test_strangers_are_ignored_and_their_clicks_rejected_privately(self):
        runtime, api, transport = self._runtime()
        result = asyncio.run(runtime.process_channel_event(_message(f"<@{BOT}> hi", user="USTRANGER")))
        self.assertEqual(result.reason, BlockedReason.UNAUTHORIZED)
        click = _click({"action": "allow", "token": "t"}, message_ts="1700000000.5", user="USTRANGER")
        result = asyncio.run(runtime.process_channel_event(click))
        self.assertEqual(result.reason, BlockedReason.UNAUTHORIZED)
        ephemeral = api.sent("chat.postEphemeral")
        self.assertEqual(len(ephemeral), 1)
        self.assertEqual(ephemeral[0]["user"], "USTRANGER")
        self.assertEqual(transport.submitted_turns, [])

    def test_bang_status_is_a_walkcode_command(self):
        runtime, api, transport = self._runtime()
        result = asyncio.run(runtime.process_channel_event(_message(f"<@{BOT}> !status")))
        self.assertTrue(result.accepted)
        self.assertEqual(transport.submitted_turns, [])


if __name__ == "__main__":
    unittest.main()
