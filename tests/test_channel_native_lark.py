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
from walkcode import channel_native_runtime as runtime_module


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


class _LarkRuntimeHarness(unittest.TestCase):
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


class LarkRuntimeTests(_LarkRuntimeHarness):
    def test_plain_message_creates_session_rooted_at_status_card(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(runtime.process_lark_event(self._message_payload()))

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["run tests"])
        # thread root = bot-sent status card, user's text forwarded as first reply
        self.assertEqual(api.calls[0][0], "sendCard")
        self.assertEqual(api.calls[0][1]["view"]["type"], "health")
        self.assertEqual(api.calls[0][1]["root_id"], "")
        forward = api.calls[1]
        self.assertEqual(forward[0], "sendMessage")
        self.assertEqual(forward[1]["root_id"], "lark-msg-1")
        self.assertIn("run tests", forward[1]["text"])
        sessions = runtime.state.sessions.list_sessions(channel_kind="lark")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].root_message_id, "lark-msg-1")
        session = runtime.state.sessions.get(sessions[0].session_id)
        self.assertEqual(session.channel_binding.health_message_id, "lark-msg-1")
        # lifecycle refreshes patch the root card instead of sending a second one
        edits = [p for m, p in api.calls if m == "editCard"]
        self.assertTrue(edits)
        self.assertTrue(all(p["message_id"] == "lark-msg-1" for p in edits))
        self.assertEqual(len([m for m, _ in api.calls if m == "sendCard"]), 1)

    def test_thread_reply_to_status_card_continues_session(self):
        runtime, api, transport = self._runtime(
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {"message": "done", "agent_session_id": "agent-abc"},
                )
            ]
        )
        asyncio.run(runtime.process_lark_event(self._message_payload()))

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(
                    event_id="evt-2",
                    message_id="om_reply",
                    root_id="lark-msg-1",
                    text="follow up",
                )
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [turn.text for turn in transport.submitted_turns], ["run tests", "follow up"]
        )
        self.assertEqual(len(runtime.state.sessions.list_sessions(channel_kind="lark")), 1)

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


class WorkspaceResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "workspace"
        (self.root / "proj-a").mkdir(parents=True)
        (self.root / "proj-b").mkdir()
        self.outside = Path(self._tmp.name) / "outside"
        self.outside.mkdir()

    def test_bare_name_resolves_under_root(self):
        resolved, reason = runtime_module._resolve_workspace_target("proj-a", (str(self.root),))

        self.assertEqual(reason, "")
        self.assertEqual(resolved, str((self.root / "proj-a").resolve()))

    def test_dotdot_escape_is_rejected(self):
        resolved, reason = runtime_module._resolve_workspace_target(
            "proj-a/../../outside", (str(self.root),)
        )

        self.assertIsNone(resolved)
        self.assertIn("outside", reason)

    def test_symlink_escape_is_rejected(self):
        link = self.root / "sneaky"
        link.symlink_to(self.outside)

        resolved, reason = runtime_module._resolve_workspace_target("sneaky", (str(self.root),))

        self.assertIsNone(resolved)

    def test_absolute_path_inside_root_is_accepted(self):
        resolved, reason = runtime_module._resolve_workspace_target(
            str(self.root / "proj-b"), (str(self.root),)
        )

        self.assertEqual(resolved, str((self.root / "proj-b").resolve()))

    def test_missing_directory_is_rejected_with_reason(self):
        resolved, reason = runtime_module._resolve_workspace_target("nope", (str(self.root),))

        self.assertIsNone(resolved)
        self.assertIn("nope", reason)


