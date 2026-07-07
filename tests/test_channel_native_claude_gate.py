"""PreToolUse gate (ADR 0046 v2) — decision spool, blocking hook, drain, daemon patch semantics."""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ActorRef,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelNativeConfig,
    TransportHandle,
    TransportUnavailable,
)
from walkcode.channel_native import claude_gate
from walkcode.channel_native import claude_daemon as claude_daemon_mod
from walkcode.channel_native.claude_daemon import ClaudeDaemonTransport
from walkcode.channel_native_runtime import ChannelNativeRuntime


AGENT_SESSION_ID = "5ca3e37c-1111-2222-3333-444455556666"


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


class _FakeTelegramApi:
    def __init__(self):
        self.token = "fake"
        self.calls = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        return {"ok": True, "result": {}}


class _StubDaemonClient:
    """gate_tui_hook's daemon-job probe seam: hermetic job_ready control."""

    def __init__(self, *, ready: bool = False, error: Exception | None = None):
        self.ready = ready
        self.error = error
        self.probes: list[str] = []

    async def job_ready(self, short: str) -> bool:
        self.probes.append(short)
        if self.error is not None:
            raise self.error
        return self.ready


def _runtime_with_observed_session(tmp: str, *, extra_env: dict | None = None):
    cfg = ChannelNativeConfig.from_env(
        {
            "WALKCODE_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "fake",
            "WALKCODE_AGENT": "claude",
            "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
            "WALKCODE_CWD": tmp,
            **(extra_env or {}),
        }
    )
    api = _FakeTelegramApi()
    runtime = ChannelNativeRuntime.from_config(cfg, telegram_api=api)
    # Keep the gate hook's daemon probe off the machine's real control socket.
    daemon_transport = runtime.transports.get("claude_daemon")
    if daemon_transport is not None:
        daemon_transport.client = _StubDaemonClient()
    session = runtime.state.sessions.create_observed_session(
        session_id="observed-1",
        binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
        cwd=tmp,
        external_ref={
            "source": "native_tui_hook",
            "resume_ref": {
                "transport_kind": "claude_headless",
                "agent_session_id": AGENT_SESSION_ID,
            },
        },
        owner=_actor("owner"),
    )
    return runtime, session, api


def _pre_tool_payload(tool_name: str, tool_input: dict, **overrides) -> dict:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": AGENT_SESSION_ID,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": f"toolu_{tool_name.lower()}_1",
        "permission_mode": "default",
        "cwd": "/tmp/project",
    }
    payload.update(overrides)
    return payload


class DecisionSpoolTests(unittest.TestCase):
    def test_decision_is_write_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            self.assertTrue(claude_gate.write_decision(state, "toolu_1", {"action": "allow"}))
            self.assertFalse(claude_gate.write_decision(state, "toolu_1", {"action": "deny"}))
            self.assertEqual(claude_gate.read_decision(state, "toolu_1")["action"], "allow")

    def test_pending_roundtrip_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            claude_gate.write_pending(state, {"rid": "toolu_1", "tool_name": "Edit"})
            self.assertEqual(claude_gate.list_pending(state)[0]["tool_name"], "Edit")
            self.assertEqual(claude_gate.read_pending(state, "toolu_1")["rid"], "toolu_1")
            claude_gate.cleanup_gate_files(state, "toolu_1")
            self.assertEqual(claude_gate.list_pending(state), [])
            self.assertIsNone(claude_gate.read_pending(state, "toolu_1"))

    def test_wait_abstains_on_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            decision = claude_gate.wait_for_decision(state, "toolu_x", timeout=5)
            self.assertEqual(decision, {"action": "pass", "reason": "walkcode_offline"})

    def test_wait_returns_decision_landed_mid_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            claude_gate.touch_heartbeat(state)

            def land():
                time.sleep(0.4)
                claude_gate.write_decision(state, "toolu_y", {"action": "deny", "reason": "no"})

            thread = threading.Thread(target=land)
            thread.start()
            decision = claude_gate.wait_for_decision(state, "toolu_y", timeout=5)
            thread.join()
            self.assertEqual(decision["action"], "deny")

    def test_pending_mode_defaults_to_block_for_v2_files_and_unknown_values(self):
        self.assertEqual(claude_gate.pending_mode(None), claude_gate.MODE_BLOCK)
        self.assertEqual(claude_gate.pending_mode({}), claude_gate.MODE_BLOCK)
        self.assertEqual(claude_gate.pending_mode({"mode": "weird"}), claude_gate.MODE_BLOCK)
        self.assertEqual(claude_gate.pending_mode({"mode": "notify"}), claude_gate.MODE_NOTIFY)
        self.assertEqual(claude_gate.pending_mode({"mode": " NOTIFY "}), claude_gate.MODE_NOTIFY)

    def test_wait_times_out_to_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            claude_gate.touch_heartbeat(state)
            decision = claude_gate.wait_for_decision(
                state, "toolu_z", timeout=1, poll_interval=0.05
            )
            self.assertIsNone(decision)


