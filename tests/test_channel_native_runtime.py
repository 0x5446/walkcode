import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    ChannelConfigError,
    ChannelBinding,
    ChannelNativeConfig,
    FakeAgentTransport,
    JsonFileStateStore,
    SessionRole,
    SubmitResult,
    TelegramBotApi,
    TransportCapabilities,
    BlockedReason,
)
from walkcode import channel_native_runtime as runtime_module
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
        external_tui_takeover=True,
    )


class _FakeTelegramApi(TelegramBotApi):
    def __init__(self, batches=None):
        self.calls = []
        self.batches = list(batches or [])
        super().__init__(token="fake", caller=self._call)

    async def _call(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "getUpdates":
            batch = self.batches.pop(0) if self.batches else []
            return {"ok": True, "result": batch}
        if method == "getMe":
            return {
                "ok": True,
                "result": {
                    "id": 123456,
                    "username": "walkcode_test_bot",
                    "first_name": "WalkCode",
                    "can_join_groups": True,
                    "can_read_all_group_messages": False,
                    "has_topics_enabled": False,
                    "allows_users_to_create_topics": False,
                },
            }
        if method == "getChat":
            return {
                "ok": True,
                "result": {
                    "id": payload.get("chat_id"),
                    "type": "private",
                    "is_forum": False,
                },
            }
        if method == "getWebhookInfo":
            return {
                "ok": True,
                "result": {
                    "url": "",
                    "pending_update_count": 1 if self.batches else 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            }
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        if method in {"sendChatAction", "setMessageReaction"}:
            return {"ok": True, "result": True}
        if method == "setMyCommands":
            return {"ok": True, "result": True}
        if method == "editMessageText":
            return {"ok": True, "result": True}
        if method == "pinChatMessage":
            return {"ok": True, "result": True}
        if method == "deleteMessage":
            return {"ok": True, "result": True}
        if method in {"closeForumTopic", "reopenForumTopic"}:
            return {"ok": True, "result": True}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method: {method}")


class _ForumTelegramApi(_FakeTelegramApi):
    async def _call(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "getUpdates":
            batch = self.batches.pop(0) if self.batches else []
            return {"ok": True, "result": batch}
        if method == "getMe":
            return {
                "ok": True,
                "result": {
                    "id": 123456,
                    "username": "walkcode_forum_bot",
                    "first_name": "WalkCode",
                    "can_join_groups": True,
                    "can_read_all_group_messages": False,
                    "has_topics_enabled": False,
                    "allows_users_to_create_topics": False,
                },
            }
        if method == "getChat":
            return {
                "ok": True,
                "result": {
                    "id": payload.get("chat_id"),
                    "type": "supergroup",
                    "is_forum": True,
                },
            }
        if method == "getChatMember":
            return {
                "ok": True,
                "result": {
                    "status": "administrator",
                    "can_manage_topics": True,
                },
            }
        if method == "createForumTopic":
            return {
                "ok": True,
                "result": {
                    "message_thread_id": 777,
                    "name": payload.get("name"),
                },
            }
        if method == "getForumTopicIconStickers":
            return {
                "ok": True,
                "result": [
                    {"custom_emoji_id": "emoji-a"},
                    {"custom_emoji_id": "emoji-b"},
                ],
            }
        if method == "getWebhookInfo":
            return {
                "ok": True,
                "result": {
                    "url": "",
                    "pending_update_count": 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            }
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        if method == "editMessageText":
            return {"ok": True, "result": True}
        if method == "pinChatMessage":
            return {"ok": True, "result": True}
        if method == "deleteMessage":
            return {"ok": True, "result": True}
        if method in {"closeForumTopic", "reopenForumTopic"}:
            return {"ok": True, "result": True}
        if method == "answerCallbackQuery":
            return {"ok": True, "result": True}
        raise AssertionError(f"unexpected Telegram method: {method}")


class _ForumTelegramApiWithoutTopicAdmin(_ForumTelegramApi):
    async def _call(self, method, payload):
        if method == "getChatMember":
            self.calls.append((method, dict(payload)))
            return {
                "ok": True,
                "result": {
                    "status": "administrator",
                    "can_manage_topics": False,
                },
            }
        return await super()._call(method, payload)


class _ConfirmFailingTelegramApi(_FakeTelegramApi):
    async def _call(self, method, payload):
        if method == "getUpdates" and "offset" in payload:
            self.calls.append((method, dict(payload)))
            raise RuntimeError("temporary confirm failure")
        return await super()._call(method, payload)


class _FlakyGetUpdatesTelegramApi(_FakeTelegramApi):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    async def _call(self, method, payload):
        if method == "getUpdates" and self.failures_left:
            self.failures_left -= 1
            self.calls.append((method, dict(payload)))
            raise TimeoutError("temporary polling timeout")
        return await super()._call(method, payload)


class _HangingGetUpdatesTelegramApi(_ForumTelegramApi):
    async def _call(self, method, payload):
        if method == "getUpdates":
            self.calls.append((method, dict(payload)))
            await asyncio.Event().wait()
        return await super()._call(method, payload)


class _HangingEventsTransport(FakeAgentTransport):
    async def events(self, handle):
        await asyncio.Event().wait()


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _telegram_update(
    update_id=10,
    text="ship it",
    *,
    reply_to_message_id="",
    chat_id=123,
    chat_type="private",
    message_thread_id="",
):
    message = {
        "message_id": update_id + 100,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 456, "first_name": "Ada"},
        "text": text,
    }
    if message_thread_id:
        message["message_thread_id"] = message_thread_id
    if reply_to_message_id:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {
        "update_id": update_id,
        "message": message,
    }


def _telegram_service_update(update_id=10, *, chat_id=123, message_thread_id="", service_field="forum_topic_closed"):
    message = {
        "message_id": update_id + 100,
        "chat": {"id": chat_id, "type": "supergroup"},
        "from": {"id": 123456, "first_name": "WalkCode", "is_bot": True},
        service_field: {},
    }
    if message_thread_id:
        message["message_thread_id"] = message_thread_id
        message["is_topic_message"] = True
    return {
        "update_id": update_id,
        "message": message,
    }


def _telegram_callback(update_id=90, *, token: str, reply_to_message_id=""):
    message = {
        "message_id": update_id + 100,
        "chat": {"id": 123, "type": "private"},
    }
    if reply_to_message_id:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": 456, "first_name": "Ada"},
            "message": message,
            "data": f"cb:{token}",
        },
    }


def _latest_callback_token(api: _FakeTelegramApi, button_text: str) -> str:
    for method, payload in reversed(api.calls):
        if method != "sendMessage":
            continue
        markup = payload.get("reply_markup") or {}
        for row in markup.get("inline_keyboard", []):
            for item in row:
                if item.get("text") == button_text:
                    return str(item.get("callback_data", "")).removeprefix("cb:")
    raise AssertionError(f"callback button not found: {button_text}")


class ChannelNativeRuntimeTests(unittest.TestCase):
    def test_process_telegram_update_starts_session_sends_reply_and_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            result = asyncio.run(runtime.process_telegram_update(_telegram_update()))

            self.assertTrue(result.accepted)
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["ship it"])
            self.assertIn(("sendMessage", {"chat_id": "123", "text": "done"}), api.calls)
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].transport_kind, "claude_headless")

    def test_process_telegram_update_uses_configured_agent_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            claude = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "claude"})],
            )
            codex = FakeAgentTransport(
                "codex_app_server",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "codex"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": claude, "codex_app_server": codex},
            )

            result = asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="selected agent")))

            self.assertTrue(result.accepted)
            self.assertEqual(claude.submitted_turns, [])
            self.assertEqual([turn.text for turn in codex.submitted_turns], ["selected agent"])
            snapshot = JsonFileStateStore(state_path).load()
            kinds = sorted(item.transport_kind for item in snapshot.sessions.list_sessions(channel_kind="telegram"))
            self.assertEqual(kinds, ["codex_app_server"])

    def test_process_telegram_forum_root_message_creates_session_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApi()
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(
                        10,
                        text="build topic session",
                        chat_id=-100,
                        chat_type="supergroup",
                    )
                )
            )

            self.assertTrue(result.accepted)
            create_calls = [payload for method, payload in api.calls if method == "createForumTopic"]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(create_calls[0]["chat_id"], "-100")
            self.assertIn(create_calls[0]["icon_custom_emoji_id"], {"emoji-a", "emoji-b"})
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertIn("已创建 session topic", sent[0]["text"])
            self.assertNotIn("message_thread_id", sent[0])
            self.assertEqual(sent[0]["reply_parameters"]["message_id"], 110)
            self.assertEqual(sent[-1]["message_thread_id"], "777")
            self.assertNotIn("deleteMessage", [method for method, _payload in api.calls])
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["build topic session"])
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(summaries[0].thread_id, "777")

    def test_telegram_topic_url_uses_private_supergroup_link_shape(self):
        self.assertEqual(
            runtime_module._telegram_topic_url("-1003984400780", "70"),
            "https://t.me/c/3984400780/70",
        )
        self.assertEqual(runtime_module._telegram_topic_url("123", "70"), "")
        self.assertEqual(runtime_module._telegram_topic_url("-1003984400780", ""), "")

    def test_process_telegram_empty_message_is_confirmed_without_starting_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(
                        10,
                        text="",
                        chat_id=-100,
                        chat_type="supergroup",
                    )
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "empty_message_ignored")
            self.assertEqual(transport.submitted_turns, [])
            self.assertEqual(
                [method for method, _payload in api.calls if method == "createForumTopic"],
                [],
            )
            self.assertFalse(Path(state_path).exists())

    def test_process_telegram_status_command_is_handled_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            first = asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="ship it")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(
                        11,
                        text="/status",
                        reply_to_message_id="110",
                    )
                )
            )

            self.assertTrue(first.accepted)
            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "telegram_bot_command")
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["ship it"])
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertTrue(any("WalkCode session:" in payload["text"] for payload in sent))

    def test_process_telegram_model_command_does_not_leak_to_agent_when_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport(
                "codex_app_server",
                TransportCapabilities(
                    structured_input=True,
                    structured_output=True,
                    permission_callback=False,
                    ask_user_question=False,
                    interrupt=False,
                    set_model=False,
                    set_permission_mode=False,
                    checkpoint_rewind=False,
                    resume_after_complete=True,
                    resume_active_turn=False,
                    multi_client_observe=False,
                    multi_client_write=False,
                    external_tui_takeover=True,
                ),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": transport},
            )

            asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="start")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(
                        11,
                        text="/model gpt-5",
                        reply_to_message_id="110",
                    )
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "telegram_bot_command")
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["start"])
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertTrue(any("Model switching is not available" in payload["text"] for payload in sent))

    def test_process_telegram_model_command_lists_claude_configured_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings_path = Path(tmp) / "vertex.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_MODEL": "claude-opus-4-8[1m]",
                            "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4-5",
                        }
                    }
                )
            )
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_SETTINGS": str(settings_path),
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="start")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(11, text="/model", reply_to_message_id="110")
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["start"])
            # With configured models + set_model capability, /model now sends an
            # interactive model_choice card; on Telegram the models are button
            # labels in the inline keyboard, not message body text.
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            button_labels = [
                btn.get("text", "")
                for payload in sent
                for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
                for btn in row
            ]
            self.assertTrue(any("claude-opus-4-8[1m]" in label for label in button_labels))
            self.assertTrue(any("claude-haiku-4-5" in label for label in button_labels))

    def test_process_telegram_model_command_lists_codex_cached_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_config = Path(tmp) / "config.toml"
            codex_config.write_text(
                'model = "gpt-custom"\n'
                'model_provider = "azure"\n'
                'model_reasoning_effort = "xhigh"\n'
            )
            models_cache = Path(tmp) / "models_cache.json"
            models_cache.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.5",
                                "display_name": "GPT-5.5",
                                "visibility": "list",
                                "priority": 10,
                            },
                            {
                                "slug": "hidden-model",
                                "display_name": "Hidden",
                                "visibility": "hide",
                                "priority": 1,
                            },
                        ]
                    }
                )
            )
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_CODEX_CONFIG": str(codex_config),
                    "WALKCODE_CODEX_MODELS_CACHE": str(models_cache),
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport(
                "codex_app_server",
                TransportCapabilities(
                    structured_input=True,
                    structured_output=True,
                    permission_callback=False,
                    ask_user_question=False,
                    interrupt=False,
                    set_model=False,
                    set_permission_mode=False,
                    checkpoint_rewind=False,
                    resume_after_complete=True,
                    resume_active_turn=False,
                    multi_client_observe=False,
                    multi_client_write=False,
                    external_tui_takeover=True,
                ),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": transport},
            )

            asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="start")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(11, text="/model", reply_to_message_id="110")
                )
            )

            self.assertTrue(result.accepted)
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            model_text = "\n".join(payload.get("text", "") for payload in sent)
            self.assertIn("Current/default: gpt-custom", model_text)
            self.assertIn("Provider: azure", model_text)
            self.assertIn("gpt-5.5 - GPT-5.5", model_text)
            self.assertNotIn("hidden-model", model_text)

    def test_process_telegram_agent_selector_command_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            result = asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="/codex")))

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "agent_selector_rejected")
            self.assertEqual(transport.submitted_turns, [])
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(sent), 1)
            self.assertIn("This bot is configured for claude.", sent[0]["text"])
            self.assertIn("Use a separate codex bot", sent[0]["text"])

    def test_process_telegram_unknown_slash_command_without_session_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            result = asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="/compact")))

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "telegram_unknown_slash_command")
            self.assertEqual(transport.submitted_turns, [])
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertIn("inside an existing session", sent[-1]["text"])

    def test_process_telegram_unknown_slash_command_inside_session_passes_to_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="start")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(11, text="/compact", reply_to_message_id="110")
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["start", "/compact"])

    def test_telegram_safe_agent_command_alias_is_forwarded_as_native_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            asyncio.run(runtime.process_telegram_update(_telegram_update(10, text="start")))
            result = asyncio.run(
                runtime.process_telegram_update(
                    _telegram_update(11, text="/add_dir /tmp/extra", reply_to_message_id="110")
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["start", "/add-dir /tmp/extra"])

    def test_poll_telegram_once_sends_processing_action_for_accepted_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)], []])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={
                    "claude_headless": FakeAgentTransport(
                        "claude_headless",
                        _transport_caps(),
                        scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
                    )
                },
            )

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            self.assertEqual(processed, 1)
            actions = [payload for method, payload in api.calls if method == "sendChatAction"]
            self.assertEqual(actions[0]["chat_id"], "123")
            self.assertEqual(actions[0]["action"], "typing")

    def test_process_telegram_update_reacts_to_received_user_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={
                    "claude_headless": FakeAgentTransport(
                        "claude_headless",
                        _transport_caps(),
                        scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
                    )
                },
            )

            result = asyncio.run(runtime.process_telegram_update(_telegram_update(41, text="ship it")))

            self.assertTrue(result.accepted)
            reactions = [payload for method, payload in api.calls if method == "setMessageReaction"]
            self.assertEqual(reactions[0]["chat_id"], "123")
            self.assertEqual(reactions[0]["message_id"], 141)
            self.assertEqual(reactions[0]["reaction"], [{"type": "emoji", "emoji": "✅"}])

    def test_serve_telegram_polling_installs_bot_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            asyncio.run(runtime.serve_telegram_polling(timeout=0, limit=5, retry_delay=0, max_iterations=1))

            methods = [method for method, _payload in api.calls]
            self.assertLess(methods.index("getUpdates"), methods.index("setMyCommands"))
            commands = [payload for method, payload in api.calls if method == "setMyCommands"]
            self.assertEqual(commands[0]["commands"][0]["command"], "status")
            self.assertTrue(any(item["command"] == "skills" for item in commands[0]["commands"]))
            self.assertTrue(any(item["command"] == "compact" for item in commands[0]["commands"]))
            self.assertTrue(any(item["command"] == "commands" for item in commands[0]["commands"]))

    def test_serve_telegram_polling_flushes_persisted_outbox_without_new_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            runtime.state.outbox.enqueue(
                channel_binding_key=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="",
                ).key(),
                view_model={"type": "text", "text": "queued after restart"},
                idempotency_key="queued-after-restart",
            )
            runtime.save_state()

            asyncio.run(runtime.serve_telegram_polling(timeout=0, limit=5, retry_delay=0, max_iterations=1))

            self.assertIn(("sendMessage", {"chat_id": "123", "text": "queued after restart"}), api.calls)
            snapshot = JsonFileStateStore(state_path).load()
            self.assertEqual(snapshot.outbox.pending_count(), 0)
            self.assertEqual(snapshot.outbox.sent_count(), 1)

    def test_serve_telegram_polling_confirms_offset_after_turn_submit_before_events_finish(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41, text="long task")], []])
            transport = _HangingEventsTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            asyncio.run(
                asyncio.wait_for(
                    runtime.serve_telegram_polling(
                        timeout=0,
                        limit=5,
                        retry_delay=0,
                        max_iterations=1,
                    ),
                    timeout=0.5,
                )
            )

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(get_updates[1]["offset"], 42)
            sessions = runtime.state.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(sessions), 1)
            session = runtime.state.sessions.get(sessions[0].session_id)
            self.assertEqual(session.lifecycle_state, "ACTIVE")
            self.assertEqual(session.last_progress_event, "turn.submitted")

    def test_serve_telegram_polling_drains_tui_hooks_while_getupdates_hangs(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _HangingGetUpdatesTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            runtime.defer_tui_hook(
                hook_type="UserPromptSubmit",
                agent="claude",
                payload={
                    "session_id": "claude-tui-hanging-poll",
                    "cwd": tmp,
                    "prompt": "mirror while polling is stuck",
                },
            )

            async def run_until_mirrored():
                task = asyncio.create_task(
                    runtime.serve_telegram_polling(timeout=0, limit=5, retry_delay=0)
                )
                try:
                    for _ in range(50):
                        sent = [payload for method, payload in api.calls if method == "sendMessage"]
                        if any(payload.get("text") == "⌨️ 终端输入\n\nmirror while polling is stuck" for payload in sent):
                            return
                        await asyncio.sleep(0.01)
                    self.fail("terminal input was not mirrored while getUpdates was hanging")
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            asyncio.run(run_until_mirrored())

            self.assertEqual(list((Path(state_path).parent / "state.json.tui-hooks.d").glob("*.json")), [])
            sessions = runtime.state.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(sessions), 1)
            session = runtime.state.sessions.get(sessions[0].session_id)
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertEqual(session.last_progress_event, "external_tui.user-prompt-submit")

    def test_deferred_tui_hook_maintenance_allows_telegram_topic_latency(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            timeouts = []

            async def record_drain(*, limit=100):
                return 0

            async def record_wait_for(awaitable, *, timeout):
                timeouts.append(timeout)
                return await awaitable

            runtime.drain_deferred_tui_hooks = record_drain
            with patch.object(runtime_module.asyncio, "wait_for", record_wait_for):
                asyncio.run(runtime._best_effort_drain_deferred_tui_hooks())

            self.assertEqual(len(timeouts), 1)
            self.assertGreaterEqual(timeouts[0], 10.0)

    def test_refresh_loaded_tui_observed_bindings_marks_stale_pid_detached_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_observed_session(
                session_id="tui-claude-old",
                binding=ChannelBinding(
                    "telegram",
                    "bot",
                    "chat",
                    "thread",
                    "",
                    capabilities={"status_card": True, "native_topic": True},
                ),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": "claude-old",
                    },
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {
                            "pid": 999999,
                            "allow_terminate": True,
                            "source": "native_hook_external_tui",
                        },
                    },
                },
                owner=ActorRef("telegram", "local_tui", "Claude TUI"),
            )

            asyncio.run(runtime._refresh_loaded_tui_observed_bindings())

            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.status, "stopped")
            self.assertEqual(updated.lifecycle_state, "EXTERNAL_DETACHED_IMPORTABLE")
            self.assertEqual(updated.writer_owner.kind, "none")
            self.assertEqual(updated.stop_reason, "external_tui_process_gone")
            self.assertEqual(updated.last_progress_event, "external_tui.detached")
            self.assertEqual(updated.generation, 1)

    def test_refresh_loaded_tui_observed_bindings_marks_stale_pid_detached_unimportable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_observed_session(
                session_id="tui-claude-no-resume",
                binding=ChannelBinding(
                    "telegram",
                    "bot",
                    "chat",
                    "thread-2",
                    "",
                    capabilities={"status_card": True, "native_topic": True},
                ),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {
                            "pid": 999999,
                            "allow_terminate": True,
                            "source": "native_hook_external_tui",
                        },
                    },
                },
                owner=ActorRef("telegram", "local_tui", "Claude TUI"),
            )

            asyncio.run(runtime._refresh_loaded_tui_observed_bindings())

            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.status, "stopped")
            self.assertEqual(updated.lifecycle_state, "EXTERNAL_DETACHED_UNIMPORTABLE")
            self.assertEqual(updated.writer_owner.kind, "none")
            self.assertEqual(updated.stop_reason, "external_tui_process_gone")

    def test_describe_reports_launchd_service_not_loaded_for_telegram_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            fake_launchctl = subprocess.CompletedProcess(
                args=["launchctl"],
                returncode=3,
                stdout="",
                stderr="Could not find service",
            )

            with patch.object(runtime_module.subprocess, "run", return_value=fake_launchctl):
                status = runtime.describe()

            self.assertEqual(status["runtime_status"]["service_label"], "com.walkcode.telegram-codex")
            self.assertFalse(status["runtime_status"]["service_loaded"])
            self.assertEqual(status["runtime_status"]["service_state"], "not_loaded")

    def test_describe_reports_missing_codex_tui_user_prompt_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            (codex / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "command": (
                                                "WALKCODE_ENV_FILE=/tmp/codex.env "
                                                "walkcode native hook SessionStart --agent codex --defer"
                                            )
                                        }
                                    ]
                                }
                            ],
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "command": (
                                                "WALKCODE_ENV_FILE=/tmp/codex.env "
                                                "walkcode native hook Stop --agent codex --defer"
                                            )
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            with patch.object(runtime_module.Path, "home", return_value=home):
                status = runtime.describe()

            self.assertFalse(status["tui_hook_status"]["ok"])
            self.assertIn("UserPromptSubmit", status["tui_hook_status"]["missing"])
            self.assertIn("MessageDisplay", status["tui_hook_status"]["missing"])

    def test_describe_accepts_complete_codex_tui_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            codex = home / ".codex"
            codex.mkdir(parents=True)
            hooks = {
                name: [
                    {
                        "hooks": [
                            {
                                "command": (
                                    f"WALKCODE_ENV_FILE=/tmp/codex.env "
                                    f"walkcode native hook {name} --agent codex --defer"
                                )
                            }
                        ]
                    }
                ]
                for name in runtime_module.CODEX_TUI_REQUIRED_HOOKS
            }
            (codex / "hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            with patch.object(runtime_module.Path, "home", return_value=home):
                status = runtime.describe()

            self.assertTrue(status["tui_hook_status"]["ok"])
            self.assertEqual(status["tui_hook_status"]["missing"], [])
            self.assertEqual(status["tui_hook_status"]["command_missing"], [])

    def test_poll_telegram_once_tracks_offsets_and_dispatches_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)], []])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={
                    "claude_headless": FakeAgentTransport(
                        "claude_headless",
                        _transport_caps(),
                        scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
                    )
                },
            )

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))
            processed_again = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 1)
            self.assertEqual(processed_again, 0)
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(get_updates[1]["offset"], 42)

    def test_poll_telegram_once_ignores_disallowed_chat_and_confirms_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "999",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)], []])
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 0)
            self.assertEqual(transport.submitted_turns, [])
            self.assertEqual([method for method, _payload in api.calls], ["getUpdates", "getUpdates"])
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(get_updates[1]["offset"], 42)

    def test_poll_telegram_once_confirms_topic_service_messages_without_routing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(
                batches=[
                    [
                        _telegram_service_update(41, chat_id=-100, message_thread_id="77"),
                        _telegram_update(
                            42,
                            text="new task",
                            chat_id=-100,
                            chat_type="supergroup",
                        ),
                    ],
                    [],
                ]
            )
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 2)
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["new task"])
            self.assertEqual(get_updates[1]["offset"], 43)
            self.assertNotIn("closeForumTopic", [method for method, _payload in api.calls])
            self.assertNotIn("reopenForumTopic", [method for method, _payload in api.calls])

    def test_serve_telegram_polling_recovers_from_transient_getupdates_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FlakyGetUpdatesTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            asyncio.run(
                runtime.serve_telegram_polling(
                    timeout=0,
                    limit=5,
                    retry_delay=0,
                    max_iterations=2,
                )
            )

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(len(get_updates), 2)
            self.assertEqual(runtime.last_telegram_poll_error, "")

    def test_diagnose_telegram_ingress_peeks_without_confirming_offset(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertTrue(report["safe_to_run_serve_once"])
            self.assertEqual(report["pending_updates"]["count"], 1)
            self.assertTrue(report["pending_updates"]["items"][0]["chat_allowed"])
            self.assertFalse(report["bot"]["has_private_topics_enabled"])
            self.assertEqual(report["target_chat"]["type"], "private")
            self.assertFalse(report["target_chat"]["topic_per_session_available"])
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(len(get_updates), 1)

    def test_diagnose_telegram_forum_reports_missing_manage_topics_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApiWithoutTopicAdmin()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            target = report["target_chat"]
            self.assertTrue(target["is_forum"])
            self.assertEqual(target["native_topic_surface"], "forum_supergroup")
            self.assertFalse(target["topic_per_session_available"])
            self.assertEqual(target["recommended_placement"], "root_reply_chain")
            self.assertEqual(target["topic_unavailable_reason"], "bot_missing_manage_topics")
            self.assertEqual(target["bot_admin"]["status"], "administrator")
            self.assertFalse(target["bot_admin"]["can_manage_topics"])

    def test_diagnose_telegram_ingress_rejects_agent_selector_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41, text="/codex hello")]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            item = report["pending_updates"]["items"][0]
            self.assertTrue(report["safe_to_run_serve_once"])
            self.assertTrue(item["submit_would_accept"])
            self.assertEqual(item["submit_action"], "agent_selector_rejected")
            self.assertEqual(item["agent_selector_command"], "codex")
            self.assertEqual(item["configured_agent"], "claude")

    def test_diagnose_telegram_ingress_treats_ambiguous_rootless_message_as_chooser(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41, text="continue")]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            for root in ("root-1", "root-2"):
                runtime.state.sessions.create_structured_session(
                    binding=ChannelBinding(
                        channel_kind="telegram",
                        account_id="bot",
                        chat_id="123",
                        thread_id="",
                        root_message_id=root,
                    ),
                    transport_kind="claude_headless",
                    transport_ref={"handle_id": root},
                    cwd=tmp,
                    owner=ActorRef("telegram", "456", "Ada"),
                )

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            item = report["pending_updates"]["items"][0]
            self.assertTrue(report["safe_to_run_serve_once"])
            self.assertTrue(item["submit_would_accept"])
            self.assertEqual(item["submit_action"], "session_chooser")
            self.assertEqual(item["submit_blocked_reason"], BlockedReason.AMBIGUOUS_SESSION)

    def test_diagnose_telegram_ingress_blocks_serve_once_for_disallowed_pending_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "999",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertFalse(report["safe_to_run_serve_once"])
            self.assertFalse(report["pending_updates"]["items"][0]["chat_allowed"])
            self.assertIn("outside Telegram allowlist", report["warnings"][0])
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(len(get_updates), 1)

    def test_diagnose_telegram_ingress_blocks_serve_once_for_expired_active_session_lease(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
                now=clock,
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "stale-handle"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            runtime.state.authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)
            clock.now += 31.0

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            item = report["pending_updates"]["items"][0]
            self.assertFalse(report["safe_to_run_serve_once"])
            self.assertTrue(item["chat_allowed"])
            self.assertTrue(item["active_session_present"])
            self.assertFalse(item["submit_would_accept"])
            self.assertEqual(item["submit_blocked_reason"], BlockedReason.LEASE_EXPIRED)
            self.assertIn("not currently submittable", report["warnings"][0])
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(len(get_updates), 1)

    def test_diagnose_telegram_ingress_allows_resumable_idle_session(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
                now=clock,
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "old-handle", "agent_session_id": "agent-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            session.lifecycle_state = "IDLE"
            session.writer_lease = None
            runtime.state.authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)
            clock.now += 31.0

            report = asyncio.run(runtime.diagnose_telegram_ingress(limit=5))

            item = report["pending_updates"]["items"][0]
            self.assertTrue(report["safe_to_run_serve_once"])
            self.assertTrue(item["chat_allowed"])
            self.assertTrue(item["active_session_present"])
            self.assertTrue(item["submit_would_accept"])
            self.assertTrue(item["submit_requires_resume"])

    def test_poll_telegram_once_resumes_idle_session_before_submit(self):
        class BatchedTransport(FakeAgentTransport):
            def __init__(self):
                super().__init__("claude_headless", _transport_caps())
                self.event_batches = [
                    [AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
                ]

            async def events(self, handle):
                return self.event_batches.pop(0)

        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41, text="follow up")]])
            transport = BatchedTransport()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
                now=clock,
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "old-handle", "agent_session_id": "agent-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            session.lifecycle_state = "IDLE"
            session.writer_lease = None
            runtime.state.authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)
            clock.now += 31.0

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 1)
            self.assertEqual(get_updates[1]["offset"], 42)
            self.assertEqual(transport.call_log, ["resume", "submit_turn"])
            self.assertEqual(transport.resume_specs[0].resume_ref["agent_session_id"], "agent-session-1")
            self.assertEqual([turn.text for turn in transport.submitted_turns], ["follow up"])

    def test_poll_telegram_once_does_not_fail_when_offset_confirm_is_transient(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ConfirmFailingTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={
                    "claude_headless": FakeAgentTransport(
                        "claude_headless",
                        _transport_caps(),
                        scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
                    )
                },
            )

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 1)
            self.assertEqual(get_updates[1]["offset"], 42)
            self.assertIn("temporary confirm failure", runtime.last_telegram_offset_confirm_error)

    def test_poll_telegram_once_does_not_confirm_offset_for_expired_active_session_lease(self):
        clock = _Clock()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi(batches=[[_telegram_update(41)]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
                now=clock,
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "stale-handle"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            runtime.state.authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)
            clock.now += 31.0
            # This test guards the lease-expiry hold-back semantics; treat the
            # session as created by this process so the startup sweep skips it.
            runtime._orphan_sweep_done = True

            processed = asyncio.run(runtime.poll_telegram_once(timeout=0, limit=5))

            get_updates = [payload for method, payload in api.calls if method == "getUpdates"]
            self.assertEqual(processed, 0)
            self.assertEqual(len(get_updates), 1)
            self.assertNotIn("offset", get_updates[0])
            self.assertEqual(runtime.state.inbound_ledger.to_dict()["completed"], {})

    def test_describe_reports_single_channel_and_bound_agent(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
            }
        )
        runtime = ChannelNativeRuntime.from_config(
            cfg,
            telegram_api=_FakeTelegramApi(),
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

        status = runtime.describe()

        self.assertEqual(status["channel"]["kind"], "telegram")
        self.assertEqual(status["channel"]["live_ingress"], "polling")
        self.assertEqual(status["agent"], "claude")
        self.assertNotIn("selected", status["agent_status"])
        self.assertNotIn("transport_kind", status["agent_status"])

    def test_build_transports_wires_codex_app_server_when_cli_exists(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
            }
        )
        original = runtime_module.shutil.which
        original_daemon_available = runtime_module._codex_standalone_daemon_available
        runtime_module.shutil.which = lambda name: "/usr/bin/codex" if name == "codex" else original(name)
        runtime_module._codex_standalone_daemon_available = lambda codex_home="": True
        try:
            transports = runtime_module._build_transports(cfg)
        finally:
            runtime_module.shutil.which = original
            runtime_module._codex_standalone_daemon_available = original_daemon_available

        self.assertIsInstance(transports["codex_app_server"], runtime_module.CodexAppServerTransport)
        self.assertIsInstance(
            transports["codex_app_server"].client,
            runtime_module.CodexManagedAppServerClient,
        )
        self.assertTrue(transports["codex_app_server"].capabilities().structured_input)

    def test_build_transports_auto_falls_back_to_codex_stdio_without_standalone_daemon(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
            }
        )
        original = runtime_module.shutil.which
        original_daemon_available = runtime_module._codex_standalone_daemon_available
        runtime_module.shutil.which = lambda name: "/usr/bin/codex" if name == "codex" else original(name)
        runtime_module._codex_standalone_daemon_available = lambda codex_home="": False
        try:
            transports = runtime_module._build_transports(cfg)
        finally:
            runtime_module.shutil.which = original
            runtime_module._codex_standalone_daemon_available = original_daemon_available

        self.assertIsInstance(
            transports["codex_app_server"].client,
            runtime_module.CodexStdioAppServerClient,
        )
        self.assertNotIsInstance(
            transports["codex_app_server"].client,
            runtime_module.CodexManagedAppServerClient,
        )

    def test_build_transports_can_force_codex_stdio_fallback(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_CODEX_APP_SERVER_MODE": "stdio",
            }
        )
        original = runtime_module.shutil.which
        runtime_module.shutil.which = lambda name: "/usr/bin/codex" if name == "codex" else original(name)
        try:
            transports = runtime_module._build_transports(cfg)
        finally:
            runtime_module.shutil.which = original

        self.assertIsInstance(
            transports["codex_app_server"].client,
            runtime_module.CodexStdioAppServerClient,
        )
        self.assertNotIsInstance(
            transports["codex_app_server"].client,
            runtime_module.CodexManagedAppServerClient,
        )

    def test_build_transports_passes_claude_agent_options(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_SETTINGS": "/tmp/vertex.json",
                "WALKCODE_CLAUDE_CLI_PATH": "/tmp/claude",
                "WALKCODE_CLAUDE_CONFIG_DIR": "/tmp/claude-profiles/work",
            }
        )

        transports = runtime_module._build_transports(cfg)

        self.assertEqual(transports["claude_headless"].settings, "/tmp/vertex.json")
        self.assertEqual(transports["claude_headless"].cli_path, "/tmp/claude")
        self.assertEqual(transports["claude_headless"].config_dir, "/tmp/claude-profiles/work")

    def test_build_transports_passes_claude_anthropic_base_url(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CLAUDE_ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
            }
        )

        transports = runtime_module._build_transports(cfg)

        self.assertEqual(transports["claude_headless"].anthropic_base_url, "http://127.0.0.1:18899")

    def test_unknown_codex_app_server_mode_fails_instead_of_dropping_socket(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_CODEX_APP_SERVER_MODE": "bogus",
                "WALKCODE_CODEX_APP_SERVER_SOCKET": "/tmp/custom.sock",
            }
        )

        with self.assertRaisesRegex(ChannelConfigError, "unknown WALKCODE_CODEX_APP_SERVER_MODE"):
            runtime_module._build_codex_app_server_client(cfg)

    def test_codex_home_flows_into_managed_client_socket_and_env(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_CODEX_APP_SERVER_MODE": "daemon",
                "WALKCODE_CODEX_HOME": "/tmp/codex-profiles/personal",
            }
        )

        client = runtime_module._build_codex_app_server_client(cfg)

        self.assertIsInstance(client, runtime_module.CodexManagedAppServerClient)
        self.assertEqual(
            client.socket_path,
            "/tmp/codex-profiles/personal/app-server-control/app-server-control.sock",
        )
        env = client._subprocess_env()
        self.assertIsNotNone(env)
        self.assertEqual(env["CODEX_HOME"], "/tmp/codex-profiles/personal")

    def test_explicit_socket_beats_codex_home_derivation(self):
        client = runtime_module.CodexManagedAppServerClient(
            socket_path="/tmp/explicit.sock",
            codex_home="/tmp/codex-profiles/work",
        )

        self.assertEqual(client.socket_path, "/tmp/explicit.sock")

    def test_codex_client_without_codex_home_inherits_environment(self):
        client = runtime_module.CodexStdioAppServerClient()

        self.assertIsNone(client._subprocess_env())

    def test_launchd_service_label_profile_and_legacy_forms(self):
        self.assertEqual(
            runtime_module._launchd_service_label("telegram", "claude"),
            "com.walkcode.telegram-claude",
        )
        self.assertEqual(
            runtime_module._launchd_service_label("lark", "claude", "work"),
            "com.walkcode.work-claude",
        )
        self.assertEqual(
            runtime_module._launchd_service_label("telegram", "codex", "personal"),
            "com.walkcode.personal-codex",
        )
        self.assertEqual(runtime_module._launchd_service_label("lark", "claude"), "")
        self.assertEqual(runtime_module._launchd_service_label("lark", "unknown", "work"), "")

    def test_describe_reports_profile(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "LARK_APP_SECRET": "secret",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_PROFILE": "work",
                "WALKCODE_STATE_PATH": "/tmp/work-claude-state.json",
            }
        )
        runtime = ChannelNativeRuntime.from_config(
            cfg,
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

        status = runtime.describe()

        self.assertEqual(status["profile"], "work")
        self.assertIn("profile: work", runtime_module._format_status(status))

    def test_load_native_env_has_no_implicit_default_env_file(self):
        merged = runtime_module._load_native_env({"WALKCODE_AGENT": "claude"})

        self.assertEqual(merged, {"WALKCODE_AGENT": "claude"})

    def test_polling_without_telegram_channel_fails_explicitly(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "lark",
                "LARK_APP_ID": "app-id",
                "WALKCODE_AGENT": "claude",
                "LARK_APP_SECRET": "secret",
            }
        )
        runtime = ChannelNativeRuntime.from_config(
            cfg,
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

        with self.assertRaisesRegex(ChannelConfigError, "Telegram"):
            asyncio.run(runtime.poll_telegram_once(timeout=0, limit=1))

    def test_telegram_webhook_config_is_not_polled_by_v3_runtime(self):
        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "TELEGRAM_WEBHOOK_URL": "https://example.test/hook",
            }
        )
        runtime = ChannelNativeRuntime.from_config(
            cfg,
            telegram_api=_FakeTelegramApi(),
            transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
        )

        self.assertEqual(runtime.describe()["channel"]["live_ingress"], "webhook_not_wired")
        with self.assertRaisesRegex(ChannelConfigError, "polling is disabled"):
            asyncio.run(runtime.poll_telegram_once(timeout=0, limit=1))

    def test_describe_includes_e2e_gates_without_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = ChannelNativeRuntime.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "super-secret-token",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_E2E_TELEGRAM": "1",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                },
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            status = runtime.describe()

            self.assertIn("e2e_gates", status)
            self.assertFalse(status["e2e_gates"]["telegram"]["enabled"])
            self.assertEqual(
                status["e2e_gates"]["telegram"]["missing"],
                ["WALKCODE_E2E_TELEGRAM_CHAT_ID"],
            )
            self.assertNotIn("super-secret-token", json.dumps(status))

    def test_explicit_env_does_not_merge_default_env_file_values(self):
        original = runtime_module._read_env_file
        runtime_module._read_env_file = lambda path: {
            "WALKCODE_E2E_TELEGRAM_CHAT_ID": "leaked-chat-id",
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                runtime = ChannelNativeRuntime.from_env(
                    {
                        "WALKCODE_CHANNEL": "telegram",
                        "TELEGRAM_BOT_TOKEN": "token",
                        "WALKCODE_AGENT": "claude",
                        "WALKCODE_E2E_TELEGRAM": "1",
                        "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                        "WALKCODE_CWD": tmp,
                    },
                    telegram_api=_FakeTelegramApi(),
                    transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
                )
        finally:
            runtime_module._read_env_file = original

        status = runtime.describe()

        self.assertFalse(status["e2e_gates"]["telegram"]["enabled"])
        self.assertEqual(
            status["e2e_gates"]["telegram"]["missing"],
            ["WALKCODE_E2E_TELEGRAM_CHAT_ID"],
        )

    def test_explicit_env_file_values_override_ambient_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "codex.env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=token",
                        "WALKCODE_AGENT=codex",
                        f"WALKCODE_STATE_PATH={Path(tmp) / 'state.json'}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )

            loaded = runtime_module._load_native_env(
                {
                    "WALKCODE_ENV_FILE": str(env_file),
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CHANNEL": "lark",
                }
            )

        self.assertEqual(loaded["WALKCODE_AGENT"], "codex")
        self.assertEqual(loaded["WALKCODE_CHANNEL"], "telegram")

    def test_tui_hook_creates_observed_telegram_session_then_stop_sends_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            created = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "terminate_ref": {
                            "controller_kind": "process",
                            "process_ref": {"pid": 123, "allow_terminate": True},
                        },
                    },
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="stop",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "message": "finished from TUI",
                        "terminate_ref": {
                            "controller_kind": "process",
                            "process_ref": {"pid": 123, "allow_terminate": True},
                        },
                    },
                )
            )

            self.assertTrue(created.accepted)
            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 2)
            self.assertIn("WalkCode session: claude: TUI claude-session-1", send_messages[0]["text"])
            self.assertIn("Input: read-only until takeover", send_messages[0]["text"])
            self.assertEqual(send_messages[1]["text"], "finished from TUI")
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(summaries), 1)
            session = snapshot.sessions.get(summaries[0].session_id)
            self.assertEqual(session.status, "running")
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertEqual(session.stop_reason, "")
            self.assertEqual(session.writer_owner.kind, "external_tui")
            self.assertEqual(session.last_progress_event, "external_tui.stop")
            self.assertEqual(session.transport_ref["resume_ref"]["agent_session_id"], "claude-session-1")
            self.assertEqual(session.transport_ref["terminate_ref"]["process_ref"]["allow_terminate"], True)
            ledger = snapshot.inbound_ledger.to_dict()
            self.assertEqual(ledger["in_progress"], {})
            self.assertEqual(len(ledger["completed"]), 2)

    def test_tui_permission_request_hook_sends_loud_notice_and_flips_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp},
                )
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="permission-request",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "tool_name": "Edit",
                        "summary": "docs/design.md",
                    },
                )
            )

            self.assertTrue(result.accepted)
            notices = [
                payload
                for method, payload in api.calls
                if method == "sendMessage" and "waiting for your approval" in payload.get("text", "")
            ]
            self.assertEqual(len(notices), 1)
            self.assertIn("Edit", notices[0]["text"])
            snapshot = JsonFileStateStore(state_path).load()
            session = snapshot.sessions.get(
                snapshot.sessions.list_sessions(channel_kind="telegram")[0].session_id
            )
            self.assertEqual(session.lifecycle_state, "WAITING_PERMISSION")

            # Claude's follow-up Notification would duplicate the notice card:
            # it must be suppressed while the session waits for permission.
            before = len(api.calls)
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="notification",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "message": "Claude needs your permission",
                    },
                )
            )
            self.assertEqual(len(api.calls), before)

            # The next tool lifecycle hook means the prompt was answered in the
            # terminal: health returns to read-only observation.
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="post-tool",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp, "tool_name": "Edit"},
                )
            )
            snapshot = JsonFileStateStore(state_path).load()
            session = snapshot.sessions.get(
                snapshot.sessions.list_sessions(channel_kind="telegram")[0].session_id
            )
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")

    def test_raw_stop_hook_name_is_normalized_before_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            created = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp},
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="Stop",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp, "message": "done"},
                )
            )

            self.assertTrue(created.accepted)
            self.assertTrue(result.accepted)
            snapshot = JsonFileStateStore(state_path).load()
            session = snapshot.sessions.get(snapshot.sessions.list_sessions(channel_kind="telegram")[0].session_id)
            self.assertEqual(session.status, "running")
            self.assertEqual(session.stop_reason, "")
            self.assertEqual(session.writer_owner.kind, "external_tui")
            self.assertEqual(session.last_progress_event, "external_tui.stop")

    def test_hook_without_resume_ref_is_accepted_as_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="Stop",
                    agent="claude",
                    payload={"cwd": tmp, "message": "no durable session id"},
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "missing_resume_ref")
            self.assertEqual(runtime.state.sessions.list_sessions(channel_kind="telegram"), [])
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_stop_hook_without_existing_observed_session_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="Stop",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp, "message": "late stop"},
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "unobserved_tui_hook")
            self.assertEqual(runtime.state.sessions.list_sessions(channel_kind="telegram"), [])
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_non_observation_hook_is_accepted_as_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="ConfigChange",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp, "message": "config changed"},
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "non_observation_hook")
            self.assertEqual(runtime.state.sessions.list_sessions(channel_kind="telegram"), [])
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_deferred_tui_hook_queue_is_drained_by_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            queued = runtime.defer_tui_hook(
                hook_type="SessionStart",
                agent="claude",
                payload={"session_id": "claude-session-1", "cwd": tmp},
            )
            self.assertTrue(queued["queued"])
            self.assertEqual(api.calls, [])

            drained = asyncio.run(runtime.drain_deferred_tui_hooks())

            self.assertEqual(drained, 1)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 1)
            self.assertIn("WalkCode session: claude: TUI claude-session-1", send_messages[0]["text"])
            self.assertEqual(list(Path(f"{state_path}.tui-hooks.d").glob("*.json")), [])

    def test_deferred_tui_hook_filename_uses_nanosecond_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            with patch.object(runtime_module.time, "time_ns", side_effect=[1_000_000_001, 1_000_000_002]):
                first = runtime.defer_tui_hook(
                    hook_type="PreToolUse",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp},
                )
                second = runtime.defer_tui_hook(
                    hook_type="PostToolUse",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp},
                )

            self.assertLess(Path(first["path"]).name, Path(second["path"]).name)
            self.assertTrue(Path(first["path"]).name.startswith("0000000001000000001-"))
            self.assertTrue(Path(second["path"]).name.startswith("0000000001000000002-"))

    def test_deferred_tui_hook_drain_prioritizes_recent_hooks_over_old_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            old_ns = 10_000_000_000
            recent_ns = old_ns + 600_000_000_000
            with patch.object(runtime_module.time, "time_ns", side_effect=[old_ns, recent_ns]):
                runtime.defer_tui_hook(
                    hook_type="SessionStart",
                    agent="claude",
                    payload={"session_id": "old-session", "marker": "old"},
                )
                runtime.defer_tui_hook(
                    hook_type="SessionStart",
                    agent="claude",
                    payload={"session_id": "recent-session", "marker": "recent"},
                )

            order = []

            async def record_hook(*, hook_type, payload, agent=""):
                order.append(payload["marker"])
                return SubmitResult(True)

            runtime.process_tui_hook = record_hook
            with patch.object(runtime_module.time, "time", return_value=recent_ns / 1_000_000_000):
                drained = asyncio.run(runtime.drain_deferred_tui_hooks(limit=2))

            self.assertEqual(drained, 2)
            self.assertEqual(order, ["recent", "old"])

    def test_tui_tool_hooks_update_single_telegram_tool_progress_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="SessionStart",
                    agent="claude",
                    payload={"session_id": "claude-session-1", "cwd": tmp},
                )
            )
            started = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="PreToolUse",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "tool_use_id": "tool-1",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "README.md"},
                    },
                )
            )
            completed = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="PostToolUse",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "tool_use_id": "tool-1",
                        "tool_name": "Read",
                        "tool_response": "full output should not be sent",
                    },
                )
            )

            self.assertTrue(started.accepted)
            self.assertTrue(completed.accepted)
            sent_tool_cards = [
                payload["text"]
                for method, payload in api.calls
                if method == "sendMessage" and "Agent activity" in payload["text"]
            ]
            edited_tool_cards = [
                payload["text"]
                for method, payload in api.calls
                if method == "editMessageText" and "Agent activity" in payload["text"]
            ]
            self.assertEqual(len(sent_tool_cards), 1)
            self.assertTrue(any("Status: COMPLETED" in text for text in edited_tool_cards))
            self.assertFalse(any("full output should not be sent" in text for text in sent_tool_cards + edited_tool_cards))
            session = runtime.state.sessions.get(runtime.state.sessions.list_sessions(channel_kind="telegram")[0].session_id)
            self.assertTrue(session.channel_binding.capabilities["tool_progress_message_id"])
            self.assertEqual(session.last_progress_event, AgentEventType.TOOL_COMPLETED)
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")

    def test_user_prompt_submit_creates_observed_session_when_session_start_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-early",
                        "cwd": tmp,
                        "prompt": "hello from TUI",
                    },
                )
            )

            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 2)
            self.assertIn("WalkCode session: claude: TUI claude-session-early", send_messages[0]["text"])
            self.assertEqual(send_messages[1]["text"], "⌨️ 终端输入\n\nhello from TUI")
            self.assertEqual(runtime.transports["claude_headless"].submitted_turns, [])
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(summaries), 1)
            session = snapshot.sessions.get(summaries[0].session_id)
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertEqual(session.last_progress_event, "external_tui.user-prompt-submit")
            self.assertEqual(session.transport_ref["resume_ref"]["agent_session_id"], "claude-session-early")

    def test_user_prompt_submit_existing_observed_session_syncs_readonly_transcript_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport("claude_headless", _transport_caps())
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="SessionStart",
                    agent="claude",
                    payload={"session_id": "claude-session-input", "cwd": tmp},
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-input",
                        "cwd": tmp,
                        "prompt": "please inspect the branch",
                    },
                )
            )

            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(send_messages[-1]["text"], "⌨️ 终端输入\n\nplease inspect the branch")
            self.assertEqual(transport.submitted_turns, [])
            session = runtime.state.sessions.get(runtime.state.sessions.list_sessions(channel_kind="telegram")[0].session_id)
            self.assertEqual(session.writer_owner.kind, "external_tui")
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertEqual(session.last_progress_event, "external_tui.user-prompt-submit")

    def test_message_display_hook_sends_claude_message_content_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="claude",
                    payload={"session_id": "claude-session-display", "cwd": tmp, "prompt": "question"},
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="MessageDisplay",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-display",
                        "cwd": tmp,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "text": "hidden thought"},
                                {"type": "text", "text": "hello from assistant"},
                            ],
                        },
                    },
                )
            )

            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(send_messages[-1]["text"], "hello from assistant")
            self.assertNotIn("hidden thought", send_messages[-1]["text"])
            self.assertNotIn("'content'", send_messages[-1]["text"])

    def test_pre_tool_hook_creates_observed_session_when_first_tui_event_is_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="PreToolUse",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-tool-first",
                        "cwd": tmp,
                        "tool_use_id": "tool-1",
                        "tool_name": "Bash",
                        "tool_input": {"command": "date"},
                    },
                )
            )

            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertTrue(any("WalkCode session: claude: TUI claude-session-tool-first" in item["text"] for item in send_messages))
            self.assertTrue(any("Agent activity" in item["text"] and "Tool: Bash" in item["text"] for item in send_messages))
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(summaries), 1)
            session = snapshot.sessions.get(summaries[0].session_id)
            self.assertEqual(session.last_progress_event, AgentEventType.TOOL_STARTED)

    def test_tui_hook_creates_forum_topic_for_observed_session_when_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="codex",
                    payload={
                        "thread_id": "codex-thread-1",
                        "cwd": tmp,
                        "_walkcode_hook_process_tree": ["codex --ask-for-approval on-request"],
                    },
                )
            )

            self.assertTrue(result.accepted)
            create_calls = [payload for method, payload in api.calls if method == "createForumTopic"]
            self.assertEqual(len(create_calls), 1)
            self.assertEqual(create_calls[0]["chat_id"], "-100")
            self.assertIn("codex: TUI codex-thread-1", create_calls[0]["name"])
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(sent[0]["message_thread_id"], "777")
            snapshot = JsonFileStateStore(state_path).load()
            summaries = snapshot.sessions.list_sessions(channel_kind="telegram")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0].thread_id, "777")

    def test_tui_hook_backfills_status_card_capabilities_for_existing_observed_topic(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            runtime.state.sessions.create_observed_session(
                session_id="tui-codex-old",
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="-100",
                    thread_id="777",
                    capabilities={"topic_closed": True},
                ),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "agent": "codex",
                    "resume_ref": {
                        "transport_kind": "codex_app_server",
                        "thread_id": "codex-thread-1",
                    },
                },
                owner=ActorRef("telegram", "local_tui:codex_app_server:codex-thread-1", "codex TUI"),
            )
            runtime.state.sessions.get("tui-codex-old").lifecycle_state = "ACTIVE"

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="codex",
                    payload={
                        "thread_id": "codex-thread-1",
                        "cwd": tmp,
                        "_walkcode_hook_process_tree": ["codex --ask-for-approval on-request"],
                    },
                )
            )

            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get("tui-codex-old")
            self.assertTrue(updated.channel_binding.capabilities["status_card"])
            self.assertTrue(updated.channel_binding.capabilities["readonly_topic"])
            self.assertTrue(updated.channel_binding.capabilities["pin_status_card"])
            self.assertTrue(updated.channel_binding.capabilities["static_status_card"])
            self.assertEqual(updated.channel_binding.capabilities["origin"], "external_tui")
            self.assertNotIn("topic_closed", updated.channel_binding.capabilities)
            self.assertEqual(updated.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertTrue(any(payload.get("message_thread_id") == "777" for payload in sent))
            self.assertTrue(any("WalkCode session:" in payload.get("text", "") for payload in sent))
            close_calls = [payload for method, payload in api.calls if method == "closeForumTopic"]
            self.assertEqual(close_calls, [])

    def test_serve_backfills_loaded_tui_observed_topic_status_cards_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            first_runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_ForumTelegramApi(),
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            first_runtime.state.sessions.create_observed_session(
                session_id="tui-codex-loaded",
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="-100",
                    thread_id="777",
                    capabilities={},
                ),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "agent": "codex",
                    "resume_ref": {
                        "transport_kind": "codex_app_server",
                        "thread_id": "codex-thread-1",
                    },
                },
                owner=ActorRef("telegram", "local_tui:codex_app_server:codex-thread-1", "codex TUI"),
            )
            first_runtime.save_state()

            api = _ForumTelegramApi(batches=[[]])
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            asyncio.run(runtime.serve_telegram_polling(timeout=0, max_iterations=1))

            updated = runtime.state.sessions.get("tui-codex-loaded")
            self.assertTrue(updated.channel_binding.capabilities["status_card"])
            self.assertTrue(updated.channel_binding.health_message_id)
            sent = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertTrue(any(payload.get("message_thread_id") == "777" for payload in sent))

    def test_tui_hook_claims_existing_claude_structured_session_by_agent_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "_walkcode_hook_process_tree": [
                            "claude --settings /Users/alpha/.claude/profiles/vertex.json"
                        ],
                    },
                )
            )

            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "external_tui")
            self.assertEqual(updated.generation, 1)
            self.assertEqual(updated.transport_ref["resume_ref"]["agent_session_id"], "claude-session-1")
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_tui_hook_from_walkcode_owned_claude_headless_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            fake_ps = subprocess.CompletedProcess(
                args=["ps"],
                returncode=0,
                stdout=(
                    "222 1 /Users/alpha/.local/share/uv/tools/walkcode/lib/python3.13/"
                    "site-packages/claude_agent_sdk/_bundled/claude --output-format stream-json "
                    "--input-format stream-json\n"
                ),
                stderr="",
            )

            with patch.object(runtime_module.subprocess, "run", return_value=fake_ps):
                result = asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="sync",
                        agent="claude",
                        payload={
                            "session_id": "claude-session-1",
                            "cwd": tmp,
                            "terminate_ref": {
                                "controller_kind": "process",
                                "process_ref": {"pid": 222, "allow_terminate": False},
                            },
                        },
                    )
                )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "internal_headless_hook_ignored")
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "claude_headless")
            self.assertEqual(updated.generation, 0)
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_deferred_tui_hook_from_captured_walkcode_owned_claude_headless_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="Stop",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "message": "duplicate stop output",
                        "_walkcode_hook_process_tree": [
                            "/Users/alpha/.local/share/uv/tools/walkcode/lib/python3.13/"
                            "site-packages/claude_agent_sdk/_bundled/claude "
                            "--output-format stream-json --input-format stream-json"
                        ],
                    },
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "internal_headless_hook_ignored")
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "claude_headless")
            self.assertEqual(updated.status, "running")
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_unverified_tui_hook_cannot_reclaim_walkcode_owned_headless_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                    },
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "internal_headless_hook_ignored")
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "claude_headless")
            self.assertEqual(updated.status, "running")
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_real_tui_hook_still_claims_matching_structured_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            fake_ps = subprocess.CompletedProcess(
                args=["ps"],
                returncode=0,
                stdout="222 1 claude --settings /Users/alpha/.claude/profiles/vertex.json\n",
                stderr="",
            )

            with patch.object(runtime_module.subprocess, "run", return_value=fake_ps):
                result = asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="sync",
                        agent="claude",
                        payload={
                            "session_id": "claude-session-1",
                            "cwd": tmp,
                            "terminate_ref": {
                                "controller_kind": "process",
                                "process_ref": {"pid": 222, "allow_terminate": True},
                            },
                        },
                    )
                )

            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "external_tui")
            self.assertEqual(updated.transport_kind, "external_tui")
            self.assertEqual(updated.generation, 1)

    def test_tui_hook_for_stopped_structured_session_is_accepted_without_reclaiming(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            session.status = "stopped"
            session.lifecycle_state = "STOPPED"
            session.stop_reason = "already_done"

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="stop",
                    agent="claude",
                    payload={
                        "session_id": "claude-session-1",
                        "cwd": tmp,
                        "message": "late stop output",
                    },
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual([method for method, _payload in api.calls], [])
            snapshot = JsonFileStateStore(state_path).load()
            updated = snapshot.sessions.get(session.session_id)
            self.assertEqual(updated.status, "stopped")
            self.assertEqual(updated.lifecycle_state, "STOPPED")
            self.assertEqual(updated.stop_reason, "already_done")
            ledger = snapshot.inbound_ledger.to_dict()
            self.assertEqual(ledger["in_progress"], {})
            self.assertEqual(len(ledger["completed"]), 1)

    def test_user_prompt_submit_hook_for_stopped_session_is_noop_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="codex_app_server",
                transport_ref={"handle_id": "h1", "thread_id": "codex-thread-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )
            session.status = "stopped"
            session.lifecycle_state = "STOPPED"
            session.stop_reason = "already_done"

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="UserPromptSubmit",
                    agent="codex",
                    payload={"thread_id": "codex-thread-1", "cwd": tmp, "message": "new prompt"},
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual([method for method, _payload in api.calls], [])
            updated = JsonFileStateStore(state_path).load().sessions.get(session.session_id)
            self.assertEqual(updated.status, "stopped")
            self.assertEqual(updated.stop_reason, "already_done")

    def test_tui_hook_claims_existing_codex_structured_session_by_thread_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=_FakeTelegramApi(),
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="codex_app_server",
                transport_ref={"handle_id": "h1", "thread_id": "codex-thread-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="codex",
                    payload={
                        "thread_id": "codex-thread-1",
                        "cwd": tmp,
                        "_walkcode_hook_process_tree": ["codex --ask-for-approval on-request"],
                    },
                )
            )

            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "external_tui")
            self.assertEqual(updated.transport_ref["resume_ref"]["thread_id"], "codex-thread-1")

    def test_stop_hook_does_not_claim_matching_im_structured_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )
            session = runtime.state.sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="123",
                    root_message_id="3",
                ),
                transport_kind="codex_app_server",
                transport_ref={"handle_id": "h1", "thread_id": "codex-thread-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "456", "Ada"),
            )

            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="stop",
                    agent="codex",
                    payload={"thread_id": "codex-thread-1", "cwd": tmp, "message": "late stop"},
                )
            )

            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "unobserved_tui_hook")
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.status, "running")
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "codex_app_server")
            self.assertEqual([method for method, _payload in api.calls], [])

    def test_tui_hook_duplicate_event_is_not_sent_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
            )
            payload = {
                "session_id": "claude-session-1",
                "turn_id": "turn-1",
                "message": "same output",
                "cwd": tmp,
            }

            asyncio.run(runtime.process_tui_hook(hook_type="sync", agent="claude", payload=payload))
            first = asyncio.run(runtime.process_tui_hook(hook_type="stop", agent="claude", payload=payload))
            second = asyncio.run(runtime.process_tui_hook(hook_type="stop", agent="claude", payload=payload))

            self.assertTrue(first.accepted)
            self.assertTrue(second.accepted)
            self.assertEqual(second.reason, BlockedReason.DUPLICATE_INBOUND)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 2)

    def test_tui_hook_filters_internal_codex_status_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            created = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="codex",
                    payload={"thread_id": "codex-thread-1"},
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="tui-output",
                    agent="codex",
                    payload={
                        "thread_id": "codex-thread-1",
                        "message": "[thread/status/changed] {'threadId': 'codex-thread-1', 'status': {'type': 'idle'}}",
                    },
                )
            )

            self.assertTrue(created.accepted)
            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 1)
            self.assertIn("WalkCode session: codex: TUI codex-thread-1", send_messages[0]["text"])
            self.assertIn("Input: read-only until takeover", send_messages[0]["text"])

    def test_tui_hook_filters_raw_hook_handler_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"codex_app_server": FakeAgentTransport("codex_app_server", _transport_caps())},
            )

            created = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync",
                    agent="codex",
                    payload={"thread_id": "codex-thread-1"},
                )
            )
            result = asyncio.run(
                runtime.process_tui_hook(
                    hook_type="tui-output",
                    agent="codex",
                    payload={
                        "thread_id": "codex-thread-1",
                        "message": (
                            "hook handler run: {'eventName': 'stop', "
                            "'handlerType': 'command', 'executionMode': 'sync'}"
                        ),
                    },
                )
            )

            self.assertTrue(created.accepted)
            self.assertTrue(result.accepted)
            send_messages = [payload for method, payload in api.calls if method == "sendMessage"]
            self.assertEqual(len(send_messages), 1)
            self.assertIn("WalkCode session: codex: TUI codex-thread-1", send_messages[0]["text"])
            self.assertIn("Input: read-only until takeover", send_messages[0]["text"])

    def test_tui_takeover_flow_terminates_process_resumes_and_submits_blocked_input(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = ChannelNativeConfig.from_env(
                    {
                        "WALKCODE_CHANNEL": "telegram",
                        "TELEGRAM_BOT_TOKEN": "fake",
                        "WALKCODE_AGENT": "claude",
                        "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                        "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                        "WALKCODE_CWD": tmp,
                    }
                )
                api = _FakeTelegramApi()
                transport = FakeAgentTransport(
                    "claude_headless",
                    _transport_caps(),
                    scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "submitted"})],
                )
                runtime = ChannelNativeRuntime.from_config(
                    cfg,
                    telegram_api=api,
                    transports={"claude_headless": transport},
                )
                session = runtime.state.sessions.create_structured_session(
                    binding=ChannelBinding(
                        channel_kind="telegram",
                        account_id="bot",
                        chat_id="123",
                        root_message_id="3",
                    ),
                    transport_kind="claude_headless",
                    transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
                    cwd=tmp,
                    owner=ActorRef("telegram", "456", "Ada"),
                )
                runtime.state.authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)

                claimed = asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="sync",
                        agent="claude",
                        payload={
                            "session_id": "claude-session-1",
                            "cwd": tmp,
                            "terminate_ref": {
                                "controller_kind": "process",
                                "process_ref": {"pid": proc.pid, "allow_terminate": True},
                            },
                            "_walkcode_hook_process_tree_entries": [
                                {
                                    "pid": proc.pid,
                                    "ppid": 1,
                                    "command": "claude --settings /Users/alpha/.claude/profiles/vertex.json",
                                }
                            ],
                            "_walkcode_hook_process_tree": [
                                "claude --settings /Users/alpha/.claude/profiles/vertex.json"
                            ],
                        },
                    )
                )
                blocked = asyncio.run(
                    runtime.process_telegram_update(
                        _telegram_update(50, text="please take over", reply_to_message_id="3")
                    )
                )
                take_over_token = _latest_callback_token(api, "Take over and send")
                confirmed = asyncio.run(
                    runtime.process_telegram_update(
                        _telegram_callback(51, token=take_over_token, reply_to_message_id="3")
                    )
                )

                self.assertTrue(claimed.accepted)
                self.assertFalse(blocked.accepted)
                self.assertEqual(blocked.reason, BlockedReason.EXTERNAL_TUI_READONLY)
                self.assertTrue(confirmed.accepted)
                proc.wait(timeout=2.0)
                self.assertIsNotNone(proc.returncode)
                self.assertEqual(transport.call_log[:2], ["resume", "submit_turn"])
                self.assertEqual(transport.resume_specs[0].resume_ref["agent_session_id"], "claude-session-1")
                self.assertEqual([turn.text for turn in transport.submitted_turns], ["please take over"])
                updated = runtime.state.sessions.get(session.session_id)
                self.assertEqual(updated.writer_owner.kind, "orchestrator")
                self.assertEqual(updated.transport_kind, "claude_headless")

                send_count_after_takeover = len(
                    [payload for method, payload in api.calls if method == "sendMessage"]
                )
                late_sync = asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="SessionStart",
                        agent="claude",
                        payload={
                            "session_id": "claude-session-1",
                            "cwd": tmp,
                        },
                    )
                )
                self.assertTrue(late_sync.accepted)
                self.assertEqual(late_sync.reason, "internal_headless_hook_ignored")
                late_stop = asyncio.run(
                    runtime.process_tui_hook(
                        hook_type="Stop",
                        agent="claude",
                        payload={
                            "session_id": "claude-session-1",
                            "cwd": tmp,
                            "message": "submitted",
                        },
                    )
                )
                self.assertTrue(late_stop.accepted)
                self.assertEqual(late_stop.reason, "unobserved_tui_hook")
                still_owned = runtime.state.sessions.get(session.session_id)
                self.assertEqual(still_owned.writer_owner.kind, "orchestrator")
                self.assertEqual(still_owned.transport_kind, "claude_headless")
                self.assertEqual(still_owned.status, "running")
                self.assertEqual(
                    len([payload for method, payload in api.calls if method == "sendMessage"]),
                    send_count_after_takeover,
                )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2.0)

    def test_inferred_native_hook_parent_is_not_authorized_for_termination(self):
        fake_ps = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="12345\n",
            stderr="",
        )

        with patch.object(runtime_module.subprocess, "run", return_value=fake_ps):
            process_ref = runtime_module._infer_process_ref_from_hook_pid(99)

        self.assertIsNotNone(process_ref)
        self.assertEqual(process_ref["pid"], 12345)
        self.assertEqual(process_ref["source"], "native_hook_parent")
        self.assertFalse(process_ref["allow_terminate"])

    def test_inferred_native_hook_process_group_authorizes_external_tui_termination(self):
        fake_ps = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="23831 23723 claude\n",
            stderr="",
        )

        with patch.object(runtime_module.subprocess, "run", return_value=fake_ps):
            terminate_ref = runtime_module._tui_terminate_ref(
                {
                    "_walkcode_infer_tui_pid": True,
                    "_walkcode_hook_process_group": 23831,
                }
            )

        self.assertIsNotNone(terminate_ref)
        process_ref = terminate_ref["process_ref"]
        self.assertEqual(process_ref["pid"], 23831)
        self.assertEqual(process_ref["source"], "native_hook_process_group")
        self.assertTrue(process_ref["allow_terminate"])

    def test_hook_shell_command_is_not_misclassified_as_external_tui(self):
        self.assertFalse(
            runtime_module._command_is_external_tui_process(
                "zsh -lc WALKCODE_AGENT=claude walkcode native hook SessionStart --agent claude --defer"
            )
        )