class RepoCommandTests(_LarkRuntimeHarness):
    def _workspace(self):
        root = Path(self._tmp.name) / "workspace"
        (root / "proj-a").mkdir(parents=True, exist_ok=True)
        return root

    def test_repo_command_starts_session_in_resolved_directory(self):
        root = self._workspace()
        runtime, api, transport = self._runtime(
            env_extra={"WALKCODE_WORKSPACE_ROOTS": str(root)}
        )

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/repo proj-a 修复登录bug"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual([turn.text for turn in transport.submitted_turns], ["修复登录bug"])
        sessions = runtime.state.sessions.list_sessions(channel_kind="lark")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].cwd, str((root / "proj-a").resolve()))

    def test_repo_command_without_roots_is_rejected(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/repo proj-a 做点事"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "repo_command_rejected")
        self.assertEqual(transport.submitted_turns, [])
        self.assertIn("WALKCODE_WORKSPACE_ROOTS", api.calls[-1][1]["view"]["text"])

    def test_repo_command_without_task_text_shows_usage(self):
        root = self._workspace()
        runtime, api, transport = self._runtime(
            env_extra={"WALKCODE_WORKSPACE_ROOTS": str(root)}
        )

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/repo proj-a"))
        )

        self.assertEqual(result.reason, "repo_command_usage")
        self.assertEqual(transport.submitted_turns, [])

    def test_repo_command_with_bad_target_lists_allowlist(self):
        root = self._workspace()
        runtime, api, transport = self._runtime(
            env_extra={"WALKCODE_WORKSPACE_ROOTS": str(root)}
        )

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/repo ../etc 干活"))
        )

        self.assertEqual(result.reason, "repo_command_rejected")
        self.assertEqual(transport.submitted_turns, [])
        self.assertIn("proj-a", api.calls[-1][1]["view"]["text"])

    def test_repo_command_inside_existing_session_is_rejected(self):
        root = self._workspace()
        runtime, api, transport = self._runtime(
            env_extra={"WALKCODE_WORKSPACE_ROOTS": str(root)}
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="第一条任务")))

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(
                    event_id="evt-2",
                    message_id="om_reply",
                    root_id="lark-msg-1",
                    text="/repo proj-a 换个目录",
                )
            )
        )

        self.assertEqual(result.reason, "repo_command_rejected")
        self.assertEqual(len(transport.submitted_turns), 1)


class LarkTuiObservationTests(_LarkRuntimeHarness):
    @staticmethod
    def _tui_payload(tmp, **extra):
        payload = {
            "session_id": "claude-session-1",
            "cwd": tmp,
            "terminate_ref": {
                "controller_kind": "process",
                "process_ref": {"pid": 123, "allow_terminate": True},
            },
        }
        payload.update(extra)
        return payload

    def test_tui_hook_creates_observed_lark_session_rooted_at_notice(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_CHAT_IDS": "oc_chat"}
        )

        created = asyncio.run(
            runtime.process_tui_hook(
                hook_type="sync", agent="claude", payload=self._tui_payload(self._tmp.name)
            )
        )
        stopped = asyncio.run(
            runtime.process_tui_hook(
                hook_type="stop",
                agent="claude",
                payload=self._tui_payload(self._tmp.name, message="finished from TUI"),
            )
        )

        self.assertTrue(created.accepted)
        self.assertTrue(stopped.accepted)
        root_call = api.calls[0]
        self.assertEqual(root_call[0], "sendMessage")
        self.assertEqual(root_call[1]["chat_id"], "oc_chat")
        self.assertIn("👀 TUI: ", root_call[1]["text"])
        summaries = runtime.state.sessions.list_sessions(channel_kind="lark")
        self.assertEqual(len(summaries), 1)
        session = runtime.state.sessions.get(summaries[0].session_id)
        self.assertEqual(session.channel_binding.chat_id, "oc_chat")
        self.assertEqual(session.channel_binding.root_message_id, "lark-msg-1")
        self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
        self.assertEqual(session.writer_owner.kind, "external_tui")
        forwarded = [p for m, p in api.calls if m == "sendMessage" and p.get("text") == "finished from TUI"]
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0]["root_id"], "lark-msg-1")

    def test_tui_hook_without_lark_chat_raises_config_error(self):
        runtime, api, transport = self._runtime()

        with self.assertRaisesRegex(
            runtime_module.ChannelConfigError, "WALKCODE_LARK_TUI_CHAT_ID"
        ):
            asyncio.run(
                runtime.process_tui_hook(
                    hook_type="sync", agent="claude", payload=self._tui_payload(self._tmp.name)
                )
            )

    def test_explicit_tui_chat_id_beats_allowlist(self):
        runtime, api, transport = self._runtime(
            env_extra={
                "LARK_ALLOWED_CHAT_IDS": "oc_a,oc_b",
                "WALKCODE_LARK_TUI_CHAT_ID": "oc_tui",
            }
        )

        created = asyncio.run(
            runtime.process_tui_hook(
                hook_type="sync", agent="claude", payload=self._tui_payload(self._tmp.name)
            )
        )

        self.assertTrue(created.accepted)
        session = runtime.state.sessions.get(
            runtime.state.sessions.list_sessions(channel_kind="lark")[0].session_id
        )
        self.assertEqual(session.channel_binding.chat_id, "oc_tui")