class ShouldGateTests(unittest.TestCase):
    def test_ask_user_question_is_always_intercepted(self):
        for mode in ("auto", "ask_only"):
            self.assertEqual(
                claude_gate.should_gate(
                    tool_name="AskUserQuestion", tool_input={}, gate_mode=mode
                ),
                "ask_user_question",
            )
        # Even under bypassPermissions: the question exists to reach the human.
        self.assertEqual(
            claude_gate.should_gate(
                tool_name="AskUserQuestion",
                tool_input={},
                permission_mode="bypassPermissions",
            ),
            "ask_user_question",
        )

    def test_gate_mode_off_disables_everything(self):
        self.assertEqual(
            claude_gate.should_gate(tool_name="AskUserQuestion", tool_input={}, gate_mode="off"),
            "",
        )
        self.assertEqual(
            claude_gate.should_gate(tool_name="Edit", tool_input={}, gate_mode="off"), ""
        )

    def test_permission_gating_targets_native_prompt_tools_only(self):
        self.assertEqual(claude_gate.should_gate(tool_name="Edit", tool_input={}), "permission")
        self.assertEqual(claude_gate.should_gate(tool_name="Write", tool_input={}), "permission")
        self.assertEqual(
            claude_gate.should_gate(tool_name="mcp__lark__send", tool_input={}), "permission"
        )
        # Internal / read-only tools never native-prompt: stay on native flow.
        for tool in ("Read", "Grep", "Task", "TodoWrite", "ExitPlanMode"):
            self.assertEqual(claude_gate.should_gate(tool_name=tool, tool_input={}), "", tool)

    def test_permission_mode_short_circuits(self):
        for mode in ("bypassPermissions", "plan"):
            self.assertEqual(
                claude_gate.should_gate(tool_name="Edit", tool_input={}, permission_mode=mode),
                "",
                mode,
            )
        # dontAsk stays gated: its native fallback is auto-deny, which makes
        # the channel-side card the only way to approve (work-profile E2E).
        self.assertEqual(
            claude_gate.should_gate(tool_name="Edit", tool_input={}, permission_mode="dontAsk"),
            "permission",
        )
        self.assertEqual(
            claude_gate.should_gate(tool_name="Edit", tool_input={}, permission_mode="acceptEdits"),
            "",
        )
        self.assertEqual(
            claude_gate.should_gate(tool_name="Bash", tool_input={}, permission_mode="acceptEdits"),
            "permission",
        )

    def test_allow_rules_cover_bare_and_bash_prefix(self):
        self.assertEqual(
            claude_gate.should_gate(tool_name="Bash", tool_input={"command": "ls"}, allow_rules=["Bash"]),
            "",
        )
        self.assertEqual(
            claude_gate.should_gate(
                tool_name="Bash", tool_input={"command": "git push"}, allow_rules=["Bash(git:*)"]
            ),
            "",
        )
        self.assertEqual(
            claude_gate.should_gate(
                tool_name="Bash", tool_input={"command": "rm -rf x"}, allow_rules=["Bash(git:*)"]
            ),
            "permission",
        )
        # Non-Bash argument rules are not evaluated: stay on the safe side.
        self.assertEqual(
            claude_gate.should_gate(
                tool_name="Edit", tool_input={}, allow_rules=["Edit(docs/**)"]
            ),
            "permission",
        )

    def test_gate_tools_override_replaces_default_set(self):
        self.assertEqual(
            claude_gate.should_gate(tool_name="Edit", tool_input={}, gate_tools=["WebFetch"]),
            "",
        )
        self.assertEqual(
            claude_gate.should_gate(tool_name="WebFetch", tool_input={}, gate_tools=["WebFetch"]),
            "permission",
        )


class PreToolUseOutputTests(unittest.TestCase):
    def test_permission_actions_map_to_hook_decisions(self):
        allow = claude_gate.pre_tool_use_output("permission", {"action": "allow"}, {})
        self.assertEqual(
            allow,
            {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}},
        )
        always = claude_gate.pre_tool_use_output("permission", {"action": "always_allow"}, {})
        self.assertEqual(always["hookSpecificOutput"]["permissionDecision"], "allow")
        deny = claude_gate.pre_tool_use_output("permission", {"action": "deny", "reason": "nope"}, {})
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(deny["hookSpecificOutput"]["permissionDecisionReason"], "nope")
        self.assertIsNone(claude_gate.pre_tool_use_output("permission", {"action": "pass"}, {}))

    def test_ask_answers_inject_updated_input(self):
        tool_input = {
            "questions": [
                {
                    "question": "颜色?",
                    "header": "颜色",
                    "options": [{"label": "红"}, {"label": "蓝"}],
                    "multiSelect": False,
                }
            ]
        }
        out = claude_gate.pre_tool_use_output(
            "ask_user_question", {"action": "answers", "answers": {"0": "蓝"}}, tool_input
        )
        updated = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertEqual(updated["questions"], tool_input["questions"])
        self.assertEqual(updated["answers"], {"颜色?": "蓝"})

    def test_ask_multi_select_answers_join_with_comma(self):
        tool_input = {"questions": [{"question": "颜色?", "options": [], "multiSelect": True}]}
        out = claude_gate.pre_tool_use_output(
            "ask_user_question", {"action": "answers", "answers": {0: ["红", "蓝"]}}, tool_input
        )
        self.assertEqual(
            out["hookSpecificOutput"]["updatedInput"]["answers"], {"颜色?": "红,蓝"}
        )

    def test_timeout_abstains_to_native_prompt(self):
        # Timeout must NOT deny: the hook abstains (None output) so Claude
        # Code falls back to its native dialog and the terminal can answer.
        for kind in ("ask_user_question", "permission"):
            out = claude_gate.pre_tool_use_output(
                kind, claude_gate.timeout_decision(kind), {}
            )
            self.assertIsNone(out)


