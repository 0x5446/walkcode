"""Tests for the ClaudeHeadlessTransport can_use_tool permission bridge.

These cover the real gap wired in this slice: giving the Claude Agent SDK a
``can_use_tool`` callback so a live headless turn floats a
``PERMISSION_REQUESTED`` / ``ASK_USER_REQUESTED`` event mid-turn, blocks the SDK
callback on a Future, and resolves it from a channel decision. A fake SDK stands
in for ``claude_agent_sdk``: it exposes the PermissionResult types the bridge
returns and a scripted streaming client whose ``receive_response`` drives one
tool through the bridge, blocking until the decision arrives.
"""

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from walkcode.channel_native import (
    ActorRef,
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TurnInput,
    _ClaudePermissionBridge,
)


# --- Fake claude_agent_sdk surface -----------------------------------------


class _Allow:
    def __init__(self, updated_input=None, updated_permissions=None):
        self.behavior = "allow"
        self.updated_input = updated_input
        self.updated_permissions = updated_permissions


class _Deny:
    def __init__(self, message="", interrupt=False):
        self.behavior = "deny"
        self.message = message
        self.interrupt = interrupt


class _RuleValue:
    def __init__(self, tool_name, rule_content=None):
        self.tool_name = tool_name
        self.rule_content = rule_content


class _PermissionUpdate:
    def __init__(self, type, rules=None, behavior=None, mode=None, directories=None, destination=None):
        self.type = type
        self.rules = rules
        self.behavior = behavior
        self.mode = mode
        self.directories = directories
        self.destination = destination

    def to_dict(self):
        return {"type": self.type, "behavior": self.behavior, "destination": self.destination}


class _Ctx:
    def __init__(self, tool_use_id, *, suggestions=None, title="", description=""):
        self.tool_use_id = tool_use_id
        self.suggestions = suggestions or []
        self.title = title
        self.description = description


class _Options:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.can_use_tool = kwargs.get("can_use_tool")


def _make_sdk(client_cls):
    class SDK:
        ClaudeAgentOptions = _Options
        ClaudeSDKClient = client_cls
        PermissionResultAllow = _Allow
        PermissionResultDeny = _Deny
        PermissionUpdate = _PermissionUpdate
        PermissionRuleValue = _RuleValue

    return SDK


class _ScriptedClient:
    """Fake streaming client that pushes one tool through can_use_tool.

    ``receive_response`` yields the tool_use block, then invokes the bridged
    ``can_use_tool`` (blocking until the decision resolves) exactly as the real
    turn would, then yields the tool_result + result. A fresh instance is built
    per launch, so the class captures construction args via ``configure``.
    """

    tool_name = "Bash"
    tool_input = {"command": "ls"}
    tool_use_id = "tool-1"
    ctx_suggestions = None

    def __init__(self, options=None):
        self.options = options
        self._can_use_tool = options.can_use_tool if options else None
        self.permission_results = []

    async def connect(self, prompt=None):
        return None

    async def query(self, prompt, session_id="default"):
        return None

    async def receive_response(self):
        yield {
            "content": [
                {
                    "type": "tool_use",
                    "id": self.tool_use_id,
                    "name": self.tool_name,
                    "input": self.tool_input,
                }
            ]
        }
        ctx = _Ctx(tool_use_id=self.tool_use_id, suggestions=self.ctx_suggestions)
        result = await self._can_use_tool(self.tool_name, self.tool_input, ctx)
        self.permission_results.append(result)
        if getattr(result, "behavior", "") == "allow":
            yield {"content": [{"type": "tool_result", "tool_use_id": self.tool_use_id, "content": "ok"}]}
        else:
            yield {
                "content": [
                    {"type": "tool_result", "tool_use_id": self.tool_use_id, "is_error": True, "content": "denied"}
                ]
            }
        yield {"type": "result", "result": "done", "session_id": "claude-x"}


def _client_class(**attrs):
    return type("ScriptedClient", (_ScriptedClient,), dict(attrs))


async def _drive(transport, handle, decide):
    """Iterate the bridged stream, invoking ``decide`` when a request floats."""
    collected = []
    stream = await transport.events(handle)
    async for event in stream:
        collected.append(event)
        if event.type in (AgentEventType.PERMISSION_REQUESTED, AgentEventType.ASK_USER_REQUESTED):
            await decide(event)
    return collected


def _run_turn(transport, decide):
    async def scenario():
        handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
        await transport.submit_turn(handle, TurnInput(text="run"), "k1")
        client = transport._clients[handle.handle_id]
        events = await _drive(transport, handle, decide)
        return handle, client, events

    return asyncio.run(scenario())


