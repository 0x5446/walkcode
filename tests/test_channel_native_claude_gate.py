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
)
from walkcode.channel_native import claude_gate
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

    def test_ask_timeout_denies_with_reason(self):
        out = claude_gate.pre_tool_use_output(
            "ask_user_question", claude_gate.timeout_decision("ask_user_question"), {}
        )
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertTrue(out["hookSpecificOutput"]["permissionDecisionReason"])


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

    def test_stale_card_decision_without_pending_is_dropped(self):
        # Hook timed out and cleaned its pending: a late card click must not
        # leave an orphan decision file nor feed the always_allow observer.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            asyncio.run(
                transport.approve_permission(handle, "toolu_gone", {"action": "always_allow"})
            )
            self.assertIsNone(claude_gate.read_decision(state, "toolu_gone"))
            self.assertEqual(seen, [])

    def test_lost_write_once_race_does_not_notify(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeDaemonTransport(config_dir="/tmp/profile", gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_daemon", ref={})
            claude_gate.write_pending(state, {"rid": "toolu_3", "tool_name": "Edit"})
            claude_gate.write_decision(state, "toolu_3", {"action": "deny"})
            asyncio.run(transport.approve_permission(handle, "toolu_3", {"action": "allow"}))
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