class DaemonTransportGateTests(unittest.TestCase):
    def test_capabilities_require_gate_state_path(self):
        bare = ClaudeDaemonTransport(config_dir="/tmp/profile").capabilities()
        self.assertFalse(bare.permission_callback)
        self.assertFalse(bare.ask_user_question)
        with tempfile.TemporaryDirectory() as tmp:
            gated = ClaudeDaemonTransport(
                config_dir="/tmp/profile", gate_state_path=Path(tmp) / "state.json"
            ).capabilities()
            self.assertTrue(gated.permission_callback)
            self.assertTrue(gated.ask_user_question)

    def test_approve_permission_writes_decision_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append((rid, decision["action"]))
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            claude_gate.write_pending(state, {"rid": "toolu_1", "tool_name": "Edit"})
            asyncio.run(
                transport.approve_permission(handle, "toolu_1", {"action": "deny", "reason": "no"})
            )
            decision = claude_gate.read_decision(state, "toolu_1")
            self.assertEqual(decision["kind"], "permission")
            self.assertEqual(decision["action"], "deny")
            self.assertEqual(decision["reason"], "no")
            self.assertEqual(seen, [("toolu_1", "deny")])

    def test_answer_user_question_writes_answers_and_strips_private_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            claude_gate.write_pending(state, {"rid": "toolu_2", "tool_name": "AskUserQuestion"})
            asyncio.run(
                transport.answer_user_question(
                    handle, "toolu_2", {0: "蓝", "_questions": [{"q": "x"}]}
                )
            )
            decision = claude_gate.read_decision(state, "toolu_2")
            self.assertEqual(decision["action"], "answers")
            self.assertEqual(decision["answers"], {"0": "蓝"})

    def test_stale_card_decision_without_pending_raises_stale_gate(self):
        # Hook timed out / runtime restarted and the pending is gone: a late
        # card click must not leave an orphan decision file, must not feed the
        # always_allow observer — and must NOT read as success (the caller
        # flips the card to "已失效" instead of "已允许").
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(handle, "toolu_gone", {"action": "always_allow"})
                )
            self.assertEqual(caught.exception.reason, "stale_gate")
            self.assertIsNone(claude_gate.read_decision(state, "toolu_gone"))
            self.assertEqual(seen, [])

    def test_lost_write_once_race_raises_already_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            claude_gate.write_pending(state, {"rid": "toolu_3", "tool_name": "Edit"})
            claude_gate.write_decision(state, "toolu_3", {"action": "deny"})
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(transport.approve_permission(handle, "toolu_3", {"action": "allow"}))
            self.assertEqual(caught.exception.reason, "already_resolved")
            self.assertEqual(claude_gate.read_decision(state, "toolu_3")["action"], "deny")
            self.assertEqual(seen, [])

    def test_gate_calls_without_spool_raise_capability_unsupported(self):
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile")
        handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
        with self.assertRaises(CapabilityUnsupported):
            asyncio.run(transport.approve_permission(handle, "r", {"action": "allow"}))
        with self.assertRaises(CapabilityUnsupported):
            asyncio.run(transport.answer_user_question(handle, "r", {}))