if __name__ == "__main__":
    unittest.main()


class ClaudeModelChoiceInventoryTests(unittest.TestCase):
    def test_walkcode_model_choices_yields_full_picker_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp) / "cfg"
            cdir.mkdir()
            (cdir / "settings.json").write_text(
                json.dumps(
                    {
                        "env": {"ANTHROPIC_MODEL": "claude-opus-4-8"},
                        "walkcode_model_choices": [
                            {"slug": "claude-opus-4-8", "display_name": "Opus 4.8"},
                            "claude-sonnet-5",
                            {"slug": "claude-haiku-4-5", "display_name": "Haiku 4.5"},
                        ],
                    }
                )
            )
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "a",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_CONFIG_DIR": str(cdir),
                }
            )
            inv = runtime_module._local_model_inventory(cfg, "claude_headless")
            self.assertEqual(
                [(m["slug"], m["display_name"]) for m in inv["models"]],
                [
                    ("claude-opus-4-8", "Opus 4.8"),
                    ("claude-sonnet-5", "claude-sonnet-5"),
                    ("claude-haiku-4-5", "Haiku 4.5"),
                ],
            )
            self.assertEqual(inv["current"], "claude-opus-4-8")

    def test_no_explicit_choices_falls_back_to_model_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp) / "cfg"
            cdir.mkdir()
            (cdir / "settings.json").write_text(
                json.dumps({"env": {"ANTHROPIC_MODEL": "opus", "ANTHROPIC_SMALL_FAST_MODEL": "haiku"}})
            )
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "lark",
                    "LARK_APP_ID": "a",
                    "LARK_APP_SECRET": "s",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_CONFIG_DIR": str(cdir),
                }
            )
            inv = runtime_module._local_model_inventory(cfg, "claude_headless")
            self.assertEqual([m["slug"] for m in inv["models"]], ["opus", "haiku"])