class PermissionBridgeStreamTests(unittest.TestCase):
    def test_allow_floats_permission_then_completes_turn(self):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_client_class()))
        captured = {}

        async def decide(event):
            await transport.approve_permission(captured["handle"], event.payload["rid"], {"action": "allow"})

        async def scenario():
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            captured["handle"] = handle
            await transport.submit_turn(handle, TurnInput(text="run"), "k1")
            client = transport._clients[handle.handle_id]
            events = await _drive(transport, handle, decide)
            return client, events

        client, events = asyncio.run(scenario())

        types = [event.type for event in events]
        self.assertIn(AgentEventType.PERMISSION_REQUESTED, types)
        self.assertIn(AgentEventType.TURN_COMPLETED, types)
        # Permission floated before the turn finished.
        self.assertLess(
            types.index(AgentEventType.PERMISSION_REQUESTED),
            types.index(AgentEventType.TURN_COMPLETED),
        )
        perm = next(e for e in events if e.type == AgentEventType.PERMISSION_REQUESTED)
        self.assertEqual(perm.payload["rid"], "tool-1")
        self.assertEqual(perm.payload["tool_name"], "Bash")
        self.assertTrue(perm.payload["high_risk"])
        self.assertEqual(len(client.permission_results), 1)
        self.assertEqual(client.permission_results[0].behavior, "allow")
        self.assertIsNone(client.permission_results[0].updated_input)

    def test_deny_returns_permission_result_deny(self):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_client_class()))
        captured = {}

        async def decide(event):
            await transport.approve_permission(captured["handle"], event.payload["rid"], {"action": "deny"})

        async def scenario():
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            captured["handle"] = handle
            await transport.submit_turn(handle, TurnInput(text="run"), "k1")
            client = transport._clients[handle.handle_id]
            events = await _drive(transport, handle, decide)
            return client, events

        client, events = asyncio.run(scenario())

        self.assertEqual(client.permission_results[0].behavior, "deny")
        self.assertIn(AgentEventType.TURN_COMPLETED, [e.type for e in events])

    def test_timeout_defaults_to_deny_when_no_decision(self):
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: _make_sdk(_client_class()),
            permission_timeout=0.05,
        )

        async def decide(event):
            # Never resolve: the bridge must fall back to deny after the timeout.
            return None

        _handle, client, events = _run_turn(transport, decide)

        self.assertEqual(client.permission_results[0].behavior, "deny")
        self.assertIn(AgentEventType.TURN_COMPLETED, [e.type for e in events])

    def test_always_allow_returns_updates_and_persists_settings(self):
        with TemporaryDirectory() as config_dir:
            settings_path = Path(config_dir) / "settings.json"
            settings_path.write_text(json.dumps({"permissions": {"allow": ["Read"]}}))
            transport = ClaudeHeadlessTransport(
                sdk_loader=lambda: _make_sdk(_client_class()),
                config_dir=config_dir,
            )
            captured = {}

            async def decide(event):
                await transport.approve_permission(
                    captured["handle"], event.payload["rid"], {"action": "always_allow"}
                )

            async def scenario():
                handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
                captured["handle"] = handle
                await transport.submit_turn(handle, TurnInput(text="run"), "k1")
                client = transport._clients[handle.handle_id]
                events = await _drive(transport, handle, decide)
                return client, events

            client, _events = asyncio.run(scenario())

            result = client.permission_results[0]
            self.assertEqual(result.behavior, "allow")
            self.assertTrue(result.updated_permissions)
            self.assertEqual(result.updated_permissions[0].type, "addRules")
            persisted = json.loads(settings_path.read_text())
            self.assertIn("Bash", persisted["permissions"]["allow"])
            self.assertIn("Read", persisted["permissions"]["allow"])

    def test_always_allow_missing_settings_file_is_silently_skipped(self):
        with TemporaryDirectory() as config_dir:
            transport = ClaudeHeadlessTransport(
                sdk_loader=lambda: _make_sdk(_client_class()),
                config_dir=config_dir,
            )
            captured = {}

            async def decide(event):
                await transport.approve_permission(
                    captured["handle"], event.payload["rid"], {"action": "always_allow"}
                )

            async def scenario():
                handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
                captured["handle"] = handle
                await transport.submit_turn(handle, TurnInput(text="run"), "k1")
                client = transport._clients[handle.handle_id]
                events = await _drive(transport, handle, decide)
                return client, events

            client, _events = asyncio.run(scenario())

            self.assertEqual(client.permission_results[0].behavior, "allow")
            self.assertFalse((Path(config_dir) / "settings.json").exists())

    def test_low_risk_tool_is_not_high_risk(self):
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: _make_sdk(_client_class(tool_name="Read", tool_input={"file": "x"}))
        )
        captured = {}

        async def decide(event):
            await transport.approve_permission(captured["handle"], event.payload["rid"], {"action": "allow"})

        async def scenario():
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            captured["handle"] = handle
            await transport.submit_turn(handle, TurnInput(text="run"), "k1")
            events = await _drive(transport, handle, decide)
            return events

        events = asyncio.run(scenario())
        perm = next(e for e in events if e.type == AgentEventType.PERMISSION_REQUESTED)
        self.assertFalse(perm.payload["high_risk"])