class GateTuiHookTests(unittest.TestCase):
    def test_non_pre_tool_hooks_abstain_but_spool_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            output = runtime.gate_tui_hook(
                hook_type="Stop", payload={"session_id": AGENT_SESSION_ID}, agent="claude"
            )
            self.assertIsNone(output)
            self.assertTrue(list(runtime._tui_hook_queue_dir.glob("*.json")))

    def test_ungated_tool_abstains(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            claude_gate.touch_heartbeat(runtime.state_store.path)
            output = runtime.gate_tui_hook(
                hook_type="PreToolUse",
                payload=_pre_tool_payload("Read", {"file_path": "/tmp/x"}),
                agent="claude",
            )
            self.assertIsNone(output)
            self.assertEqual(claude_gate.list_pending(runtime.state_store.path), [])

    def test_gated_tool_without_serve_heartbeat_abstains(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            output = runtime.gate_tui_hook(
                hook_type="PreToolUse",
                payload=_pre_tool_payload("Edit", {"file_path": "/tmp/x"}),
                agent="claude",
            )
            self.assertIsNone(output)
            self.assertEqual(claude_gate.list_pending(runtime.state_store.path), [])

    def test_gated_tool_returns_decision_output_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "allow"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(
                output["hookSpecificOutput"]["permissionDecision"], "allow"
            )
            self.assertEqual(claude_gate.list_pending(state), [])
            self.assertIsNone(claude_gate.read_decision(state, payload["tool_use_id"]))

    def test_ask_user_question_answers_flow_through_updated_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            tool_input = {
                "questions": [{"question": "颜色?", "options": [{"label": "红"}], "multiSelect": False}]
            }
            payload = _pre_tool_payload("AskUserQuestion", tool_input)
            claude_gate.write_decision(
                state, payload["tool_use_id"], {"action": "answers", "answers": {"0": "红"}}
            )
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(
                output["hookSpecificOutput"]["updatedInput"]["answers"], {"颜色?": "红"}
            )

    def test_walkcode_headless_worker_is_never_gated(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            claude_gate.touch_heartbeat(runtime.state_store.path)
            payload = _pre_tool_payload(
                "Edit",
                {"file_path": "/tmp/x"},
                _walkcode_hook_process_tree=[
                    "python -c 'import claude_agent_sdk' /x/_bundled/claude --whatever"
                ],
            )
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertIsNone(output)
            self.assertEqual(claude_gate.list_pending(runtime.state_store.path), [])

    def test_gate_mode_off_via_env_disables_gating(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(
                tmp, extra_env={"WALKCODE_CLAUDE_GATE_MODE": "off"}
            )
            claude_gate.touch_heartbeat(runtime.state_store.path)
            output = runtime.gate_tui_hook(
                hook_type="PreToolUse",
                payload=_pre_tool_payload("Edit", {"file_path": "/tmp/x"}),
                agent="claude",
            )
            self.assertIsNone(output)


class GateNotifyRoutingTests(unittest.TestCase):
    """v3 dual-surface routing (ADR 0046 v3): daemon jobs -> capture then
    abstain (mode=notify); dontAsk / non-daemon / style=block stay on v2."""

    def _stub(self, runtime, **kwargs) -> _StubDaemonClient:
        client = _StubDaemonClient(**kwargs)
        runtime.transports["claude_daemon"].client = client
        return client

    def test_daemon_job_captures_notify_pending_and_abstains(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            stub = self._stub(runtime, ready=True)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertIsNone(output)
            self.assertEqual(stub.probes, [AGENT_SESSION_ID.split("-")[0]])
            pending = claude_gate.read_pending(state, payload["tool_use_id"])
            self.assertEqual(claude_gate.pending_mode(pending), claude_gate.MODE_NOTIFY)
            self.assertEqual(pending["daemon_short"], AGENT_SESSION_ID.split("-")[0])
            self.assertEqual(pending["tool_input"], {"file_path": "/tmp/x"})
            # No hook is blocking on a decision, so notify pendings carry no
            # reap deadline: the runtime owns their cleanup after card post.
            self.assertNotIn("deadline", pending)

    def test_ask_user_question_also_routes_to_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            self._stub(runtime, ready=True)
            payload = _pre_tool_payload(
                "AskUserQuestion",
                {"questions": [{"question": "颜色?", "options": [{"label": "红"}]}]},
            )
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertIsNone(output)
            pending = claude_gate.read_pending(state, payload["tool_use_id"])
            self.assertEqual(claude_gate.pending_mode(pending), claude_gate.MODE_NOTIFY)
            self.assertEqual(pending["kind"], claude_gate.KIND_ASK_USER)

    def test_dont_ask_stays_on_blocking_gate_without_probing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            stub = self._stub(runtime, ready=True)
            payload = _pre_tool_payload(
                "Edit", {"file_path": "/tmp/x"}, permission_mode="dontAsk"
            )
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "allow"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
            self.assertEqual(stub.probes, [])

    def test_non_daemon_session_stays_on_blocking_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            stub = self._stub(runtime, ready=False)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "allow"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
            self.assertEqual(stub.probes, [AGENT_SESSION_ID.split("-")[0]])

    def test_probe_failure_degrades_to_blocking_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            self._stub(runtime, error=OSError("socket gone"))
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "allow"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_gate_style_block_env_forces_v2_without_probing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(
                tmp, extra_env={"WALKCODE_CLAUDE_GATE_STYLE": "block"}
            )
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            stub = self._stub(runtime, ready=True)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "allow"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")
            self.assertEqual(stub.probes, [])

    def test_invalid_gate_style_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "claude",
                    "WALKCODE_CLAUDE_GATE_STYLE": "yolo",
                }
            )
        self.assertIn("WALKCODE_CLAUDE_GATE_STYLE", str(caught.exception))

    def test_block_pending_records_mode_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            self._stub(runtime, ready=False)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})

            captured = {}
            original_wait = claude_gate.wait_for_decision

            def _capture_then_allow(state_path, rid, **kwargs):
                captured.update(claude_gate.read_pending(state_path, rid) or {})
                return {"action": "allow"}

            claude_gate.wait_for_decision = _capture_then_allow
            try:
                runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            finally:
                claude_gate.wait_for_decision = original_wait
            self.assertEqual(claude_gate.pending_mode(captured), claude_gate.MODE_BLOCK)
            self.assertGreater(float(captured.get("deadline", 0)), 0)


class GateDrainTests(unittest.TestCase):
    def _pending_for_session(self, rid: str = "toolu_edit_1") -> dict:
        return {
            "rid": rid,
            "kind": "permission",
            "agent": "claude",
            "transport_kind": "claude_headless",
            "session_id": AGENT_SESSION_ID,
            "resume_ref": {"agent_session_id": AGENT_SESSION_ID},
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x"},
            "created_at": time.time(),
            "deadline": time.time() + 600,
        }

    def test_pending_becomes_permission_card_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(state, self._pending_for_session())
            processed = asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(processed, 1)
            sent = [
                payload
                for method, payload in api.calls
                if method == "sendMessage" and "Edit" in str(payload.get("text", ""))
            ]
            self.assertTrue(sent)
            # No decision yet: that comes from the card callback.
            self.assertIsNone(claude_gate.read_decision(state, "toolu_edit_1"))
            # Idempotent: second drain does not send a second card.
            calls_before = len(api.calls)
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(len(api.calls), calls_before)

    def test_session_always_allow_short_circuits_without_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            runtime._gate_always_allow.add((session.session_id, "Edit"))
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            decision = claude_gate.read_decision(state, "toolu_edit_1")
            self.assertEqual(decision["action"], "allow")
            self.assertFalse(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "Edit" in str(payload.get("text", ""))
                ]
            )

    def test_unroutable_pending_gets_pass_decision_after_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            request = self._pending_for_session(rid="toolu_orphan")
            request["session_id"] = "99999999-9999-9999-9999-999999999999"
            request["resume_ref"] = {"agent_session_id": request["session_id"]}
            request["created_at"] = time.time() - 60
            claude_gate.write_pending(state, request)
            asyncio.run(runtime.drain_claude_gate_requests())
            decision = claude_gate.read_decision(state, "toolu_orphan")
            self.assertEqual(decision["action"], "pass")

    def test_record_gate_decision_learns_always_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(state, self._pending_for_session())
            runtime._record_gate_decision("toolu_edit_1", {"action": "always_allow"})
            self.assertIn((session.session_id, "Edit"), runtime._gate_always_allow)
            runtime._record_gate_decision("toolu_edit_1", {"action": "allow"})
            self.assertEqual(len(runtime._gate_always_allow), 1)

    def test_drain_reaps_orphan_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_decision(state, "toolu_orphaned", {"action": "allow"})
            path = claude_gate.decision_path(state, "toolu_orphaned")
            import os

            old = time.time() - 3600
            os.utime(path, (old, old))
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertIsNone(claude_gate.read_decision(state, "toolu_orphaned"))

    def test_gate_prompt_interaction_outlives_default_token_ttl(self):
        # The blocking hook waits up to 30 min; the card must stay decidable
        # for that whole window, not the 10-min token default.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            request = {
                "rid": "toolu_ttl",
                "kind": "permission",
                "transport_kind": "claude_headless",
                "session_id": AGENT_SESSION_ID,
                "resume_ref": {"agent_session_id": AGENT_SESSION_ID},
                "tool_name": "Edit",
                "tool_input": {},
                "created_at": time.time(),
                "deadline": time.time() + 1800,
            }
            posted = asyncio.run(
                runtime.orchestrator.post_claude_gate_prompt("observed-1", request)
            )
            self.assertTrue(posted)
            interactions = runtime.orchestrator.interactions
            ctx = next(
                ctx
                for ctx in interactions._interactions.values()
                if ctx.transport_request_id == "toolu_ttl"
            )
            self.assertGreater(ctx.expires_at - ctx.created_at, 600)


