"""PreToolUse gate (ADR 0046 v2, ADR 0068) — decision spool, blocking hook, drain, decision transport."""

import asyncio
import io
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import walkcode.channel_native as channel_native_mod
from walkcode.channel_native import (
    ActorRef,
    BlockedReason,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelNativeConfig,
    SessionRole,
    TransportHandle,
    TransientDeliveryError,
    TurnInput,
)
from walkcode.channel_native import claude_gate
from walkcode.channel_native.claude_gate_transport import ClaudeGateTransport
from walkcode.channel_native_runtime import ChannelNativeRuntime, _build_transports
import walkcode.channel_native_runtime as runtime_mod


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

    def test_only_notify_mode_counts_as_a_legacy_pending(self):
        self.assertFalse(claude_gate.is_legacy_notify_pending(None))
        self.assertFalse(claude_gate.is_legacy_notify_pending({}))
        self.assertFalse(claude_gate.is_legacy_notify_pending({"mode": "block"}))
        self.assertFalse(claude_gate.is_legacy_notify_pending({"mode": "weird"}))
        self.assertTrue(claude_gate.is_legacy_notify_pending({"mode": "notify"}))
        self.assertTrue(claude_gate.is_legacy_notify_pending({"mode": " NOTIFY "}))

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


class GateTransportTests(unittest.TestCase):
    def test_capabilities_carry_hitl_decisions_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            caps = ClaudeGateTransport(gate_state_path=Path(tmp) / "state.json").capabilities()
            self.assertTrue(caps.permission_callback)
            self.assertTrue(caps.ask_user_question)
            self.assertFalse(caps.structured_input)
            self.assertFalse(caps.set_model)
            self.assertFalse(caps.multi_client_write)

    def test_approve_permission_writes_decision_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeGateTransport(gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append((rid, decision["action"]))
            handle = TransportHandle(handle_id="h", transport_kind="claude_gate", ref={})
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
            transport = ClaudeGateTransport(gate_state_path=state)
            handle = TransportHandle(handle_id="h", transport_kind="claude_gate", ref={})
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
            transport = ClaudeGateTransport(gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_gate", ref={})
            with self.assertRaises(claude_gate.GateDecisionFailed) as caught:
                asyncio.run(
                    transport.approve_permission(handle, "toolu_gone", {"action": "always_allow"})
                )
            self.assertEqual(caught.exception.reason, "stale_gate")
            self.assertIsNone(claude_gate.read_decision(state, "toolu_gone"))
            self.assertEqual(seen, [])

    def test_lost_write_once_race_raises_already_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            transport = ClaudeGateTransport(gate_state_path=state)
            seen = []
            transport.on_gate_decision = lambda rid, decision: seen.append(rid)
            handle = TransportHandle(handle_id="h", transport_kind="claude_gate", ref={})
            claude_gate.write_pending(state, {"rid": "toolu_3", "tool_name": "Edit"})
            claude_gate.write_decision(state, "toolu_3", {"action": "deny"})
            with self.assertRaises(claude_gate.GateDecisionFailed) as caught:
                asyncio.run(transport.approve_permission(handle, "toolu_3", {"action": "allow"}))
            self.assertEqual(caught.exception.reason, "already_resolved")
            self.assertEqual(claude_gate.read_decision(state, "toolu_3")["action"], "deny")
            self.assertEqual(seen, [])

    def test_session_calls_are_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = ClaudeGateTransport(gate_state_path=Path(tmp) / "state.json")
            with self.assertRaises(CapabilityUnsupported):
                asyncio.run(transport.resume(mock.Mock()))
            with self.assertRaises(CapabilityUnsupported):
                asyncio.run(transport.submit_turn(mock.Mock(), mock.Mock(), "k"))


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


class GateBlockPendingTests(unittest.TestCase):
    def test_block_pending_records_mode_and_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
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
            self.assertEqual(captured.get("mode"), claude_gate.MODE_BLOCK)
            self.assertNotIn("daemon_short", captured)
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

    def test_card_is_retired_when_the_hook_hands_the_prompt_to_the_terminal(self):
        # Regression: a blocking hook that timed out handed the prompt to the
        # terminal, the user answered there, and the card kept live buttons
        # forever — only a click could flip it.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            card_call = next(
                i for i, (method, payload) in enumerate(api.calls)
                if method == "sendMessage" and "Edit" in str(payload.get("text", ""))
            )
            card_id = str(card_call + 1)  # the fake API numbers messages by call index
            [hitl] = runtime.orchestrator.hitls.pending_for_session("observed-1")
            ctx = runtime.orchestrator.interactions.get(hitl.interaction_id)

            claude_gate.cleanup_gate_files(state, "toolu_edit_1")  # hook timed out
            asyncio.run(runtime.drain_claude_gate_requests())

            edits = [payload for method, payload in api.calls if method.startswith("edit")]
            self.assertEqual(len(edits), 1)
            self.assertEqual(str(edits[0].get("message_id")), card_id)
            self.assertIn("终端", str(edits[0]))
            self.assertEqual(runtime.orchestrator.hitls.pending_for_session("observed-1"), [])
            self.assertEqual(ctx.decision, {"action": "terminal"})
            # Idempotent: nothing left to retire on the next pass.
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(len([m for m, _ in api.calls if m.startswith("edit")]), 1)

    def _edits(self, api):
        return [payload for method, payload in api.calls if method.startswith("edit")]

    def test_card_delivered_after_the_timeout_is_still_retired(self):
        # The card's first send failed and sat in the outbox retry queue when
        # the hook timed out: there was no message id to edit yet. The retry
        # later delivered the original card with live buttons.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            channel = runtime.channels["telegram"]
            real_send = channel.send_view
            failures = {"left": 1}

            async def flaky_send(binding, view):
                if failures["left"]:
                    failures["left"] -= 1
                    raise TransientDeliveryError("temporarily down")
                return await real_send(binding, view)

            channel.send_view = flaky_send
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            claude_gate.cleanup_gate_files(state, "toolu_edit_1")  # hook timed out
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(self._edits(api), [])  # nothing sent yet to edit

            # A long rate-limit backoff outlasts the edit retry budget; waiting
            # for delivery must not use it up.
            with mock.patch.object(runtime_mod, "CLAUDE_GATE_RETIRE_MAX_TRIES", 2):
                for _ in range(5):
                    asyncio.run(runtime.drain_claude_gate_requests())
            self.assertIn("toolu_edit_1", runtime._gate_cards_retiring)

            outbox = runtime.orchestrator.outbox
            for item in outbox._pending.values():
                item.next_attempt_at = 0.0
            asyncio.run(runtime.orchestrator._flush_outbox())  # retry delivers the card
            asyncio.run(runtime.drain_claude_gate_requests())

            self.assertEqual(len(self._edits(api)), 1)
            self.assertEqual(runtime._gate_cards_retiring, {})

    def test_failed_card_edit_is_logged_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            channel = runtime.channels["telegram"]
            real_edit = channel.edit_view
            refusals = {"left": 1}

            async def refusing_edit(binding, message_id, view):
                if refusals["left"]:
                    refusals["left"] -= 1
                    return False
                return await real_edit(binding, message_id, view)

            channel.edit_view = refusing_edit
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            claude_gate.cleanup_gate_files(state, "toolu_edit_1")
            with mock.patch("walkcode.channel_native._log_degrade") as log:
                asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(log.call_args.args[0], "gate_card_retire_edit_failed")
            self.assertIn("toolu_edit_1", runtime._gate_cards_retiring)

            asyncio.run(runtime.drain_claude_gate_requests())

            self.assertEqual(len(self._edits(api)), 1)
            self.assertEqual(runtime._gate_cards_retiring, {})

    def test_unreadable_pending_file_is_not_mistaken_for_a_returned_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            claude_gate.pending_path(state, "toolu_edit_1").write_text("{half-written")
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertEqual(len(runtime.orchestrator.hitls.pending_for_session("observed-1")), 1)
            self.assertEqual(self._edits(api), [])

    def test_card_decided_on_the_channel_is_not_retired(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(state, self._pending_for_session())
            asyncio.run(runtime.drain_claude_gate_requests())
            [hitl] = runtime.orchestrator.hitls.pending_for_session("observed-1")
            hitl.status = "decided"  # a card click settled it first
            claude_gate.cleanup_gate_files(state, "toolu_edit_1")
            asyncio.run(runtime.drain_claude_gate_requests())
            self.assertFalse([m for m, _ in api.calls if m.startswith("edit")])

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


class DecisionResultRenderTests(unittest.TestCase):
    def test_terminal_decision_result_renders(self):
        from walkcode.channel_native.lark_cards import render_lark_message

        terminal = render_lark_message(
            {"type": "decision_result", "kind": "ask_user_question", "action": "terminal"}
        )
        self.assertIn("已在终端处理", str(terminal))

    def test_gate_cards_carry_no_dual_surface_note(self):
        from walkcode.channel_native.lark_cards import render_lark_message

        message = render_lark_message(
            {
                "type": "permission_prompt",
                "tool_name": "Bash",
                "tool_input": {"command": "date"},
                "actions": [{"action": "allow", "label": "允许", "token": "t1"}],
                "dual_surface": True,  # stale key from a pre-ADR 0068 outbox item
            }
        )
        self.assertNotIn("先答先生效", str(message))


class StatusCardTests(unittest.TestCase):
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

    def test_stale_daemon_live_flag_no_longer_hides_takeover(self):
        # Pre-ADR 0068 state may still carry daemon_live; with no daemon to
        # write through, the takeover button is the only write path.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            session.transport_ref["daemon_live"] = True
            self.assertEqual(
                runtime.orchestrator._status_card_actions(session),
                [{"action": "request_takeover", "label": "Take over"}],
            )


class GateWithoutDaemonTests(unittest.TestCase):
    """ADR 0068: the blocking gate stands on its own — no daemon transport,
    no daemon socket, no daemon env switch can turn it off."""

    def test_claude_runtime_registers_the_gate_transport_and_no_daemon(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            self.assertEqual(set(runtime.transports), {"claude_headless", "claude_gate"})
            transport = runtime.orchestrator._interaction_transport(session)
            self.assertIsInstance(transport, ClaudeGateTransport)
            self.assertIs(transport.on_gate_decision.__self__, runtime)
            self.assertNotIn("claude_daemon", runtime.describe())

    def test_codex_runtime_has_no_gate_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake",
                    "WALKCODE_AGENT": "codex",
                    "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                }
            )
            self.assertNotIn("claude_gate", _build_transports(cfg))

    def test_gate_drain_runs_as_a_maintenance_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, _api = _runtime_with_observed_session(tmp)

            async def names():
                tasks = runtime._start_telegram_maintenance_tasks()
                try:
                    return {task.get_name() for task in tasks}
                finally:
                    await runtime._stop_telegram_maintenance_tasks(tasks)

            started = asyncio.run(names())
            self.assertIn("walkcode-claude-gate-drain", started)
            self.assertFalse([name for name in started if "daemon" in name])

    def test_hook_waits_for_the_card_click_end_to_end(self):
        # Hook process side blocks in gate_tui_hook; serve side drains the
        # pending into a card and the click goes through the orchestrator's
        # interaction transport. Nothing here knows about a daemon.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            rid = payload["tool_use_id"]
            result = {}

            def hook():
                result["output"] = runtime.gate_tui_hook(
                    hook_type="PreToolUse", payload=payload, agent="claude"
                )

            thread = threading.Thread(target=hook)
            thread.start()
            deadline = time.monotonic() + 5
            while claude_gate.read_pending(state, rid) is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertIsNotNone(claude_gate.read_pending(state, rid))
            self.assertEqual(asyncio.run(runtime.drain_claude_gate_requests()), 1)
            self.assertTrue(
                [p for m, p in api.calls if m == "sendMessage" and "Edit" in str(p.get("text", ""))]
            )
            transport = runtime.orchestrator._interaction_transport(session)
            asyncio.run(
                transport.approve_permission(
                    runtime.orchestrator._handle_for_session(session), rid, {"action": "always_allow"}
                )
            )
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["output"]["hookSpecificOutput"]["permissionDecision"], "allow")
            self.assertIn((session.session_id, "Edit"), runtime._gate_always_allow)
            self.assertIsNone(claude_gate.read_pending(state, rid))

    def test_retired_daemon_mode_off_no_longer_disables_the_gate(self):
        # Before ADR 0068, WALKCODE_CLAUDE_DAEMON_MODE=off silently turned the
        # TUI permission / AskUserQuestion cards off too.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(channel_native_mod, "_retired_env_noticed", True):
                runtime, _session, _api = _runtime_with_observed_session(
                    tmp, extra_env={"WALKCODE_CLAUDE_DAEMON_MODE": "off"}
                )
            state = runtime.state_store.path
            claude_gate.touch_heartbeat(state)
            payload = _pre_tool_payload("Edit", {"file_path": "/tmp/x"})
            claude_gate.write_decision(state, payload["tool_use_id"], {"action": "deny"})
            output = runtime.gate_tui_hook(hook_type="PreToolUse", payload=payload, agent="claude")
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_legacy_notify_pending_is_dropped_without_card_or_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _session, api = _runtime_with_observed_session(tmp)
            state = runtime.state_store.path
            claude_gate.write_pending(
                state,
                {
                    "rid": "toolu_old",
                    "mode": "notify",
                    "daemon_short": "5ca3e37c",
                    "kind": "permission",
                    "transport_kind": "claude_headless",
                    "session_id": AGENT_SESSION_ID,
                    "resume_ref": {"agent_session_id": AGENT_SESSION_ID},
                    "tool_name": "Edit",
                    "tool_input": {},
                    "created_at": time.time(),
                },
            )
            self.assertEqual(asyncio.run(runtime.drain_claude_gate_requests()), 0)
            self.assertIsNone(claude_gate.read_pending(state, "toolu_old"))
            self.assertIsNone(claude_gate.read_decision(state, "toolu_old"))
            self.assertFalse([m for m, _ in api.calls if m == "sendMessage"])


class RetiredEnvKeysTests(unittest.TestCase):
    RETIRED = {
        "WALKCODE_CLAUDE_DAEMON_MODE": "auto",
        "WALKCODE_CLAUDE_SPAWN_MODE": "headless",
        "WALKCODE_CLAUDE_LIST_ADOPT": "off",
        # Values the old parser rejected must not fail startup either.
        "WALKCODE_CLAUDE_GATE_STYLE": "yolo",
    }

    def _config(self, extra: dict):
        return ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                **extra,
            }
        )

    def test_old_daemon_keys_are_ignored_with_one_notice(self):
        with mock.patch.object(channel_native_mod, "_retired_env_noticed", False), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as err:
            cfg = self._config(self.RETIRED)
            self._config(self.RETIRED)
        options = cfg.agent_options.get("claude", {})
        for key in ("daemon_mode", "spawn_mode", "list_adopt", "gate_style"):
            self.assertNotIn(key, options)
        lines = [line for line in err.getvalue().splitlines() if "retired env" in line]
        self.assertEqual(len(lines), 1)
        for key in self.RETIRED:
            self.assertIn(key, lines[0])

    def test_no_notice_without_old_keys(self):
        with mock.patch.object(channel_native_mod, "_retired_env_noticed", False), mock.patch(
            "sys.stderr", new_callable=io.StringIO
        ) as err:
            self._config({})
        self.assertNotIn("retired env", err.getvalue())

    def test_old_spawn_mode_daemon_still_starts_headless(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            channel_native_mod, "_retired_env_noticed", True
        ):
            runtime, _session, _api = _runtime_with_observed_session(
                tmp,
                extra_env={
                    "WALKCODE_CLAUDE_SPAWN_MODE": "daemon",
                    "WALKCODE_CLAUDE_DAEMON_MODE": "off",
                },
            )
        self.assertFalse(hasattr(runtime.orchestrator, "daemon_spawner"))
        self.assertEqual(set(runtime.transports), {"claude_headless", "claude_gate"})