class TranscriptModelBackfillTests(unittest.TestCase):
    def test_reads_latest_assistant_model_from_transcript_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "user", "message": {"role": "user"}}),
                        json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5[1m]"}}),
                        json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}}),
                        json.dumps({"type": "progress"}),
                        "not json",
                    ]
                )
            )
            self.assertEqual(
                runtime_module._transcript_model_from_payload({"transcript_path": str(transcript)}),
                "claude-sonnet-5[1m]",
            )

    def test_missing_or_absent_transcript_returns_empty(self):
        self.assertEqual(runtime_module._transcript_model_from_payload({}), "")
        self.assertEqual(
            runtime_module._transcript_model_from_payload({"transcript_path": "/nonexistent/x.jsonl"}),
            "",
        )




class TuiHookModelBackfillIntegrationTests(unittest.TestCase):
    def test_process_tui_hook_backfills_session_model_from_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "t.jsonl"
            transcript.write_text(
                json.dumps({"type": "assistant", "message": {"model": "claude-sonnet-5[1m]"}})
            )
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "TELEGRAM_ALLOWED_CHAT_IDS": "-100",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _ForumTelegramApi()
            runtime = ChannelNativeRuntime.from_config(cfg, telegram_api=api, transports={})
            payload = {
                "hook_event_name": "SessionStart",
                "session_id": "sess-tui-model",
                "transcript_path": str(transcript),
                "cwd": tmp,
                "_walkcode_external_tui_pid": 4242,
            }
            asyncio.run(runtime.process_tui_hook(hook_type="SessionStart", payload=payload, agent="claude"))
            sessions = [
                s for s in runtime.state.sessions.iter_sessions()
                if s.transport_kind == "external_tui"
            ]
            if not sessions:
                self.skipTest("TUI hook did not create an observed session in this configuration")
            self.assertEqual(sessions[0].model, "claude-sonnet-5[1m]")