class _InjectStubClient:
    """Transport-level injection seam: list_jobs snapshots + attach recorder."""

    def __init__(self, *, jobs: list | None = None):
        self.jobs = jobs if jobs is not None else []
        self.injections: list[tuple[str, list[bytes]]] = []
        self.clear_after_inject = True

    async def job_ready(self, short: str) -> bool:
        return True

    async def list_jobs(self):
        return [dict(job) for job in self.jobs]

    async def attach_send_keys(self, short, frames, **kwargs):
        self.injections.append((short, [bytes(data) for data, _delay in frames]))
        if self.clear_after_inject:
            for job in self.jobs:
                if job.get("short") == short:
                    job["needs"] = ""
                    job["tempo"] = "idle"


SHORT = AGENT_SESSION_ID.split("-")[0]


def _blocked_job(needs: str) -> dict:
    return {"short": SHORT, "sessionId": AGENT_SESSION_ID, "tempo": "blocked", "needs": needs}


def _notify_request(rid: str = "toolu_edit_1", **overrides) -> dict:
    request = {
        "rid": rid,
        "kind": "permission",
        "mode": "notify",
        "daemon_short": SHORT,
        "agent": "claude",
        "transport_kind": "claude_headless",
        "session_id": AGENT_SESSION_ID,
        "resume_ref": {"agent_session_id": AGENT_SESSION_ID},
        "tool_name": "Edit",
        "tool_input": {"file_path": "/tmp/x"},
        "created_at": time.time(),
    }
    request.update(overrides)
    return request


class NotifyGateInjectionTests(unittest.TestCase):
    """v3 keystroke delivery on the daemon transport (ADR 0046 v3)."""

    def setUp(self):
        self._verify_timeout = claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS
        self._verify_poll = claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS
        claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS = 0.1
        claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = 0.01

    def tearDown(self):
        claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS = self._verify_timeout
        claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = self._verify_poll

    def _transport(self, tmp: str, client: _InjectStubClient) -> ClaudeDaemonTransport:
        transport = ClaudeDaemonTransport(
            client=client, gate_state_path=Path(tmp) / "state.json"
        )
        return transport

    def _handle(self) -> TransportHandle:
        return TransportHandle(
            handle_id=f"claude-daemon-{SHORT}",
            transport_kind="claude_daemon",
            ref={"short": SHORT},
        )

    def test_permission_allow_injects_key_1_without_decision_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("approve Edit: /tmp/x")])
            transport = self._transport(tmp, client)
            observed = []
            transport.on_gate_decision = lambda rid, decision: observed.append((rid, decision))
            transport.register_notify_gate("rid-1", _notify_request(), session_id="observed-1")
            asyncio.run(
                transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
            )
            # Permission allow is a single "1", no trailing Enter (round-2).
            self.assertEqual(client.injections, [(SHORT, [b"1"])])
            self.assertIsNone(claude_gate.read_decision(transport.gate_state_path, "rid-1"))
            self.assertIsNone(transport.notify_gate("rid-1"))
            self.assertTrue(transport.recently_injected(SHORT))
            self.assertEqual(observed[0][0], "rid-1")
            self.assertEqual(observed[0][1]["tool_name"], "Edit")
            self.assertEqual(observed[0][1]["session_id"], "observed-1")

    def test_permission_deny_injects_esc(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("approve Edit: /tmp/x")])
            transport = self._transport(tmp, client)
            transport.register_notify_gate("rid-1", _notify_request())
            asyncio.run(
                transport.approve_permission(self._handle(), "rid-1", {"action": "deny"})
            )
            self.assertEqual(client.injections, [(SHORT, [b"\x1b"])])

    def test_ask_answers_inject_mapped_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("answer: 颜色? (红 · 蓝)")])
            transport = self._transport(tmp, client)
            request = _notify_request(
                kind="ask_user_question",
                tool_name="AskUserQuestion",
                tool_input={"questions": [{"question": "颜色?", "options": ["红", "蓝"]}]},
            )
            transport.register_notify_gate("rid-ask", request)
            asyncio.run(
                transport.answer_user_question(self._handle(), "rid-ask", {"0": "蓝"})
            )
            self.assertEqual(client.injections, [(SHORT, [b"2", b"\r"])])

    def test_dialog_mismatch_raises_and_tombstones(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("approve Bash: rm -rf /tmp/y")])
            transport = self._transport(tmp, client)
            transport.register_notify_gate("rid-1", _notify_request())  # tool Edit
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "dialog_mismatch")
            self.assertEqual(client.injections, [])
            # A second click on the retired gate is told the truth.
            with self.assertRaises(claude_gate.GateInjectionFailed) as second:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(second.exception.reason, "already_resolved")

    def test_tool_name_matching_is_exact_not_substring(self):
        # Review finding (9-dimension hit): "Edit" must NOT drive an
        # "approve MultiEdit: ..." dialog via substring matching.
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("approve MultiEdit: /tmp/y")])
            transport = self._transport(tmp, client)
            transport.register_notify_gate("rid-1", _notify_request())  # tool Edit
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "dialog_mismatch")
            self.assertEqual(client.injections, [])

    def test_probe_outage_never_reads_as_success(self):
        # Review finding: list_jobs failure must not be folded into "dialog
        # is gone" — neither before nor after the keys are written.
        class _OutageClient(_InjectStubClient):
            async def list_jobs(self):
                raise TransportUnavailable("socket gone")

        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp, _OutageClient())
            transport.register_notify_gate("rid-1", _notify_request())
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "inject_failed")
            self.assertFalse(transport.recently_injected(SHORT))

    def test_probe_outage_after_injection_raises_not_cleared(self):
        class _OutageAfterInject(_InjectStubClient):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.fail_after_inject = False

            async def list_jobs(self):
                if self.fail_after_inject:
                    raise TransportUnavailable("socket gone")
                return await super().list_jobs()

            async def attach_send_keys(self, short, frames, **kwargs):
                await super().attach_send_keys(short, frames, **kwargs)
                self.fail_after_inject = True

        with tempfile.TemporaryDirectory() as tmp:
            client = _OutageAfterInject(jobs=[_blocked_job("approve Edit: /tmp/x")])
            client.clear_after_inject = False
            transport = self._transport(tmp, client)
            transport.register_notify_gate("rid-1", _notify_request())
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "not_cleared")
            self.assertFalse(transport.recently_injected(SHORT))

    def test_unmappable_answers_raise_not_injectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("answer: 颜色? (红)")])
            transport = self._transport(tmp, client)
            request = _notify_request(
                kind="ask_user_question",
                tool_name="AskUserQuestion",
                tool_input={"questions": [{"question": "颜色?", "options": ["红"]}]},
            )
            transport.register_notify_gate("rid-ask", request)
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.answer_user_question(self._handle(), "rid-ask", {"0": "   "})
                )
            self.assertEqual(caught.exception.reason, "not_injectable")
            self.assertEqual(client.injections, [])

    def test_dialog_not_clearing_raises_not_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = _InjectStubClient(jobs=[_blocked_job("approve Edit: /tmp/x")])
            client.clear_after_inject = False
            transport = self._transport(tmp, client)
            transport.register_notify_gate("rid-1", _notify_request())
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "not_cleared")
            self.assertEqual(len(client.injections), 1)
            self.assertFalse(transport.recently_injected(SHORT))

    def test_terminal_resolution_tombstones_open_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = self._transport(tmp, _InjectStubClient())
            transport.register_notify_gate("rid-1", _notify_request())
            resolved = transport.resolve_notify_gates_for_short(SHORT)
            self.assertEqual(resolved, ["rid-1"])
            self.assertIsNone(transport.notify_gate("rid-1"))
            with self.assertRaises(claude_gate.GateInjectionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(self._handle(), "rid-1", {"action": "allow"})
                )
            self.assertEqual(caught.exception.reason, "already_resolved")


