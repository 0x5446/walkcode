import asyncio
import io
import urllib.error
import urllib.request
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    BlockedReason,
    ChannelBinding,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeAgentTransport,
    InteractionStore,
    LaunchSpec,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    TelegramBotApi,
    TelegramChannelAdapter,
    TransientDeliveryError,
    TransportCapabilities,
    TransportUnavailable,
    TurnInput,
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


class TelegramAdapterTests(unittest.TestCase):
    def test_parse_private_text_message(self):
        adapter = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        event = adapter.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 200, "first_name": "Ada"},
                    "text": "hello",
                },
            }
        )

        self.assertEqual(event.event_id, "telegram:1")
        self.assertEqual(event.chat_id, "100")
        self.assertEqual(event.message_id, "10")
        self.assertEqual(event.root_message_id, "")
        self.assertEqual(event.sender_id, "200")
        self.assertEqual(event.text, "hello")

    def test_parse_callback_query_with_short_token(self):
        adapter = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=lambda *_: {}))
        event = adapter.parse_update(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cb-1",
                    "from": {"id": 200, "first_name": "Ada"},
                    "data": "cb:short-token",
                    "message": {
                        "message_id": 11,
                        "chat": {"id": 100, "type": "private"},
                    },
                },
            }
        )

        self.assertEqual(event.callback["token"], "short-token")
        self.assertEqual(event.callback["callback_query_id"], "cb-1")
        self.assertEqual(event.message_id, "11")

    def test_send_view_splits_long_text(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api, max_text_chars=10)
        binding = ChannelBinding(
            channel_kind="telegram",
            account_id="bot",
            chat_id="100",
            thread_id="",
            root_message_id="10",
        )

        message_id = asyncio.run(
            adapter.send_view(binding, {"type": "turn_delta", "text": "abcdefghijklmno"})
        )

        self.assertEqual(message_id, "2")
        self.assertEqual([call[0] for call in api.calls], ["sendMessage", "sendMessage"])
        self.assertEqual(api.calls[0][1]["text"], "abcdefghij")
        self.assertEqual(api.calls[1][1]["text"], "klmno")

    def test_agent_markdown_is_sent_as_telegram_html_by_default(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api)
        binding = ChannelBinding("telegram", "bot", "100")

        message_id = asyncio.run(
            adapter.send_view(
                binding,
                {
                    "type": "turn_completed",
                    "message": "## Result\n\n**Bold** and `code`\n\n| A | B |\n| - | - |",
                },
            )
        )

        payload = api.calls[0][1]
        self.assertEqual(message_id, "1")
        self.assertEqual(api.calls[0][0], "sendMessage")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn("<b>Result</b>", payload["text"])
        self.assertIn("<b>Bold</b>", payload["text"])
        self.assertIn("<code>code</code>", payload["text"])
        self.assertIn("<pre>| A | B |", payload["text"])

    def test_agent_markdown_can_opt_into_telegram_rich_message(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api, use_rich_messages=True)
        binding = ChannelBinding("telegram", "bot", "100")

        message_id = asyncio.run(
            adapter.send_view(
                binding,
                {
                    "type": "turn_completed",
                    "message": "## Result\n\n**Bold** and `code`",
                },
            )
        )

        payload = api.calls[0][1]
        self.assertEqual(message_id, "1")
        self.assertEqual(api.calls[0][0], "sendRichMessage")
        self.assertEqual(payload["rich_message"]["markdown"], "## Result\n\n**Bold** and `code`")

    def test_agent_markdown_html_parse_failure_falls_back_to_plain_text(self):
        calls = []

        async def caller(method, payload):
            calls.append((method, dict(payload)))
            if payload.get("parse_mode") == "HTML":
                raise RuntimeError("Bad Request: can't parse entities")
            return {"ok": True, "result": {"message_id": len(calls)}}

        adapter = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=caller))

        message_id = asyncio.run(
            adapter.send_view(
                ChannelBinding("telegram", "bot", "100"),
                {"type": "turn_completed", "message": "## Result\n\n**Bold**"},
            )
        )

        self.assertEqual(message_id, "2")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")
        self.assertNotIn("parse_mode", calls[1][1])
        self.assertEqual(calls[1][1]["text"], "## Result\n\n**Bold**")

    def test_agent_markdown_transient_html_failure_does_not_fallback_duplicate(self):
        calls = []

        async def caller(method, payload):
            calls.append((method, dict(payload)))
            raise TransientDeliveryError("rate limited", retry_after=12.0)

        adapter = TelegramChannelAdapter(TelegramBotApi(token="fake", caller=caller))

        with self.assertRaises(TransientDeliveryError) as raised:
            asyncio.run(
                adapter.send_view(
                    ChannelBinding("telegram", "bot", "100"),
                    {"type": "turn_completed", "message": "## Result\n\n**Bold**"},
                )
            )

        self.assertEqual(raised.exception.retry_after, 12.0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")

    def test_agent_markdown_rich_failure_falls_back_to_html(self):
        calls = []

        async def caller(method, payload):
            calls.append((method, dict(payload)))
            if method == "sendRichMessage":
                raise RuntimeError("Bad Request: rich message unsupported")
            return {"ok": True, "result": {"message_id": len(calls)}}

        adapter = TelegramChannelAdapter(
            TelegramBotApi(token="fake", caller=caller),
            use_rich_messages=True,
        )

        message_id = asyncio.run(
            adapter.send_view(
                ChannelBinding("telegram", "bot", "100"),
                {"type": "turn_completed", "message": "## Result\n\n**Bold**"},
            )
        )

        self.assertEqual(message_id, "2")
        self.assertEqual(calls[0][0], "sendRichMessage")
        self.assertEqual(calls[1][0], "sendMessage")
        self.assertEqual(calls[1][1]["parse_mode"], "HTML")

    def test_get_updates_http_timeout_exceeds_long_poll_timeout(self):
        observed = {}
        original = urllib.request.urlopen

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true, "result": []}'

        def fake_urlopen(request, timeout):
            observed["timeout"] = timeout
            return Response()

        urllib.request.urlopen = fake_urlopen
        try:
            TelegramBotApi("fake")._call_sync("getUpdates", {"timeout": 60})
        finally:
            urllib.request.urlopen = original

        self.assertGreaterEqual(observed["timeout"], 70)

    def test_telegram_http_429_exposes_retry_after_as_transient_delivery(self):
        original = urllib.request.urlopen
        body = b'{"ok":false,"description":"Too Many Requests","parameters":{"retry_after":17}}'

        def fake_urlopen(_request, timeout):
            raise urllib.error.HTTPError(
                url="https://api.telegram.org/botfake/sendMessage",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=io.BytesIO(body),
            )

        urllib.request.urlopen = fake_urlopen
        try:
            with self.assertRaises(TransientDeliveryError) as raised:
                TelegramBotApi("fake")._call_sync("sendMessage", {"chat_id": "1", "text": "hello"})
        finally:
            urllib.request.urlopen = original

        self.assertEqual(raised.exception.retry_after, 17.0)
        self.assertIn("Too Many Requests", str(raised.exception))

    def test_send_chat_action_targets_topic(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api)

        asyncio.run(adapter.send_action(ChannelBinding("telegram", "bot", "100", "77"), "typing"))

        self.assertEqual(api.calls[0][0], "sendChatAction")
        self.assertEqual(
            api.calls[0][1],
            {"chat_id": "100", "action": "typing", "message_thread_id": "77"},
        )

    def test_react_to_message_uses_telegram_reaction_api(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api)

        asyncio.run(adapter.react_to_message(ChannelBinding("telegram", "bot", "100", "77"), "42", "✅"))

        self.assertEqual(api.calls[0][0], "setMessageReaction")
        self.assertEqual(
            api.calls[0][1],
            {
                "chat_id": "100",
                "message_id": 42,
                "reaction": [{"type": "emoji", "emoji": "✅"}],
            },
        )

    def test_install_bot_commands_uses_telegram_native_command_menu(self):
        api = _FakeTelegramApi()
        adapter = TelegramChannelAdapter(api)

        asyncio.run(
            adapter.set_bot_commands(
                [
                    {"command": "status", "description": "Show WalkCode session status"},
                    {"command": "model", "description": "Show or switch model"},
                ]
            )
        )

        self.assertEqual(api.calls[0][0], "setMyCommands")
        self.assertEqual(api.calls[0][1]["commands"][0]["command"], "status")