class LarkTuiTakeoverAuthzTests(_LarkRuntimeHarness):
    def test_observed_session_grants_owner_from_allowed_open_ids(self):
        runtime, api, transport = self._runtime(
            env_extra={
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
                "LARK_ALLOWED_OPEN_IDS": "ou_owner",
            }
        )

        asyncio.run(
            runtime.process_tui_hook(
                hook_type="sync",
                agent="claude",
                payload={
                    "session_id": "claude-session-9",
                    "cwd": self._tmp.name,
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {"pid": 123, "allow_terminate": True},
                    },
                },
            )
        )

        session_id = runtime.state.sessions.list_sessions(channel_kind="lark")[0].session_id
        from walkcode.channel_native import ActorRef
        result = runtime.state.authz.can_submit(
            session_id, ActorRef("lark", "ou_owner", "ou_owner")
        )
        self.assertTrue(result.allowed)

    def test_observed_session_message_from_owner_blocks_input_and_prompts_takeover(self):
        runtime, api, transport = self._runtime(
            env_extra={
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
                "LARK_ALLOWED_OPEN_IDS": "ou_user",
            }
        )
        asyncio.run(
            runtime.process_tui_hook(
                hook_type="sync",
                agent="claude",
                payload={
                    "session_id": "claude-session-9",
                    "cwd": self._tmp.name,
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {"pid": 123, "allow_terminate": True},
                    },
                },
            )
        )
        session_id = runtime.state.sessions.list_sessions(channel_kind="lark")[0].session_id
        session = runtime.state.sessions.get(session_id)
        root = session.channel_binding.root_message_id

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(
                    event_id="evt-to",
                    message_id="om_to_input",
                    root_id=root,
                    text="改成用三句话介绍",
                )
            )
        )

        self.assertTrue(result.blocked_input_id)
        session = runtime.state.sessions.get(session_id)
        self.assertTrue(session.blocked_inputs)
        prompts = [
            p["view"] for m, p in api.calls
            if m == "sendCard" and p.get("view", {}).get("type") == "takeover_prompt"
        ]
        self.assertEqual(len(prompts), 1)
        self.assertTrue(prompts[0]["actions"])

    def test_binding_refresh_regrants_owners_for_loaded_lark_sessions(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_CHAT_IDS": "oc_chat"}
        )
        asyncio.run(
            runtime.process_tui_hook(
                hook_type="sync",
                agent="claude",
                payload={
                    "session_id": "claude-session-9",
                    "cwd": self._tmp.name,
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {"pid": 999999, "allow_terminate": True},
                    },
                },
            )
        )
        session_id = runtime.state.sessions.list_sessions(channel_kind="lark")[0].session_id
        from walkcode.channel_native import ActorRef
        self.assertFalse(
            runtime.state.authz.can_submit(session_id, ActorRef("lark", "ou_late", "ou_late")).allowed
        )

        # simulate config gaining the open id afterwards + service restart refresh
        options = dict(runtime.config.channel.options)
        options["allowed_open_ids"] = ("ou_late",)
        object.__setattr__(runtime.config.channel, "options", options)
        runtime._loaded_tui_observed_bindings_refreshed = False
        session = runtime.state.sessions.get(session_id)
        session.transport_ref.pop("terminate_ref", None)
        asyncio.run(runtime._refresh_loaded_tui_observed_bindings())

        self.assertTrue(
            runtime.state.authz.can_submit(session_id, ActorRef("lark", "ou_late", "ou_late")).allowed
        )