class NotifyGateDrainTests(unittest.TestCase):
    def test_notify_pending_posts_card_registers_gate_and_removes_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            runtime.transports["claude_daemon"].client = _InjectStubClient(
                jobs=[_blocked_job("approve Edit: /tmp/x")]
            )
            claude_gate.write_pending(state, _notify_request())
            processed = asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(processed, 1)
            self.assertTrue(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "Edit" in str(payload.get("text", ""))
                ]
            )
            self.assertIsNone(claude_gate.read_pending(state, "toolu_edit_1"))
            self.assertIsNone(claude_gate.read_decision(state, "toolu_edit_1"))
            gate = runtime.transports["claude_daemon"].notify_gate("toolu_edit_1")
            self.assertIsNotNone(gate)
            self.assertEqual(gate["session_id"], "observed-1")
            self.assertEqual(gate["short"], SHORT)

    def test_notify_unroutable_pending_removed_without_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            request = _notify_request(rid="toolu_orphan")
            request["session_id"] = "99999999-9999-9999-9999-999999999999"
            request["resume_ref"] = {"agent_session_id": request["session_id"]}
            request["created_at"] = time.time() - 60
            claude_gate.write_pending(state, request)
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertIsNone(claude_gate.read_pending(state, "toolu_orphan"))
            self.assertIsNone(claude_gate.read_decision(state, "toolu_orphan"))

    def test_notify_always_allow_auto_injects_without_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            client = _InjectStubClient(jobs=[_blocked_job("approve Edit: /tmp/x")])
            runtime.transports["claude_daemon"].client = client
            runtime._gate_always_allow.add((session.session_id, "Edit"))
            claude_gate.write_pending(state, _notify_request())
            old_poll = claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS
            claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = 0.01
            try:
                asyncio.run(runtime.drain_claude_gate_requests())
            finally:
                claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = old_poll
            # Permission auto-allow is a single "1", no trailing Enter (round-2).
            self.assertEqual(client.injections, [(SHORT, [b"1"])])
            self.assertIsNone(claude_gate.read_pending(state, "toolu_edit_1"))
            self.assertFalse(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "Edit" in str(payload.get("text", ""))
                ]
            )

    def test_notify_uninjectable_ask_form_degrades_to_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            runtime.transports["claude_daemon"].client = _InjectStubClient(
                jobs=[_blocked_job("answer: A? (x)")]
            )
            request = _notify_request(
                rid="toolu_ask",
                kind="ask_user_question",
                tool_name="AskUserQuestion",
                tool_input={
                    "questions": [
                        {"question": "A?", "options": ["x"]},
                        {"question": "B?", "options": ["y", "z"], "multiSelect": True},
                    ]
                },
            )
            claude_gate.write_pending(state, request)
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertTrue(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "请在终端" in str(payload.get("text", ""))
                ]
            )
            self.assertIsNone(claude_gate.read_pending(state, "toolu_ask"))
            # The degraded form still registers (suppresses the duplicate
            # needs notice; reaped when the terminal answers). Injection is
            # impossible anyway: there is no interactive card to click.
            self.assertIsNotNone(runtime.transports["claude_daemon"].notify_gate("toolu_ask"))

    def test_notify_always_allow_failure_hands_over_to_card_without_retry(self):
        # Review finding: a failed auto-injection may already have pressed a
        # key — the drain must not auto-retry; it falls through to a human
        # card in the same tick and the pending is consumed.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            client = _InjectStubClient(jobs=[_blocked_job("approve Edit: /tmp/x")])
            client.clear_after_inject = False  # injection never confirms
            runtime.transports["claude_daemon"].client = client
            runtime._gate_always_allow.add((session.session_id, "Edit"))
            claude_gate.write_pending(state, _notify_request())
            old_poll = claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS
            old_timeout = claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS
            claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = 0.01
            claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS = 0.05
            try:
                asyncio.run(runtime.drain_claude_gate_requests())
                asyncio.run(runtime.drain_claude_gate_requests())
            finally:
                claude_daemon_mod.GATE_INJECT_VERIFY_POLL_SECONDS = old_poll
                claude_daemon_mod.GATE_INJECT_VERIFY_TIMEOUT_SECONDS = old_timeout
            # Exactly one automatic keypress across both ticks.
            self.assertEqual(len(client.injections), 1)
            self.assertIsNone(claude_gate.read_pending(state, "toolu_edit_1"))
            # The human card went out as the fallback surface.
            self.assertTrue(
                [
                    p
                    for m, p in api.calls
                    if m == "sendMessage" and "Edit" in str(p.get("text", ""))
                ]
            )

    def test_notify_card_waits_for_dialog_and_drops_when_never_rendered(self):
        # Auto-approved tool calls never render a dialog: no dialog, no card
        # (live-E2E: `date` was auto-approved and the card dangled forever).
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            runtime.transports["claude_daemon"].client = _InjectStubClient(jobs=[])
            claude_gate.write_pending(state, _notify_request())
            asyncio.run(runtime.drain_claude_gate_requests())
            # Dialog not up yet: card held back, pending kept for retry.
            self.assertIsNotNone(claude_gate.read_pending(state, "toolu_edit_1"))
            self.assertFalse(
                [
                    p
                    for m, p in api.calls
                    if m == "sendMessage" and "Edit" in str(p.get("text", ""))
                ]
            )
            # Past the dialog grace with still no dialog: dropped silently.
            stale = _notify_request()
            stale["created_at"] = time.time() - 60
            claude_gate.write_pending(state, stale)
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertIsNone(claude_gate.read_pending(state, "toolu_edit_1"))
            self.assertIsNone(runtime.transports["claude_daemon"].notify_gate("toolu_edit_1"))

    def test_permission_request_notice_suppressed_while_notify_gate_open(self):
        # v3: the dialog renders natively, so the PermissionRequest hook fires
        # even though a rich gate card exists — the old "answer in the
        # terminal" notice must not double-post for the same tool.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            runtime.transports["claude_daemon"].register_notify_gate(
                "rid-1",
                _notify_request(kind="ask_user_question", tool_name="AskUserQuestion"),
            )
            payload = {
                "hook_event_name": "PermissionRequest",
                "session_id": AGENT_SESSION_ID,
                "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "Pick", "options": ["a"]}]},
            }
            asyncio.run(
                runtime._send_tui_hook_output(
                    session, hook_type="permission-request", payload=payload
                )
            )
            self.assertFalse(
                [
                    p
                    for m, p in api.calls
                    if m == "sendMessage" and "waiting for your approval" in str(p.get("text", ""))
                ]
            )
            # A different tool's native prompt still gets the notice.
            other = dict(payload, tool_name="WebFetch", tool_input={"url": "https://x"})
            asyncio.run(
                runtime._send_tui_hook_output(
                    session, hook_type="permission-request", payload=other
                )
            )
            self.assertTrue(
                [
                    p
                    for m, p in api.calls
                    if m == "sendMessage" and "waiting for your approval" in str(p.get("text", ""))
                ]
            )

    def test_record_gate_decision_uses_embedded_notify_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            runtime._record_gate_decision(
                "rid-x",
                {
                    "action": "always_allow",
                    "tool_name": "Bash",
                    "session_id": session.session_id,
                },
            )
            self.assertIn((session.session_id, "Bash"), runtime._gate_always_allow)


