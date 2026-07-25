"""Channel environment context injection (2026-07-25 user request).

The user talks through Feishu/Telegram and cannot see the machine walkcode
runs on; agents kept asking them to "scan the QR code on the screen". Every
agent conversation a channel drives must therefore carry an environment
preamble. Per-transport mechanism:

- claude_headless: appended to the REAL system prompt via the SDK's
  claude_code preset (launch and resume both build options through
  _create_client, so native sessions and post-takeover resumes are covered);
- codex_app_server: no append surface exists (base_instructions REPLACES the
  built-in prompt), so the context rides the first turn of each
  launched/resumed thread.
"""

import asyncio
import unittest

from walkcode.channel_native import (
    ClaudeHeadlessTransport,
    CodexAppServerTransport,
    LaunchSpec,
    TransportUnavailable,
    TurnInput,
    _channel_environment_context,
)


class EnvironmentContextTemplateTests(unittest.TestCase):
    def test_channel_names_are_substituted(self):
        lark = _channel_environment_context("lark")
        self.assertIn("Feishu (Lark)", lark)
        self.assertIn("<environment_context>", lark)
        self.assertIn("</environment_context>", lark)
        self.assertIn("Telegram", _channel_environment_context("telegram"))
        self.assertIn("the remote chat", _channel_environment_context("unknown-channel"))

    def test_core_behavior_rules_are_present(self):
        text = _channel_environment_context("lark")
        # The incident this exists for: QR-login flows pointing at the local
        # screen. The rules must survive future rewording.
        self.assertIn("QR", text)
        self.assertIn("cannot see the local terminal", text)


class _RecordingClient:
    def __init__(self, options=None):
        self.options = options


class _OptionsModernSdk:
    """Mimics the installed claude_agent_sdk: system_prompt, no append field."""

    def __init__(self, cwd=None, system_prompt=None, max_buffer_size=None, env=None,
                 settings=None, cli_path=None, permission_mode=None, resume=None):
        self.kwargs = {"cwd": cwd, "system_prompt": system_prompt}
        self.system_prompt = system_prompt
        self.max_buffer_size = max_buffer_size


class _OptionsLegacySdk:
    """Mimics an SDK generation with append_system_prompt instead."""

    def __init__(self, cwd=None, append_system_prompt=None, env=None, settings=None,
                 cli_path=None, permission_mode=None, resume=None):
        self.append_system_prompt = append_system_prompt


class _OptionsNoPromptSupport:
    def __init__(self, cwd=None, env=None, settings=None, cli_path=None,
                 permission_mode=None, resume=None):
        self.cwd = cwd


def _sdk(options_cls):
    class SDK:
        ClaudeAgentOptions = options_cls
        ClaudeSDKClient = _RecordingClient

    return SDK


class ClaudeEnvironmentContextOptionTests(unittest.TestCase):
    def _client_options(self, options_cls, environment_context):
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: _sdk(options_cls),
            environment_context=environment_context,
        )
        client, _bridge = transport._create_client(LaunchSpec(cwd="/tmp", session_id="s1"))
        return client.options

    def test_modern_sdk_gets_claude_code_preset_with_append(self):
        context = _channel_environment_context("lark")
        options = self._client_options(_OptionsModernSdk, context)
        self.assertEqual(
            options.system_prompt,
            {"type": "preset", "preset": "claude_code", "append": context},
        )

    def test_legacy_sdk_gets_append_system_prompt(self):
        context = _channel_environment_context("telegram")
        options = self._client_options(_OptionsLegacySdk, context)
        self.assertEqual(options.append_system_prompt, context)

    def test_unsupporting_sdk_is_not_passed_unknown_kwargs(self):
        # Passing an unknown kwarg would raise TypeError inside _create_client
        # and surface as TransportUnavailable — this asserts it constructs.
        options = self._client_options(_OptionsNoPromptSupport, _channel_environment_context("lark"))
        self.assertEqual(options.cwd, "/tmp")

    def test_empty_context_sets_nothing(self):
        options = self._client_options(_OptionsModernSdk, "")
        self.assertIsNone(options.system_prompt)

    def test_sdk_stream_buffer_ceiling_is_raised(self):
        # The SDK default rejects any single stream-json message over 1 MiB
        # ("Agent output stream failed ... maximum buffer size"); large tool
        # results hit that in real headless turns.
        options = self._client_options(_OptionsModernSdk, "")
        self.assertEqual(options.max_buffer_size, 64 * 1024 * 1024)
        # An SDK without the field must simply not receive the kwarg.
        options = self._client_options(_OptionsNoPromptSupport, "")
        self.assertEqual(options.cwd, "/tmp")

    def test_resume_path_builds_options_with_context_too(self):
        # Post-takeover resumes go through the same _create_client; the
        # resume kwarg and the preset must coexist.
        context = _channel_environment_context("lark")
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: _sdk(_OptionsModernSdk),
            environment_context=context,
        )
        client, _bridge = transport._create_client(
            LaunchSpec(cwd="/tmp", session_id="s1"), resume_id="agent-native-id"
        )
        self.assertEqual(client.options.system_prompt["append"], context)