class _NoToolClient(_ScriptedClient):
    async def receive_response(self):
        yield {"content": [{"type": "text", "text": "all done"}]}
        yield {"type": "result", "result": "done", "session_id": "claude-x"}


class PermissionBridgePassthroughTests(unittest.TestCase):
    def test_bridge_active_turn_without_permission_completes(self):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_NoToolClient))

        async def decide(event):  # pragma: no cover - never called
            raise AssertionError("no request should float")

        _handle, _client, events = _run_turn(transport, decide)
        types = [e.type for e in events]
        self.assertIn(AgentEventType.TURN_DELTA, types)
        self.assertIn(AgentEventType.TURN_COMPLETED, types)
        self.assertNotIn(AgentEventType.PERMISSION_REQUESTED, types)


class PermissionBridgeUnitTests(unittest.TestCase):
    def test_resolve_is_write_once(self):
        bridge = _ClaudePermissionBridge(sdk=_make_sdk(_ScriptedClient), timeout=5.0)

        async def run():
            task = asyncio.ensure_future(bridge.can_use_tool("Bash", {"c": "ls"}, _Ctx("tool-1")))
            event = await bridge.next_event()
            first = bridge.resolve("tool-1", {"action": "allow"})
            second = bridge.resolve("tool-1", {"action": "deny"})
            result = await task
            return event, first, second, result

        event, first, second, result = asyncio.run(run())
        self.assertEqual(event.type, AgentEventType.PERMISSION_REQUESTED)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(result.behavior, "allow")

    def test_replayed_tool_use_id_is_denied_without_new_event(self):
        bridge = _ClaudePermissionBridge(sdk=_make_sdk(_ScriptedClient), timeout=5.0)

        async def run():
            task = asyncio.ensure_future(bridge.can_use_tool("Bash", {}, _Ctx("tool-1")))
            await bridge.next_event()
            bridge.resolve("tool-1", {"action": "allow"})
            await task
            replay = await bridge.can_use_tool("Bash", {}, _Ctx("tool-1"))
            return replay

        replay = asyncio.run(run())
        self.assertEqual(replay.behavior, "deny")
        self.assertEqual(bridge.drain_ready_events(), [])

    def test_fail_pending_default_deny_unblocks_callback(self):
        bridge = _ClaudePermissionBridge(sdk=_make_sdk(_ScriptedClient), timeout=5.0)

        async def run():
            task = asyncio.ensure_future(bridge.can_use_tool("Bash", {}, _Ctx("tool-1")))
            await bridge.next_event()
            bridge.fail_pending_default_deny(reason="interrupted")
            return await task

        result = asyncio.run(run())
        self.assertEqual(result.behavior, "deny")

    def test_always_allow_uses_ctx_suggestions_when_present(self):
        suggestion = _PermissionUpdate(type="addRules", rules=[_RuleValue("Bash")], behavior="allow")
        bridge = _ClaudePermissionBridge(sdk=_make_sdk(_ScriptedClient), timeout=5.0)

        async def run():
            task = asyncio.ensure_future(
                bridge.can_use_tool("Bash", {}, _Ctx("tool-1", suggestions=[suggestion]))
            )
            await bridge.next_event()
            bridge.resolve("tool-1", {"action": "always_allow"})
            return await task

        result = asyncio.run(run())
        self.assertEqual(result.updated_permissions, [suggestion])