class LegacyDaemonStateTests(unittest.TestCase):
    def test_state_with_daemon_era_sessions_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            session.transport_ref.update({"daemon_short": "5ca3e37c", "daemon_live": True})
            session.last_progress_event = "external_tui.daemon_working:Bash"
            spawned = runtime.state.sessions.create_observed_session(
                session_id="tui-claude-daemonspawn",
                binding=ChannelBinding(
                    "telegram", "bot", "chat", "topic-2", "root-2", capabilities={"origin": "daemon_spawn"}
                ),
                cwd=tmp,
                external_ref={
                    "source": "walkcode_daemon_spawn",
                    "resume_ref": {"transport_kind": "claude_headless", "agent_session_id": "abc"},
                    "daemon_short": "abcdef12",
                    "daemon_live": True,
                },
                owner=_actor("owner"),
            )
            spawned.transport_kind = "claude_daemon"
            runtime.save_state()

            reloaded = ChannelNativeRuntime.from_config(runtime.config, telegram_api=_FakeTelegramApi())
            old = reloaded.state.sessions.get("tui-claude-daemonspawn")
            self.assertEqual(old.transport_kind, "external_tui")
            self.assertIsInstance(
                reloaded.orchestrator._interaction_transport(old), ClaudeGateTransport
            )
            self.assertEqual(
                reloaded.state.sessions.get("observed-1").transport_ref.get("daemon_live"), True
            )
            status = runtime_mod._format_status(reloaded.describe())
            self.assertNotIn("claude_daemon", status)

    def test_tui_input_goes_straight_to_the_takeover_prompt(self):
        # The daemon reply attempt that used to run first (and log
        # claude_daemon_reply_failed on every message) is gone.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)
            runtime.state.authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
            with mock.patch.object(channel_native_mod, "_log_degrade") as log:
                result = asyncio.run(
                    runtime.orchestrator.submit_user_input(
                        session.session_id,
                        TurnInput(text="continue"),
                        actor=_actor("owner"),
                        generation=session.generation,
                    )
                )
            self.assertFalse(result.accepted)
            self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
            self.assertTrue(result.blocked_input_id)
            self.assertNotIn(
                "claude_daemon_reply_failed", [call.args[0] for call in log.call_args_list]
            )
            self.assertTrue(
                [p for m, p in api.calls if m == "sendMessage" and "Take over" in str(p)]
            )


if __name__ == "__main__":
    unittest.main()