class DualSurfaceCardRenderTests(unittest.TestCase):
    """Regression: live E2E caught a KeyError('card') when appending the
    dual-surface note — the Lark message envelope key is ``content``."""

    def test_permission_card_renders_dual_surface_note(self):
        from walkcode.channel_native.lark_cards import render_lark_message

        view = {
            "type": "permission_prompt",
            "tool_name": "Bash",
            "tool_input": {"command": "date"},
            "actions": [{"action": "allow", "label": "允许", "token": "t1"}],
            "dual_surface": True,
        }
        message = render_lark_message(view)
        notes = [
            element
            for element in message["content"]["elements"]
            if element.get("tag") == "note"
        ]
        self.assertTrue(any("先答先生效" in str(note) for note in notes))

    def test_ask_button_and_form_cards_render_dual_surface_note(self):
        from walkcode.channel_native.lark_cards import render_lark_message

        button_view = {
            "type": "ask_user_question",
            "questions": [
                {
                    "prompt": "Pick a color",
                    "options": [{"action": "answer:0:0", "label": "red", "token": "t1"}],
                }
            ],
            "dual_surface": True,
        }
        form_view = {
            "type": "ask_user_question",
            "questions": [
                {
                    "prompt": "Pick a color",
                    "options": [{"label": "red"}],
                    "other": {"action": "other:0", "token": "t2"},
                }
            ],
            "submit": {"action": "submit_all", "token": "t3"},
            "dual_surface": True,
        }
        for view in (button_view, form_view):
            message = render_lark_message(view)
            self.assertTrue(
                any(
                    element.get("tag") == "note" and "先答先生效" in str(element)
                    for element in message["content"]["elements"]
                ),
                view["questions"][0],
            )

    def test_degraded_and_terminal_decision_results_render(self):
        from walkcode.channel_native.lark_cards import render_lark_message

        degraded = render_lark_message(
            {"type": "decision_result", "kind": "permission", "action": "degraded", "detail": "x"}
        )
        self.assertIn("请在终端操作", str(degraded))
        terminal = render_lark_message(
            {"type": "decision_result", "kind": "ask_user_question", "action": "terminal"}
        )
        self.assertIn("已在终端处理", str(terminal))