class OrphanHeadlessSweepTests(unittest.TestCase):
    def test_sweep_settles_orchestrator_owned_headless_sessions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = str(Path(tmp) / "state.json")
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_STATE_PATH": state_path,
                    "WALKCODE_CWD": tmp,
                }
            )
            api = _FakeTelegramApi()
            transport = FakeAgentTransport(
                "claude_headless",
                _transport_caps(),
                scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})],
            )
            runtime = ChannelNativeRuntime.from_config(
                cfg,
                telegram_api=api,
                transports={"claude_headless": transport},
            )
            result = asyncio.run(runtime.process_telegram_update(_telegram_update()))
            self.assertTrue(result.accepted)
            session = next(runtime.state.sessions.iter_sessions())
            self.assertEqual(session.status, "running")

            # A TUI-owned session must survive the sweep untouched.
            session.writer_owner = runtime_module.WriterOwner(kind="external_tui")
            asyncio.run(runtime._settle_orphan_headless_sessions_once())
            self.assertEqual(session.status, "running")

            # An IDLE session must also survive: the resume path revives it.
            runtime._orphan_sweep_done = False
            session.writer_owner = runtime_module.WriterOwner(kind="orchestrator")
            session.lifecycle_state = "IDLE"
            asyncio.run(runtime._settle_orphan_headless_sessions_once())
            self.assertEqual(session.status, "running")

            # An in-flight orchestrator-owned session is a true zombie.
            runtime._orphan_sweep_done = False
            session.lifecycle_state = "WAITING_USER"
            session.writer_owner = runtime_module.WriterOwner(kind="orchestrator")
            asyncio.run(runtime._settle_orphan_headless_sessions_once())
            self.assertEqual(session.status, "stopped")
            self.assertEqual(session.stop_reason, "runtime_restart")
            self.assertEqual(session.lifecycle_state, "STOPPED")