class TelegramOrchestratorTests(unittest.TestCase):
    def test_private_text_creates_session_and_submits_to_agent_transport(self):
        clock = _Clock()
        api = _FakeTelegramApi()
        channel = TelegramChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        event = channel.parse_update(
            {
                "update_id": 1,
                "message": {
                    "message_id": 10,
                    "chat": {"id": 100, "type": "private"},
                    "from": {"id": 200, "first_name": "Ada"},
                    "text": "ship it",
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
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["ship it"])
        self.assertIn("ok", channel.rendered_text())

    def test_status_card_updates_immediately_after_turn_submit(self):
        clock = _Clock()
        api = _FakeTelegramApi()
        channel = TelegramChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                ChannelBinding(
                    "telegram",
                    "bot",
                    "100",
                    "77",
                    capabilities={"status_card": True},
                ),
                "fake-transport",
                "/tmp/project",
                ActorRef("telegram", "200", "Ada"),
            )
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it"),
                actor=ActorRef("telegram", "200", "Ada"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        sent_texts = [payload["text"] for method, payload in api.calls if method == "sendMessage"]
        edit_texts = [payload["text"] for method, payload in api.calls if method == "editMessageText"]
        self.assertTrue(any("Progress: turn.submitted" in text for text in sent_texts + edit_texts))
        self.assertTrue(any("Progress: turn.completed" in text for text in edit_texts))

    def test_tool_events_update_single_progress_message_without_output_spam(self):
        clock = _Clock()
        api = _FakeTelegramApi()
        channel = TelegramChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TOOL_STARTED, {"tool_id": "t1", "tool_name": "Bash", "summary": "Running command"}),
                AgentEvent(AgentEventType.TOOL_COMPLETED, {"tool_id": "t1", "tool_name": "Bash", "summary": "Command finished", "output": "very long"}),
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"telegram": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                ChannelBinding("telegram", "bot", "100", "77"),
                "fake-transport",
                "/tmp/project",
                ActorRef("telegram", "200", "Ada"),
            )
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it"),
                actor=ActorRef("telegram", "200", "Ada"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
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
        self.assertTrue(any("Tool: Bash" in text for text in edited_tool_cards))
        self.assertFalse(any("very long" in text for text in sent_tool_cards + edited_tool_cards))
        # started + completed share tool_id "t1" → one coalesced line (single
        # block layout), never a residual "RUNNING" line after completion.
        self.assertFalse(any("RUNNING" in text for text in edited_tool_cards))
        # the turn-completed message seals the burst so the next run starts fresh.
        self.assertNotIn("tool_progress_message_id", session.channel_binding.capabilities)
        self.assertNotIn("tool_progress_lines", session.channel_binding.capabilities)


class ClaudeHeadlessTransportTests(unittest.TestCase):
    def test_missing_sdk_disables_capabilities_and_launch_fails_explicitly(self):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: (_ for _ in ()).throw(ModuleNotFoundError("x")))

        self.assertFalse(transport.capabilities().structured_input)
        with self.assertRaises(TransportUnavailable):
            asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

    def test_fake_client_factory_launch_submit_events(self):
        class Client:
            def __init__(self):
                self.submitted = []

            async def submit(self, turn: TurnInput):
                self.submitted.append(turn)

            async def events(self):
                return [AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "done"})]

        client = Client()
        transport = ClaudeHeadlessTransport(client_factory=lambda spec: client)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(transport.events(handle))

        self.assertEqual(client.submitted[0].text, "hello")
        self.assertEqual(events[0].payload["message"], "done")
        self.assertTrue(transport.capabilities().permission_callback)

    def test_option_kwargs_pin_profile_config_dir_via_sdk_env(self):
        transport = ClaudeHeadlessTransport(
            settings="/tmp/vertex.json",
            cli_path="/tmp/claude",
            config_dir="/tmp/claude-profiles/work",
        )

        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

        self.assertEqual(kwargs["cwd"], "/tmp/project")
        self.assertEqual(kwargs["settings"], "/tmp/vertex.json")
        self.assertEqual(kwargs["cli_path"], "/tmp/claude")
        self.assertEqual(kwargs["env"], {"CLAUDE_CONFIG_DIR": "/tmp/claude-profiles/work"})
        self.assertNotIn("resume", kwargs)

    def test_option_kwargs_without_config_dir_do_not_touch_env(self):
        transport = ClaudeHeadlessTransport()

        kwargs = transport._option_kwargs(
            LaunchSpec(cwd="/tmp/project", session_id="s1"), resume_id="r1"
        )

        self.assertNotIn("env", kwargs)
        self.assertEqual(kwargs["resume"], "r1")

    def test_real_sdk_shape_connects_queries_and_converts_messages(self):
        created_options = []
        clients = []

        class TextBlock:
            def __init__(self, text):
                self.text = text

        class AssistantMessage:
            def __init__(self):
                self.content = [TextBlock("working")]
                self.error = None

        class ResultMessage:
            def __init__(self):
                self.is_error = False
                self.result = "done"
                self.session_id = "claude-sdk-session"

        class Options:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                created_options.append(self)

        class Client:
            def __init__(self, options=None, transport=None):
                self.options = options
                self.transport = transport
                self.connected = False
                self.queries = []
                clients.append(self)

            async def connect(self, prompt=None):
                self.connected = True
                self.connect_prompt = prompt

            async def query(self, prompt, session_id="default"):
                self.queries.append((prompt, session_id))

            async def receive_response(self):
                yield AssistantMessage()
                yield ResultMessage()

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(transport.events(handle))

        self.assertEqual(created_options[0].kwargs["cwd"], "/tmp/project")
        self.assertTrue(clients[0].connected)
        self.assertEqual(clients[0].queries, [("hello", "default")])
        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "working")
        self.assertEqual(events[1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[1].payload["message"], "done")
        self.assertEqual(handle.ref["session_id"], "s1")

    def test_real_sdk_shape_converts_tool_use_and_tool_result_messages(self):
        class Client:
            async def connect(self, prompt=None):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                yield {
                    "content": [
                        {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "ls"}},
                    ]
                }
                yield {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "content": "large output"},
                    ]
                }
                yield {"type": "result", "result": "done", "session_id": "claude-sdk-session"}

        class SDK:
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(transport.events(handle))

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "Bash")
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("large output", events[1].payload.get("summary", ""))
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_direct_sdk_tool_blocks_convert_to_tool_lifecycle_events(self):
        class Client:
            async def connect(self, prompt=None):
                return None

            async def query(self, prompt, session_id="default"):
                return None

            async def receive_response(self):
                yield {"type": "server_tool_use", "id": "tool-1", "name": "WebSearch", "input": {"query": "x"}}
                yield {"type": "tool_result", "tool_use_id": "tool-1", "content": "large output"}
                yield {"type": "result", "result": "done"}

        class SDK:
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(transport.events(handle))

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "WebSearch")
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("large output", events[1].payload.get("summary", ""))

    def test_real_sdk_shape_receives_settings_and_cli_path(self):
        created_options = []

        class Options:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                created_options.append(self)

        class Client:
            def __init__(self, options=None, transport=None):
                self.options = options

            async def connect(self, prompt=None):
                return None

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: SDK,
            settings="/tmp/vertex.json",
            cli_path="/tmp/claude",
        )

        asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        self.assertEqual(created_options[0].kwargs["cwd"], "/tmp/project")
        self.assertEqual(created_options[0].kwargs["settings"], "/tmp/vertex.json")
        self.assertEqual(created_options[0].kwargs["cli_path"], "/tmp/claude")

    def test_real_sdk_shape_resume_uses_options_resume(self):
        created_options = []

        class Options:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                created_options.append(self)

        class Client:
            def __init__(self, options=None, transport=None):
                self.options = options
                self.connected = False

            async def connect(self, prompt=None):
                self.connected = True

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(
            transport.resume(
                ResumeSpec(
                    cwd="/tmp/project",
                    session_id="walkcode-session",
                    resume_ref={"agent_session_id": "claude-agent-session"},
                )
            )
        )

        self.assertEqual(created_options[0].kwargs["cwd"], "/tmp/project")
        self.assertEqual(created_options[0].kwargs["resume"], "claude-agent-session")
        self.assertEqual(handle.ref["agent_session_id"], "claude-agent-session")
        self.assertEqual(handle.ref["session_id"], "claude-agent-session")