class AskUserQuestionBridgeTests(unittest.TestCase):
    def _run_ask(self, *, tool_input, answers):
        client_cls = _client_class(tool_name="AskUserQuestion", tool_input=tool_input, tool_use_id="ask-1")
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(client_cls))
        captured = {}

        async def decide(event):
            await transport.answer_user_question(captured["handle"], event.payload["rid"], answers)

        async def scenario():
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            captured["handle"] = handle
            await transport.submit_turn(handle, TurnInput(text="run"), "k1")
            client = transport._clients[handle.handle_id]
            events = await _drive(transport, handle, decide)
            return client, events

        return asyncio.run(scenario())

    def test_single_select_maps_answer_into_updated_input(self):
        client, events = self._run_ask(
            tool_input={"questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]},
            answers={0: "A"},
        )
        ask = next(e for e in events if e.type == AgentEventType.ASK_USER_REQUESTED)
        self.assertEqual(ask.payload["questions"][0]["prompt"], "Pick")
        self.assertEqual(ask.payload["questions"][0]["options"], ["A", "B"])
        result = client.permission_results[0]
        self.assertEqual(result.behavior, "allow")
        self.assertEqual(result.updated_input["answers"], {"Pick": "A"})
        self.assertEqual(result.updated_input["questions"], client.tool_input["questions"])

    def test_multi_select_joins_answers_with_comma(self):
        client, _events = self._run_ask(
            tool_input={
                "questions": [
                    {"question": "Pick many", "options": [{"label": "X"}, {"label": "Y"}], "multiSelect": True}
                ]
            },
            answers={0: ["X", "Y"]},
        )
        result = client.permission_results[0]
        self.assertEqual(result.updated_input["answers"], {"Pick many": "X,Y"})

    def test_multi_select_flag_surfaces_in_floated_event(self):
        client, events = self._run_ask(
            tool_input={
                "questions": [
                    {"question": "Pick many", "options": [{"label": "X"}, {"label": "Y"}], "multiSelect": True}
                ]
            },
            answers={0: ["X"]},
        )
        ask = next(e for e in events if e.type == AgentEventType.ASK_USER_REQUESTED)
        self.assertTrue(ask.payload["questions"][0]["allow_multiple"])
        self.assertTrue(ask.payload["questions"][0]["allow_other"])

    def test_other_free_text_answer_is_carried_through(self):
        client, _events = self._run_ask(
            tool_input={"questions": [{"question": "Pick", "options": [{"label": "A"}]}]},
            answers={0: "my own words"},
        )
        result = client.permission_results[0]
        self.assertEqual(result.updated_input["answers"], {"Pick": "my own words"})


class PermissionBridgeOrchestratorTests(unittest.TestCase):
    """End-to-end: floated card -> token callback -> bridge resolve -> turn done."""

    def _channel_caps(self):
        return ChannelCapabilities(
            thread_context=True,
            editable_message=True,
            interactive_message=True,
            interactive_update=True,
            private_callback_ack=True,
            toast_or_ephemeral_notice=True,
            force_reply=True,
            attachment_download=True,
            forum_or_topic=True,
            max_text_chars=4096,
            max_callback_payload_bytes=64,
        )

    def _build(self, client_cls):
        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(client_cls))
        channel = FakeChannelAdapter("telegram", self._channel_caps())
        interactions = InteractionStore()
        authz = AuthorizationStore()
        orch = Orchestrator(
            sessions=SessionRegistry(),
            interactions=interactions,
            outbox=DurableOutbox(),
            channels={"telegram": channel},
            transports={"claude_headless": transport},
            authz=authz,
            defer_event_drain=True,
        )
        return transport, channel, orch

    def _find_view(self, channel, view_type):
        for item in channel.sent_views:
            if item.get("view", {}).get("type") == view_type:
                return item["view"]
        return None

    def _callback(self, token):
        return InboundEvent(
            event_id=f"cb-{token[:6]}",
            channel_kind="telegram",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m-owner",
            root_message_id="root",
            sender_id="owner",
            sender_display="Owner",
            text=f"cb:{token}",
            callback={"token": token},
        )

    def test_permission_card_click_unblocks_and_completes_turn(self):
        async def scenario():
            transport, channel, orch = self._build(_client_class())
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/project", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="run"), actor=owner, generation=session.generation
            )
            card = None
            for _ in range(4000):
                card = self._find_view(channel, "permission_prompt")
                if card is not None:
                    break
                await asyncio.sleep(0)
            self.assertIsNotNone(card, "permission card never floated")
            token = next(a["token"] for a in card["actions"] if a["action"] == "allow")
            await orch.handle_inbound_event(
                self._callback(token), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            handle_id = session.transport_ref["handle_id"]
            client = transport._clients[handle_id]
            return session, client

        session, client = asyncio.run(scenario())
        self.assertEqual(client.permission_results[0].behavior, "allow")
        self.assertEqual(session.lifecycle_state, "IDLE")

    def test_ask_user_card_click_delivers_answer(self):
        client_cls = _client_class(
            tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]},
            tool_use_id="ask-1",
        )

        async def scenario():
            transport, channel, orch = self._build(client_cls)
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/project", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="run"), actor=owner, generation=session.generation
            )
            card = None
            for _ in range(4000):
                card = self._find_view(channel, "ask_user_question")
                if card is not None:
                    break
                await asyncio.sleep(0)
            self.assertIsNotNone(card, "ask_user card never floated")
            # Claude questions always allow a typed answer, so the card is the
            # batch form: select an option (set), then submit the whole card.
            set_token = next(a["token"] for a in card["actions"] if a["action"] == "set:0:0")
            await orch.handle_inbound_event(
                self._callback(set_token), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
            submit_token = card["submit"]["token"]
            await orch.handle_inbound_event(
                self._callback(submit_token), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
            drains = list(orch._background_event_drains)
            if drains:
                await asyncio.wait_for(asyncio.gather(*drains), timeout=5.0)
            client = transport._clients[session.transport_ref["handle_id"]]
            return client

        client = asyncio.run(scenario())
        self.assertEqual(client.permission_results[0].behavior, "allow")
        self.assertEqual(client.permission_results[0].updated_input["answers"], {"Pick": "A"})


class BridgeBypassAndStaleWorkerTests(PermissionBridgeOrchestratorTests):
    """bypassPermissions keeps the ask bridge; stale-worker clicks get feedback."""

    def test_bridge_supported_under_bypass_permissions(self):
        # bypass auto-approves regular tools CLI-side, but the CLI still
        # consults can_use_tool for AskUserQuestion — dropping the bridge
        # there would kill the IM answer loop.
        sdk = _make_sdk(_client_class())
        transport = ClaudeHeadlessTransport(
            sdk_loader=lambda: sdk, permission_mode="bypassPermissions"
        )
        self.assertTrue(transport._permission_bridging_supported(sdk))

    def test_answer_user_question_without_worker_raises_transport_unavailable(self):
        from walkcode.channel_native import TransportHandle, TransportUnavailable

        transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_client_class()))
        handle = TransportHandle(handle_id="gone", transport_kind="claude_headless")
        with self.assertRaises(TransportUnavailable):
            asyncio.run(transport.answer_user_question(handle, "rid-1", {"0": "A"}))
        with self.assertRaises(TransportUnavailable):
            asyncio.run(transport.approve_permission(handle, "rid-1", {"action": "allow"}))

    def test_stale_worker_submit_flips_card_and_notifies(self):
        # A card clicked after a runtime restart: the decision records but the
        # worker (and its Future) died with the old process. The user must see
        # a stale-card flip + a text notice instead of silence.
        client_cls = _client_class(
            tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]},
            tool_use_id="ask-stale",
        )

        async def scenario():
            transport, channel, orch = self._build(client_cls)
            owner = ActorRef("telegram", "owner", "Owner")
            binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
            session = await orch.start_session(binding, "claude_headless", "/tmp/project", owner)
            await orch.submit_user_input(
                session.session_id, TurnInput(text="run"), actor=owner, generation=session.generation
            )
            card = None
            for _ in range(4000):
                card = self._find_view(channel, "ask_user_question")
                if card is not None:
                    break
                await asyncio.sleep(0)
            self.assertIsNotNone(card, "ask_user card never floated")
            set_token = next(a["token"] for a in card["actions"] if a["action"] == "set:0:0")
            await orch.handle_inbound_event(
                self._callback(set_token), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
            # Simulate a runtime restart: in-process worker state is gone.
            transport._bridges.clear()
            transport._clients.clear()
            submit_token = card["submit"]["token"]
            result = await orch.handle_inbound_event(
                self._callback(submit_token), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
            return channel, result

        channel, result = asyncio.run(scenario())
        self.assertFalse(result.accepted)
        stale_flip = None
        notice = None
        for item in channel.sent_views:
            view = item.get("view", {})
            if view.get("type") == "decision_result" and view.get("action") == "stale":
                stale_flip = item
            if view.get("type") == "text" and "已失效" in str(view.get("text", "")) or (
                view.get("type") == "text" and "重启" in str(view.get("text", ""))
            ):
                notice = view
        self.assertIsNotNone(stale_flip, "stale decision_result flip was not sent")
        self.assertTrue(stale_flip.get("edited"), "stale flip must edit the clicked card")
        self.assertIsNotNone(notice, "user-facing restart notice was not sent")


if __name__ == "__main__":
    unittest.main()