class LarkStatusCardCallbackTests(unittest.TestCase):
    def test_tokenless_card_action_routes_by_action_name(self):
        adapter = LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))

        event = adapter.parse_event(
            {
                "event_id": "evt-cb",
                "event": {
                    "message_id": "om_card",
                    "chat_id": "oc_chat",
                    "open_id": "ou_user",
                    "action": {"value": {"action": "request_takeover"}},
                },
            }
        )

        self.assertEqual(event.callback["data"], "request_takeover")
        self.assertEqual(event.callback["token"], "")

    def test_tokened_card_action_keeps_token_as_data(self):
        adapter = LarkChannelAdapter(LarkBotApi(caller=lambda *_: {}))

        event = adapter.parse_event(
            {
                "event_id": "evt-cb2",
                "event": {
                    "message_id": "om_card",
                    "chat_id": "oc_chat",
                    "action": {"value": {"token": "tok-1", "action": "allow_once"}},
                },
            }
        )

        self.assertEqual(event.callback["data"], "tok-1")


class LarkSlashPassthroughTests(_LarkRuntimeHarness):
    def test_double_slash_bypasses_walkcode_command_and_reaches_agent(self):
        runtime, api, transport = self._runtime(
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {"message": "ok", "agent_session_id": "agent-1"},
                )
            ]
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="先建个会话")))

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(
                    event_id="evt-2",
                    message_id="om_slash",
                    root_id="lark-msg-1",
                    text="//model",
                )
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [t.text for t in transport.submitted_turns], ["先建个会话", "/model"]
        )

    def test_unknown_slash_inside_session_passes_through_verbatim(self):
        runtime, api, transport = self._runtime(
            scripted_events=[
                AgentEvent(
                    AgentEventType.TURN_COMPLETED,
                    {"message": "ok", "agent_session_id": "agent-1"},
                )
            ]
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="先建个会话")))

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(
                    event_id="evt-3",
                    message_id="om_slash2",
                    root_id="lark-msg-1",
                    text="/compact",
                )
            )
        )

        self.assertTrue(result.accepted)
        self.assertIn("/compact", [t.text for t in transport.submitted_turns])

    def test_walkcode_command_still_intercepted_without_double_slash(self):
        runtime, api, transport = self._runtime()

        result = asyncio.run(
            runtime.process_lark_event(self._message_payload(text="/status"))
        )

        self.assertTrue(result.accepted)
        self.assertEqual(transport.submitted_turns, [])