class ClaudeAddDirsOptionTests(unittest.TestCase):
    def test_download_dir_is_added_as_working_dir_when_options_support_it(self):
        import dataclasses

        from walkcode.channel_native import attachment_download_dir

        created_options = []

        @dataclasses.dataclass
        class Options:
            cwd: str = ""
            add_dirs: list = dataclasses.field(default_factory=list)

            def __post_init__(self):
                created_options.append(self)

        class Client:
            def __init__(self, options=None, transport=None):
                self.options = options

            async def connect(self, prompt=None):
                return None

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)
        asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        self.assertIn(str(attachment_download_dir()), created_options[0].add_dirs)

    def test_add_dirs_skipped_when_options_do_not_declare_it(self):
        created_options = []

        class Options:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                created_options.append(self)

        class Client:
            def __init__(self, options=None, transport=None):
                self.options = options

            async def connect(self, prompt=None):
                return None

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = Client

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)
        asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

        self.assertNotIn("add_dirs", created_options[0].kwargs)


class ClaudePermissionModeOptionTests(unittest.TestCase):
    def test_permission_mode_flows_into_agent_options(self):
        transport = ClaudeHeadlessTransport(permission_mode="acceptEdits")
        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/p", session_id="s1"))
        self.assertEqual(kwargs["permission_mode"], "acceptEdits")

    def test_no_permission_mode_leaves_kwargs_clean(self):
        transport = ClaudeHeadlessTransport()
        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/p", session_id="s1"))
        self.assertNotIn("permission_mode", kwargs)
