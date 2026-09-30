import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ServerToolResultBlock,
    ServerToolUseBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    ChannelBinding,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeAgentTransport,
    InteractionStore,
    LaunchSpec,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    LarkBotApi,
    LarkChannelAdapter,
    TransportCapabilities,
    TransportUnavailable,
    TurnInput,
)


def _sdk_result(message="done", session_id="claude-sdk-session"):
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session_id,
        result=message,
    )


def _sdk_stream_client_class(messages):
    """ClaudeSDKClient stand-in with the real client surface: receive_messages
    yields the given typed SDK messages, then ends (worker EOF)."""

    class Client:
        instances: list = []

        def __init__(self, options=None):
            self.options = options
            self.connected = False
            self.connect_prompt = "unset"
            self.disconnected = False
            self.queries = []
            type(self).instances.append(self)

        async def connect(self, prompt=None):
            self.connected = True
            self.connect_prompt = prompt

        async def query(self, prompt, session_id="default"):
            self.queries.append((prompt, session_id))

        async def receive_messages(self):
            for message in messages:
                yield message

        async def disconnect(self):
            self.disconnected = True

    return Client


async def _drain_events(transport, handle):
    stream = await transport.events(handle)
    return [event async for event in stream]


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


_SEND_METHODS = {"sendMessage", "sendCard"}


def _texts(api, methods) -> list[str]:
    return [payload["text"] for method, payload in api.calls if method in methods]


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