class _FakeCodexClient:
    def __init__(self, fail_first_turn=False):
        self.requests = []
        self._fail_first_turn = fail_first_turn

    async def request(self, method, params):
        if method == "turn/start" and self._fail_first_turn:
            self._fail_first_turn = False
            raise TransportUnavailable("boom")
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        return {}


class CodexEnvironmentContextTests(unittest.TestCase):
    def setUp(self):
        self.context = _channel_environment_context("lark")

    def _turn_texts(self, client):
        return [
            params["input"][0]["text"]
            for method, params in client.requests
            if method == "turn/start"
        ]

    def test_first_turn_of_launched_thread_carries_context_once(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, environment_context=self.context)

        async def scenario():
            handle = await transport.launch(LaunchSpec(cwd="/tmp", session_id="s1"))
            await transport.submit_turn(handle, TurnInput(text="first"), idempotency_key="k1")
            await transport.submit_turn(handle, TurnInput(text="second"), idempotency_key="k2")

        asyncio.run(scenario())
        texts = self._turn_texts(client)
        self.assertEqual(texts[0], f"{self.context}\n\nfirst")
        self.assertEqual(texts[1], "second")

    def test_resumed_thread_carries_context_once_and_not_on_rresume(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, environment_context=self.context)

        async def scenario():
            handle = await transport.resume_thread("thread-9", cwd="/tmp")
            await transport.submit_turn(handle, TurnInput(text="after takeover"), idempotency_key="k1")
            # Writer reacquisition resumes the SAME thread again and again;
            # the preamble must not repeat on each resume.
            handle2 = await transport.resume_thread("thread-9", cwd="/tmp")
            await transport.submit_turn(handle2, TurnInput(text="later"), idempotency_key="k2")

        asyncio.run(scenario())
        texts = self._turn_texts(client)
        self.assertEqual(texts[0], f"{self.context}\n\nafter takeover")
        self.assertEqual(texts[1], "later")

    def test_failed_first_submit_keeps_context_pending_for_retry(self):
        client = _FakeCodexClient(fail_first_turn=True)
        transport = CodexAppServerTransport(client=client, environment_context=self.context)

        async def scenario():
            handle = await transport.launch(LaunchSpec(cwd="/tmp", session_id="s1"))
            with self.assertRaises(TransportUnavailable):
                await transport.submit_turn(handle, TurnInput(text="first"), idempotency_key="k1")
            await transport.submit_turn(handle, TurnInput(text="first"), idempotency_key="k1")

        asyncio.run(scenario())
        texts = self._turn_texts(client)
        self.assertEqual(texts, [f"{self.context}\n\nfirst"])

    def test_no_context_configured_leaves_turns_untouched(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client)

        async def scenario():
            handle = await transport.launch(LaunchSpec(cwd="/tmp", session_id="s1"))
            await transport.submit_turn(handle, TurnInput(text="plain"), idempotency_key="k1")

        asyncio.run(scenario())
        self.assertEqual(self._turn_texts(client), ["plain"])


class RuntimeWiringTests(unittest.TestCase):
    def test_claude_transport_receives_channel_context(self):
        from walkcode.channel_native import ChannelNativeConfig
        from walkcode.channel_native_runtime import _build_transports

        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_CWD": "/tmp",
            }
        )
        transports = _build_transports(cfg)
        self.assertIn("Telegram", transports["claude_headless"].environment_context)

    def test_codex_transport_receives_channel_context(self):
        from unittest.mock import patch

        from walkcode.channel_native import ChannelNativeConfig
        from walkcode import channel_native_runtime as runtime_module

        cfg = ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "codex",
                "WALKCODE_CWD": "/tmp",
            }
        )
        with patch.object(runtime_module.shutil, "which", return_value="/usr/bin/codex"):
            transports = runtime_module._build_transports(cfg)
        self.assertIn("Telegram", transports["codex_app_server"].environment_context)


if __name__ == "__main__":
    unittest.main()