class DaemonStatePatchSemanticsTests(unittest.TestCase):
    def test_idle_needs_does_not_flip_waiting_permission(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "idle", "needs": "send a prompt to start"},
                    "",
                )
            )
            self.assertEqual(last, "")
            self.assertNotEqual(session.lifecycle_state, "WAITING_PERMISSION")
            self.assertTrue(session.transport_ref.get("daemon_live"))
            self.assertFalse(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "send a prompt" in str(payload.get("text", ""))
                ]
            )

    def test_cleared_needs_syncs_terminal_decision_back_to_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Edit: /tmp/x.py"},
                    "",
                )
            )
            self.assertEqual(session.lifecycle_state, "WAITING_PERMISSION")
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id, {"tempo": "active", "needs": ""}, last
                )
            )
            self.assertEqual(last, "")
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertTrue(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "已在终端处理" in str(payload.get("text", ""))
                ]
            )

    def test_notice_suppressed_while_notify_gate_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            runtime.transports["claude_daemon"].register_notify_gate(
                "rid-1", _notify_request(), session_id=session.session_id
            )
            asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Edit: /tmp/x"},
                    "",
                    short=SHORT,
                )
            )
            self.assertEqual(session.lifecycle_state, "WAITING_PERMISSION")
            # The v3 rich card came from the gate drain; no duplicate orange
            # notice for the same dialog.
            self.assertFalse(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "approve Edit" in str(payload.get("text", ""))
                ]
            )

    def test_cleared_needs_after_injection_skips_terminal_sync_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            transport = runtime.transports["claude_daemon"]
            transport.register_notify_gate("rid-1", _notify_request())
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Edit: /tmp/x"},
                    "",
                    short=SHORT,
                )
            )
            transport._recent_injections[SHORT] = time.time()
            asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id, {"tempo": "active", "needs": ""}, last, short=SHORT
                )
            )
            self.assertFalse(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "已在终端处理" in str(payload.get("text", ""))
                ]
            )
            self.assertIsNone(transport.notify_gate("rid-1"))

    def test_cleared_needs_retires_gates_even_when_lifecycle_flipped_away(self):
        # Review finding: a tool event can move the session out of
        # WAITING_PERMISSION before the needs-cleared patch arrives; the
        # tombstone must not depend on lifecycle state.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            transport = runtime.transports["claude_daemon"]
            transport.register_notify_gate("rid-1", _notify_request())
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Edit: /tmp/x"},
                    "",
                    short=SHORT,
                )
            )
            session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"  # tool event raced
            asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id, {"tempo": "active", "needs": ""}, last, short=SHORT
                )
            )
            self.assertIsNone(transport.notify_gate("rid-1"))

    def test_cleared_needs_terminal_answer_retires_notify_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            transport = runtime.transports["claude_daemon"]
            transport.register_notify_gate("rid-1", _notify_request())
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Edit: /tmp/x"},
                    "",
                    short=SHORT,
                )
            )
            asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id, {"tempo": "active", "needs": ""}, last, short=SHORT
                )
            )
            self.assertTrue(
                [
                    payload
                    for method, payload in api.calls
                    if method == "sendMessage" and "已在终端处理" in str(payload.get("text", ""))
                ]
            )
            self.assertIsNone(transport.notify_gate("rid-1"))

    def test_status_card_refresh_skips_unchanged_state(self):
        # Event-driven refreshes fire on every hook event, but only material
        # state changes may spend a Lark API call (monthly-quota exhaustion
        # was traced to no-op card patches).
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            orch = runtime.orchestrator
            session.channel_binding.capabilities["status_card"] = True

            def card_calls():
                return sum(
                    1
                    for method, _payload in api.calls
                    if method in {"sendMessage", "editMessageText", "sendRichMessage"}
                )

            session.last_progress_event = "external_tui.pre-tool"
            asyncio.run(orch.refresh_session_status_card(session))
            first = card_calls()
            self.assertGreater(first, 0)
            # Tool churn: progress flips and seq ticks, nothing material.
            session.last_progress_event = "external_tui.post-tool"
            asyncio.run(orch.refresh_session_status_card(session))
            session.last_progress_event = "external_tui.pre-tool"
            session.last_event_seq += 5
            asyncio.run(orch.refresh_session_status_card(session))
            self.assertEqual(card_calls(), first)
            # Material change (lifecycle flip) must go out.
            session.lifecycle_state = "WAITING_PERMISSION"
            asyncio.run(orch.refresh_session_status_card(session))
            self.assertGreater(card_calls(), first)
            # gate.waiting progress is material (it is what the user watches).
            session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
            session.last_progress_event = "gate.waiting:Write"
            before = card_calls()
            asyncio.run(orch.refresh_session_status_card(session))
            self.assertGreater(card_calls(), before)

    def test_status_card_hides_takeover_while_daemon_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            session.transport_ref["daemon_live"] = True
            self.assertEqual(runtime.orchestrator._status_card_actions(session), [])
            session.transport_ref.pop("daemon_live")
            self.assertEqual(
                runtime.orchestrator._status_card_actions(session),
                [{"action": "request_takeover", "label": "Take over"}],
            )


if __name__ == "__main__":
    unittest.main()