class LarkOrchestratorTests(unittest.TestCase):
    def test_root_text_creates_session_and_submits_to_agent_transport(self):
        clock = _Clock()
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
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
                        "message_id": "om_10",
                        "chat_id": "oc_100",
                        "message_type": "text",
                        "content": json.dumps({"text": "ship it"}),
                    },
                    "sender": {"sender_id": {"open_id": "ou_200"}},
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
        self.assertIn("ok", "\n".join(_texts(api, _SEND_METHODS)))

    def test_status_card_updates_immediately_after_turn_submit(self):
        clock = _Clock()
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok"})],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"lark": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                ChannelBinding(
                    "lark",
                    "bot",
                    "oc_100",
                    "om_77",
                    capabilities={"status_card": True},
                ),
                "fake-transport",
                "/tmp/project",
                ActorRef("lark", "ou_200", "Ada"),
            )
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it"),
                actor=ActorRef("lark", "ou_200", "Ada"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        sent_texts = _texts(api, _SEND_METHODS)
        edit_texts = _texts(api, {"editCard"})
        self.assertTrue(any("Progress: turn.submitted" in text for text in sent_texts + edit_texts))
        self.assertTrue(any("Progress: turn.completed" in text for text in edit_texts))

    def test_tool_events_update_single_progress_message_without_output_spam(self):
        clock = _Clock()
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
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
            channels={"lark": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                ChannelBinding("lark", "bot", "oc_100", "om_77"),
                "fake-transport",
                "/tmp/project",
                ActorRef("lark", "ou_200", "Ada"),
            )
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it"),
                actor=ActorRef("lark", "ou_200", "Ada"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        sent_tool_cards = [
            payload["text"]
            for method, payload in api.calls
            if method in _SEND_METHODS and "Agent activity" in payload["text"]
        ]
        edited_tool_cards = [
            payload["text"]
            for method, payload in api.calls
            if method == "editCard" and "Agent activity" in payload["text"]
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

    def test_empty_turn_completion_still_seals_tool_progress(self):
        clock = _Clock()
        api = _FakeLarkApi()
        channel = LarkChannelAdapter(api)
        transport = FakeAgentTransport(
            "fake-transport",
            _transport_caps(),
            scripted_events=[
                AgentEvent(AgentEventType.TOOL_STARTED, {"tool_id": "t1", "tool_name": "Bash", "summary": "ls"}),
                AgentEvent(AgentEventType.TOOL_COMPLETED, {"tool_id": "t1", "tool_name": "Bash", "summary": "done"}),
                # empty completion message renders no visible text — the burst
                # must still be sealed or the next turn edits this turn's card
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": ""}),
            ],
        )
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"lark": channel},
            transports={"fake-transport": transport},
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                ChannelBinding("lark", "bot", "oc_100", "om_77"),
                "fake-transport",
                "/tmp/project",
                ActorRef("lark", "ou_200", "Ada"),
            )
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="go"),
                actor=ActorRef("lark", "ou_200", "Ada"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertNotIn("tool_progress_message_id", session.channel_binding.capabilities)
        self.assertNotIn("tool_progress_lines", session.channel_binding.capabilities)


class ClaudeHeadlessTransportTests(unittest.TestCase):
    def test_missing_sdk_disables_capabilities_and_launch_fails_explicitly(self):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: (_ for _ in ()).throw(ModuleNotFoundError("x")))

        self.assertFalse(transport.capabilities().structured_input)
        with self.assertRaises(TransportUnavailable):
            asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))

    def test_fake_client_factory_launch_submit_events(self):
        client = _sdk_stream_client_class([_sdk_result("done")])()
        transport = ClaudeHeadlessTransport(client_factory=lambda spec: client)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(_drain_events(transport, handle))

        self.assertEqual(client.queries, [("hello", "default")])
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

    def test_option_kwargs_config_dir_and_anthropic_base_url_combine(self):
        # With a config_dir, the override merges the profile settings.json env
        # (confirmed live: the --settings env map REPLACES the profile env map
        # wholesale, so an override without the profile's ANTHROPIC_API_KEY
        # fails "Not logged in") and goes through a 0600 file so secrets never
        # reach argv.
        with tempfile.TemporaryDirectory() as config_dir:
            (Path(config_dir) / "settings.json").write_text(
                json.dumps({"env": {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CODE_USE_VERTEX": "1"}, "model": "opus"}),
                encoding="utf-8",
            )
            transport = ClaudeHeadlessTransport(
                config_dir=config_dir,
                anthropic_base_url="http://127.0.0.1:18899",
            )

            kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

            self.assertEqual(kwargs["env"], {"CLAUDE_CONFIG_DIR": config_dir})
            override_path = Path(kwargs["settings"])
            self.assertEqual(override_path, Path(config_dir) / "walkcode-tap-override-settings.json")
            self.assertEqual(override_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(override_path.read_text(encoding="utf-8")),
                {
                    "env": {
                        "ANTHROPIC_API_KEY": "sk-test",
                        "CLAUDE_CODE_USE_VERTEX": "1",
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                        "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:18899",
                    }
                },
            )

    def test_option_kwargs_config_dir_without_settings_json_writes_override_file(self):
        # OAuth-style profiles have no settings.json env; the override file
        # then carries only the base URLs.
        with tempfile.TemporaryDirectory() as config_dir:
            transport = ClaudeHeadlessTransport(
                config_dir=config_dir,
                anthropic_base_url="http://127.0.0.1:18899",
            )

            kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

            self.assertEqual(
                json.loads(Path(kwargs["settings"]).read_text(encoding="utf-8")),
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                        "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:18899",
                    }
                },
            )

    def test_option_kwargs_corrupt_profile_settings_fails_loud(self):
        # A profile whose settings.json cannot be parsed must not silently run
        # with a partial env override (that would drop its auth and every turn
        # would fail with a misleading "Not logged in").
        with tempfile.TemporaryDirectory() as config_dir:
            (Path(config_dir) / "settings.json").write_text("{not json", encoding="utf-8")
            transport = ClaudeHeadlessTransport(
                config_dir=config_dir,
                anthropic_base_url="http://127.0.0.1:18899",
            )

            with self.assertRaisesRegex(TransportUnavailable, "unreadable or invalid JSON"):
                transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

    def test_option_kwargs_anthropic_base_url_only(self):
        # Plain options.env is confirmed (via CLAUDE_CONFIG_DIR) to reach the
        # subprocess, but Claude Code still applies this profile's own
        # settings.json (loaded from CLAUDE_CONFIG_DIR) env block with higher
        # priority than inherited process env for the provider base URL —
        # verified live: zero claude-tap trace records with an env-only
        # override against a real Vertex-routed profile. --settings is the
        # layer Claude Code actually honors.
        transport = ClaudeHeadlessTransport(anthropic_base_url="http://127.0.0.1:18899")

        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

        self.assertNotIn("env", kwargs)
        self.assertEqual(
            json.loads(kwargs["settings"]),
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                    "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:18899",
                }
            },
        )

    def test_option_kwargs_anthropic_base_url_overrides_vertex_regardless_of_process_env(self):
        # The Vertex switch may live only in the profile's settings.json —
        # invisible to this runtime's process env (a launchd-run serve has
        # neither; confirmed live: an env-gated variant silently bypassed the
        # proxy for Vertex-routed profiles). Both variables must be set no
        # matter what the process env says.
        transport = ClaudeHeadlessTransport(anthropic_base_url="http://127.0.0.1:18899")

        expected = {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:18899",
            }
        }
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_USE_VERTEX": "1"}):
            kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))
        self.assertEqual(json.loads(kwargs["settings"]), expected)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_USE_VERTEX", None)
            kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))
        self.assertEqual(json.loads(kwargs["settings"]), expected)

    def test_option_kwargs_anthropic_base_url_ignores_settings_field(self):
        # WALKCODE_CLAUDE_SETTINGS + WALKCODE_CLAUDE_ANTHROPIC_BASE_URL together
        # is rejected at config-parse time (_configured_agent_options); at this
        # layer anthropic_base_url simply takes priority and self.settings is
        # not read/merged — merging previously meant re-serializing that
        # file's content (possibly including secrets) into this process'
        # argv, and silently dropping it on any read/parse failure.
        transport = ClaudeHeadlessTransport(
            settings='{"env": {"OTHER_KEY": "keep-me"}, "model": "opus"}',
            anthropic_base_url="http://127.0.0.1:18899",
        )

        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

        self.assertEqual(
            json.loads(kwargs["settings"]),
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:18899",
                    "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:18899",
                }
            },
        )

    def test_option_kwargs_settings_path_without_anthropic_base_url_passthrough(self):
        transport = ClaudeHeadlessTransport(settings="/tmp/vertex.json")

        kwargs = transport._option_kwargs(LaunchSpec(cwd="/tmp/project", session_id="s1"))

        self.assertEqual(kwargs["settings"], "/tmp/vertex.json")

    def test_real_sdk_shape_connects_queries_and_converts_messages(self):
        created_options = []

        class Options:
            def __init__(self, **kwargs):
                self.kwargs = dict(kwargs)
                created_options.append(self)

        client_cls = _sdk_stream_client_class(
            [
                AssistantMessage(content=[TextBlock(text="working")], model="claude-test"),
                _sdk_result("done", session_id="claude-sdk-session"),
            ]
        )

        class SDK:
            ClaudeAgentOptions = Options
            ClaudeSDKClient = client_cls

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(_drain_events(transport, handle))

        client = client_cls.instances[0]
        self.assertEqual(created_options[0].kwargs["cwd"], "/tmp/project")
        self.assertTrue(client.connected)
        self.assertIsNone(client.connect_prompt)
        self.assertEqual(client.queries, [("hello", "default")])
        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "working")
        self.assertEqual(events[1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[1].payload["message"], "done")
        self.assertEqual(handle.ref["session_id"], "s1")
        # Worker EOF ends the session-level listener and closes the client.
        self.assertTrue(client.disconnected)

    def test_sdk_messages_carry_model_and_usage(self):
        assistant = AssistantMessage(content=[TextBlock(text="working")], model="claude-opus-4-8-20260610")
        result = ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid",
            result="done",
            usage={"input_tokens": 10, "output_tokens": 3},
        )

        deltas = ClaudeHeadlessTransport._convert_sdk_message(assistant)
        self.assertEqual(deltas[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(deltas[0].payload["model"], "claude-opus-4-8-20260610")

        completed = ClaudeHeadlessTransport._convert_sdk_message(result)
        self.assertEqual(completed[0].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(completed[0].payload["usage"], {"input_tokens": 10, "output_tokens": 3})
        self.assertNotIn("model", completed[0].payload)

    def test_real_sdk_shape_converts_tool_use_and_tool_result_messages(self):
        client_cls = _sdk_stream_client_class(
            [
                AssistantMessage(
                    content=[ToolUseBlock(id="tool-1", name="Bash", input={"command": "ls"})],
                    model="claude-test",
                ),
                UserMessage(content=[ToolResultBlock(tool_use_id="tool-1", content="large output")]),
                _sdk_result("done", session_id="claude-sdk-session"),
            ]
        )

        class SDK:
            ClaudeSDKClient = client_cls

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(_drain_events(transport, handle))

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "Bash")
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("large output", events[1].payload.get("summary", ""))
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_server_tool_blocks_convert_to_tool_lifecycle_events(self):
        client_cls = _sdk_stream_client_class(
            [
                AssistantMessage(
                    content=[ServerToolUseBlock(id="tool-1", name="web_search", input={"query": "x"})],
                    model="claude-test",
                ),
                AssistantMessage(
                    content=[ServerToolResultBlock(tool_use_id="tool-1", content={"type": "web_search_tool_result"})],
                    model="claude-test",
                ),
                _sdk_result("done"),
            ]
        )

        class SDK:
            ClaudeSDKClient = client_cls

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: SDK)

        handle = asyncio.run(transport.launch_session(cwd="/tmp/project", session_id="s1"))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "k1"))
        events = asyncio.run(_drain_events(transport, handle))

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "web_search")
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("web_search_tool_result", events[1].payload.get("summary", ""))

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