class LarkModelChoiceCardTests(_LarkRuntimeHarness):
    def _runtime_with_models(self, settings_models):
        import json as _json
        from pathlib import Path as _Path
        cdir = _Path(self._tmp.name) / "claude-cfg"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "settings.json").write_text(_json.dumps({"env": settings_models}))
        return self._runtime(
            env_extra={
                "LARK_ALLOWED_CHAT_IDS": "oc_chat",
                "LARK_ALLOWED_OPEN_IDS": "ou_user",
                "WALKCODE_CLAUDE_CONFIG_DIR": str(cdir),
            },
            scripted_events=[
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok", "agent_session_id": "a1"})
            ],
        )

    def test_slash_model_sends_choice_card_and_click_switches(self):
        runtime, api, transport = self._runtime_with_models(
            {"ANTHROPIC_MODEL": "opus", "ANTHROPIC_SMALL_FAST_MODEL": "haiku"}
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="建会话")))
        api.calls.clear()

        result = asyncio.run(
            runtime.process_lark_event(
                self._message_payload(event_id="evt-m", message_id="om_m", root_id="lark-msg-1", text="/model")
            )
        )
        self.assertTrue(result.accepted)
        cards = [p for m, p in api.calls if m == "sendCard" and p.get("view", {}).get("type") == "model_choice"]
        self.assertEqual(len(cards), 1)
        actions = cards[0]["view"]["actions"]
        self.assertEqual([a["action"] for a in actions], ["opus", "haiku"])
        token = next(a["token"] for a in actions if a["action"] == "haiku")

        api.calls.clear()
        click = asyncio.run(
            runtime.process_lark_event(
                {
                    "event_id": "evt-click",
                    "event": {
                        "message_id": "om_m",
                        "chat_id": "oc_chat",
                        "open_id": "ou_user",
                        "root_id": "lark-msg-1",
                        "action": {"value": {"token": token, "action": "haiku"}},
                    },
                }
            )
        )
        self.assertTrue(click.accepted)
        self.assertEqual(transport.model_calls[-1], "haiku")
        confirms = [p for m, p in api.calls if m == "sendMessage" and "haiku" in p.get("view", {}).get("text", "")]
        self.assertTrue(confirms)

    def test_slash_model_without_models_falls_back_to_text(self):
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_CHAT_IDS": "oc_chat"},
            scripted_events=[
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok", "agent_session_id": "a1"})
            ],
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="建会话")))
        api.calls.clear()

        asyncio.run(
            runtime.process_lark_event(
                self._message_payload(event_id="evt-m", message_id="om_m", root_id="lark-msg-1", text="/model")
            )
        )
        cards = [p for m, p in api.calls if p.get("view", {}).get("type") == "model_choice"]
        self.assertEqual(cards, [])
        texts = [p for m, p in api.calls if p.get("view", {}).get("type") == "text"]
        self.assertTrue(texts)


class LarkPermissionCardFlipTests(_LarkRuntimeHarness):
    def test_permission_decision_flips_card_to_result(self):
        from walkcode.channel_native import ViewModelFactory
        runtime, api, transport = self._runtime(
            env_extra={"LARK_ALLOWED_CHAT_IDS": "oc_chat", "LARK_ALLOWED_OPEN_IDS": "ou_user"},
            scripted_events=[
                AgentEvent(AgentEventType.TURN_COMPLETED, {"message": "ok", "agent_session_id": "a1"})
            ],
        )
        asyncio.run(runtime.process_lark_event(self._message_payload(text="建会话")))
        session = runtime.state.sessions.get(
            runtime.state.sessions.list_sessions(channel_kind="lark")[0].session_id
        )
        # register a permission interaction + token as the orchestrator would
        store = runtime.orchestrator.interactions
        ctx = store.register_permission(
            session_id=session.session_id,
            generation=session.generation,
            tool_name="Bash",
            tool_input={"command": "gcloud auth ..."},
            actions=["allow", "always_allow", "deny"],
            high_risk=True,
        )
        token = store.create_callback_token(ctx.interaction_id, "always_allow", generation=session.generation)
        api.calls.clear()

        result = asyncio.run(
            runtime.process_lark_event(
                {
                    "event_id": "evt-perm",
                    "event": {
                        "message_id": "om_permcard",
                        "chat_id": "oc_chat",
                        "open_id": "ou_user",
                        "root_id": session.channel_binding.root_message_id,
                        "action": {"value": {"token": token, "action": "always_allow"}},
                    },
                }
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(transport.permission_approval_calls[-1][1].get("action"), "always_allow")
        patches = [
            p for m, p in api.calls
            if m == "editCard" and p.get("message_id") == "om_permcard"
            and p.get("view", {}).get("type") == "decision_result"
        ]
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["view"]["action"], "always_allow")
