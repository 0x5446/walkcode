import asyncio
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import channel_native_debug
from walkcode.channel_native import (
    AgentEvent,
    AgentEventType,
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    DurableOutbox,
    InboundLedger,
    InteractionStore,
    JsonFileStateStore,
    SessionRegistry,
)


class ChannelNativeDebugScriptTests(unittest.TestCase):
    def test_debug_script_help_runs(self):
        result = subprocess.run(
            [sys.executable, "scripts/channel_native_debug.py", "--help"],
            cwd="/Users/alpha/Documents/workspace/walkcode",
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("channel_native_debug.py", result.stdout)

    def test_state_debug_uses_temp_probe_and_does_not_create_configured_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"write_probe"', result.stdout)
        self.assertIn('"exists": false', result.stdout)
        self.assertFalse(state_path.exists())

    def test_state_debug_reports_expired_running_writer_lease_as_informational(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "stale"},
                cwd=tmp,
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

        # ADR 0059: expired lease on a running session is normal (never
        # renewed mid-turn) and no longer blocks submits — the count stays
        # informational and must not fail the health gate.
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"expired_writer_leases": 1', result.stdout)
        self.assertNotIn("expired writer lease", result.stdout)

    def test_state_repair_stops_unresumable_expired_error_session_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            session = sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "stale"},
                cwd=tmp,
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            session.lifecycle_state = "ERROR_RECOVERABLE"
            session.last_progress_event = "session.error"
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--repair-stale-errors",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"repaired_count": 1', result.stdout)
            snapshot = JsonFileStateStore(state_path).load()
            repaired = snapshot.sessions.get(session.session_id)
            self.assertEqual(repaired.status, "stopped")
            self.assertEqual(repaired.lifecycle_state, "STOPPED")
            self.assertEqual(repaired.stop_reason, "repaired_stale_unresumable_error")
            self.assertTrue(list(Path(tmp).glob("state.json.bak-*")))

    def test_state_repair_stops_external_tui_session_whose_process_is_gone_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=codex",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            session = sessions.create_observed_session(
                session_id="tui-codex-dead",
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                cwd=tmp,
                external_ref={
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {"pid": 999999, "allow_terminate": False},
                    },
                },
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--repair-stale-external-tui",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"repaired_count": 1', result.stdout)
            snapshot = JsonFileStateStore(state_path).load()
            repaired = snapshot.sessions.get(session.session_id)
            self.assertEqual(repaired.status, "stopped")
            self.assertEqual(repaired.lifecycle_state, "STOPPED")
            self.assertEqual(repaired.stop_reason, "repaired_stale_external_tui_process_gone")
            self.assertTrue(list(Path(tmp).glob("state.json.bak-*")))

    def test_state_repair_stops_external_tui_stop_hook_session_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            session = sessions.create_observed_session(
                session_id="tui-claude-stopped",
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                cwd=tmp,
                external_ref={
                    "hook_type": "stop",
                    "terminate_ref": {
                        "controller_kind": "process",
                        "process_ref": {"pid": 1, "allow_terminate": False},
                    },
                },
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            session.last_progress_event = "external_tui.stop"
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--repair-stale-external-tui",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"repaired_count": 1', result.stdout)
            snapshot = JsonFileStateStore(state_path).load()
            repaired = snapshot.sessions.get(session.session_id)
            self.assertEqual(repaired.status, "stopped")
            self.assertEqual(repaired.lifecycle_state, "STOPPED")
            self.assertEqual(repaired.stop_reason, "repaired_external_tui_stop_hook")
            self.assertTrue(list(Path(tmp).glob("state.json.bak-*")))

    def test_state_debug_allows_idle_session_without_active_writer_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            session = sessions.create_structured_session(
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                transport_kind="claude_headless",
                transport_ref={"handle_id": "old", "agent_session_id": "agent-session-1"},
                cwd=tmp,
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            session.lifecycle_state = "IDLE"
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"expired_writer_leases": 0', result.stdout)

    def test_state_debug_allows_external_observed_session_without_active_writer_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            sessions = SessionRegistry(now=lambda: 1000.0)
            sessions.create_observed_session(
                session_id="tui-claude-observed",
                binding=ChannelBinding(
                    channel_kind="telegram",
                    account_id="bot",
                    chat_id="chat",
                    root_message_id="root",
                ),
                cwd=tmp,
                external_ref={"source": "native_hook", "agent": "claude"},
                owner=ActorRef("telegram", "owner", "Owner"),
            )
            JsonFileStateStore(state_path).save(
                sessions=sessions,
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=DurableOutbox(now=lambda: 1000.0),
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "state",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"expired_writer_leases": 0', result.stdout)

    def test_outbox_debug_runs_synthetic_dispatch_without_live_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "outbox",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('"synthetic_dispatch"', result.stdout)
        self.assertIn('"sent_count": 1', result.stdout)

    def test_outbox_repair_drops_empty_pending_delivery_with_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "WALKCODE_CHANNEL=telegram",
                        "TELEGRAM_BOT_TOKEN=fake-token",
                        "WALKCODE_AGENT=claude",
                        f"WALKCODE_STATE_PATH={state_path}",
                        f"WALKCODE_CWD={tmp}",
                    ]
                )
            )
            outbox = DurableOutbox(now=lambda: 1000.0)
            outbox.enqueue(
                channel_binding_key=("telegram", "bot", "chat", "", "root"),
                view_model={"type": "turn_completed", "message": ""},
                idempotency_key="empty",
            )
            JsonFileStateStore(state_path).save(
                sessions=SessionRegistry(now=lambda: 1000.0),
                interactions=InteractionStore(now=lambda: 1000.0),
                outbox=outbox,
                authz=AuthorizationStore(now=lambda: 1000.0),
                inbound_ledger=InboundLedger(now=lambda: 1000.0),
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/channel_native_debug.py",
                    "--env-file",
                    str(env_file),
                    "outbox",
                    "--drop-empty-pending",
                    "--json",
                ],
                cwd="/Users/alpha/Documents/workspace/walkcode",
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn('"dropped_count": 1', result.stdout)
            snapshot = JsonFileStateStore(state_path).load()
            self.assertEqual(snapshot.outbox.pending_count(), 0)
            self.assertTrue(list(Path(tmp).glob("state.json.bak-*")))

    def test_agent_smoke_dry_run_does_not_start_transport(self):
        class _Transport:
            launched = False

            def capabilities(self):
                raise AssertionError("runtime.describe should provide capabilities")

            async def launch(self, _spec):
                self.launched = True
                raise AssertionError("dry-run smoke must not launch")

        class _Runtime:
            config = type(
                "Config",
                (),
                {
                    "agent": "claude",
                    "cwd": "/tmp/project",
                },
            )()
            transports = {"claude_headless": _Transport()}

            def describe(self):
                return {
                    "agent": "claude",
                    "agent_status": {
                        "available": True,
                        "capabilities": {
                            "structured_input": True,
                            "structured_output": True,
                        },
                    },
                }

        with patch.object(channel_native_debug.ChannelNativeRuntime, "from_env", return_value=_Runtime()):
            payload = asyncio.run(
                channel_native_debug.debug_agent_smoke(
                    agent="",
                    live=False,
                    prompt="hello",
                    timeout=1.0,
                )
            )

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["live"])
        self.assertTrue(payload["available"])

    def test_agent_smoke_live_fails_on_session_error_event(self):
        class _Transport:
            def capabilities(self):
                raise AssertionError("runtime.describe should provide capabilities")

            async def launch(self, _spec):
                return "handle"

            async def submit_turn(self, _handle, _turn, idempotency_key):
                self.idempotency_key = idempotency_key

            def events(self, _handle):
                return [AgentEvent(AgentEventType.SESSION_ERROR, {"message": "authentication_failed"})]

        class _Runtime:
            config = type(
                "Config",
                (),
                {
                    "agent": "claude",
                    "cwd": "/tmp/project",
                },
            )()
            transports = {"claude_headless": _Transport()}

            def describe(self):
                return {
                    "agent": "claude",
                    "agent_status": {
                        "available": True,
                        "capabilities": {
                            "structured_input": True,
                            "structured_output": True,
                        },
                    },
                }

        with patch.object(channel_native_debug.ChannelNativeRuntime, "from_env", return_value=_Runtime()):
            payload = asyncio.run(
                channel_native_debug.debug_agent_smoke(
                    agent="",
                    live=True,
                    prompt="hello",
                    timeout=1.0,
                )
            )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "agent_session_error")
        self.assertEqual(payload["messages"], ["authentication_failed"])

    @staticmethod
    def _streaming_runtime(events, *, wrap_in_coroutine=False):
        """A runtime whose transport streams events like codex's does.

        `wrap_in_coroutine` mimics ClaudeHeadlessTransport: `events()` is a
        coroutine that *resolves to* the async generator, so the smoke has to
        await before it can recognise a stream.
        """
        closed: list[bool] = []

        async def stream():
            try:
                for event in events:
                    yield event
                    await asyncio.sleep(0)
            finally:
                closed.append(True)

        class _Transport:
            async def launch(self, _spec):
                return "handle"

            async def submit_turn(self, _handle, _turn, idempotency_key):
                self.idempotency_key = idempotency_key

            if wrap_in_coroutine:

                async def events(self, _handle):
                    return stream()

            else:

                def events(self, _handle):
                    return stream()

        class _Runtime:
            config = type("Config", (), {"agent": "codex", "cwd": "/tmp/project"})()
            transports = {"codex_app_server": _Transport()}

            def describe(self):
                return {
                    "agent": "codex",
                    "agent_status": {"available": True, "capabilities": {}},
                }

        return _Runtime(), closed

    def _run_streaming_smoke(self, events, *, wrap_in_coroutine=False, timeout=1.0):
        runtime, closed = self._streaming_runtime(events, wrap_in_coroutine=wrap_in_coroutine)
        with patch.object(channel_native_debug.ChannelNativeRuntime, "from_env", return_value=runtime):
            payload = asyncio.run(
                channel_native_debug.debug_agent_smoke(
                    agent="", live=True, prompt="hello", timeout=timeout
                )
            )
        return payload, closed

    def test_agent_smoke_live_drains_streaming_transport(self):
        """codex's `events()` is an async generator; `list()` on it raises.

        That TypeError killed the codex smoke before it reached the agent, so
        the release gate's real-environment check never ran.
        """
        payload, closed = self._run_streaming_smoke(
            [
                AgentEvent(
                    AgentEventType.TOOL_STARTED,
                    {"tool_id": "p1", "tool_name": "apply_patch", "summary": "src/a.py"},
                ),
                AgentEvent(
                    AgentEventType.TOOL_COMPLETED,
                    {"tool_id": "p1", "tool_name": "apply_patch", "summary": "src/a.py"},
                ),
                AgentEvent(AgentEventType.TURN_COMPLETED, {}),
            ]
        )

        self.assertTrue(payload["ok"], payload)
        self.assertNotIn("drain_error", payload)
        self.assertEqual(payload["event_count"], 3)
        self.assertEqual(
            payload["tool_events"],
            [
                {"kind": "started", "tool_name": "apply_patch", "summary": "src/a.py"},
                {"kind": "completed", "tool_name": "apply_patch", "summary": "src/a.py"},
            ],
        )
        self.assertTrue(closed, "the event stream must be closed")

    def test_agent_smoke_live_fails_when_turn_never_closes(self):
        """A half-drained turn is not a pass — false green here hides outages."""
        payload, _ = self._run_streaming_smoke(
            [
                AgentEvent(
                    AgentEventType.TOOL_STARTED,
                    {"tool_id": "p1", "tool_name": "apply_patch", "summary": "src/a.py"},
                )
            ]
        )

        self.assertFalse(payload["ok"], payload)
        self.assertEqual(payload["error"], "agent_drain_incomplete")
        self.assertIn("without a turn-closing event", payload["drain_error"])
        self.assertEqual(payload["event_count"], 1)

    def test_agent_smoke_live_times_out_instead_of_reporting_success(self):
        """A stalled stream must fail, not return the events it managed to get."""

        async def stalling():
            yield AgentEvent(AgentEventType.TOOL_STARTED, {"tool_id": "p1", "tool_name": "shell"})
            await asyncio.sleep(30)
            yield AgentEvent(AgentEventType.TURN_COMPLETED, {})

        events, error = asyncio.run(
            channel_native_debug._drain_agent_events(stalling(), timeout=0.05)
        )

        self.assertEqual(len(events), 1)
        self.assertIn("timed out", error)

    def test_agent_smoke_live_awaits_before_detecting_the_stream(self):
        """ClaudeHeadlessTransport returns a coroutine that yields the stream."""
        payload, closed = self._run_streaming_smoke(
            [AgentEvent(AgentEventType.TURN_COMPLETED, {})], wrap_in_coroutine=True
        )

        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["event_count"], 1)
        self.assertTrue(closed)

    def test_runtime_debug_reports_competing_consumers_without_full_command(self):
        ps_output = "\n".join(
            [
                "  101     1 /opt/python /Users/alpha/.local/bin/walkcode serve",
                "  102     1 uv run python -m walkcode native serve --once --poll-timeout 0",
                "  103     1 /opt/python scripts/channel_native_debug.py runtime",
            ]
        )

        consumers = channel_native_debug._parse_competing_consumers(ps_output, current_pid=103)

        self.assertEqual(len(consumers), 2)
        self.assertEqual(consumers[0]["kind"], "legacy_walkcode_serve")
        self.assertEqual(consumers[0]["command"], "walkcode serve")
        self.assertEqual(consumers[1]["kind"], "channel_native_serve")
        self.assertEqual(consumers[1]["command"], "walkcode native serve")
        self.assertNotIn("/Users/alpha", str(consumers))

    def test_runtime_debug_fails_when_competing_consumer_exists(self):
        result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="  101     1 /opt/python /Users/alpha/.local/bin/walkcode serve\n",
            stderr="",
        )

        with patch.object(channel_native_debug.subprocess, "run", return_value=result):
            payload = channel_native_debug.debug_runtime_processes()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["competing_consumer_count"], 1)
        self.assertIn("stop competing", payload["warnings"][0])

    def test_telegram_debug_allows_other_channel_native_consumers(self):
        result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                "  101     1 /opt/python /Users/alpha/.local/bin/walkcode native serve --poll-timeout 5\n"
                "  102     1 /opt/python /Users/alpha/.local/bin/walkcode native serve --poll-timeout 5\n"
            ),
            stderr="",
        )

        with patch.object(channel_native_debug.subprocess, "run", return_value=result):
            payload = channel_native_debug.debug_runtime_processes(allow_channel_native=True)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["competing_consumer_count"], 0)
        self.assertEqual(payload["native_consumer_count"], 2)
        self.assertIn("Telegram 409", payload["warnings"][0])

    def test_runtime_debug_allows_managed_per_agent_launchd_services(self):
        ps_result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                "  101     1 /opt/python /Users/alpha/.local/bin/walkcode native serve --poll-timeout 5\n"
                "  102     1 /opt/python /Users/alpha/.local/bin/walkcode native serve --poll-timeout 5\n"
            ),
            stderr="",
        )
        launchctl_result = subprocess.CompletedProcess(
            args=["launchctl", "list"],
            returncode=0,
            stdout=(
                "101\t0\tcom.walkcode.telegram-claude\n"
                "102\t0\tcom.walkcode.telegram-codex\n"
            ),
            stderr="",
        )

        with (
            patch.object(channel_native_debug.subprocess, "run", side_effect=[ps_result, launchctl_result]),
            patch.object(channel_native_debug, "_detect_legacy_runtime_remnants", return_value=[]),
            patch.dict(
                channel_native_debug.os.environ,
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake-token",
                    "WALKCODE_AGENT": "claude",
                },
                clear=True,
            ),
        ):
            payload = channel_native_debug.debug_runtime_processes()

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["expected_service_label"], "com.walkcode.telegram-claude")
        self.assertEqual(payload["competing_consumer_count"], 0)
        self.assertEqual(payload["native_consumer_count"], 2)
        self.assertEqual(payload["managed_native_consumer_count"], 2)

    def test_runtime_debug_fails_for_unmanaged_native_consumer(self):
        ps_result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout="  101     1 /opt/python /Users/alpha/.local/bin/walkcode native serve --poll-timeout 5\n",
            stderr="",
        )
        launchctl_result = subprocess.CompletedProcess(
            args=["launchctl", "list"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with (
            patch.object(channel_native_debug.subprocess, "run", side_effect=[ps_result, launchctl_result]),
            patch.object(channel_native_debug, "_detect_legacy_runtime_remnants", return_value=[]),
            patch.dict(
                channel_native_debug.os.environ,
                {
                    "WALKCODE_CHANNEL": "telegram",
                    "TELEGRAM_BOT_TOKEN": "fake-token",
                    "WALKCODE_AGENT": "claude",
                },
                clear=True,
            ),
        ):
            payload = channel_native_debug.debug_runtime_processes()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["competing_consumer_count"], 1)
        self.assertEqual(payload["native_consumer_count"], 1)
        self.assertEqual(payload["managed_native_consumer_count"], 0)

    def test_runtime_debug_detects_legacy_remnants(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            launch_agents = home / "Library" / "LaunchAgents"
            launch_agents.mkdir(parents=True)
            (launch_agents / "com.walkcode.plist").write_text("walkcode serve", encoding="utf-8")
            (launch_agents / "com.walkcode-codex.plist").write_text(
                """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/alpha/.local/bin/walkcode</string>
    <string>serve</string>
  </array>
</dict>
</plist>
""",
                encoding="utf-8",
            )
            claude = home / ".claude"
            claude.mkdir()
            (claude / "settings.json").write_text('{"hook": "walkcode hook sync"}', encoding="utf-8")
            wrappers = home / ".agent-control-plane"
            wrappers.mkdir()
            (wrappers / "agent-wrappers.sh").write_text(
                "codex() { tmux new-session walkcode hook sync; command codex --yolo \"$@\"; }",
                encoding="utf-8",
            )
            (home / ".zprofile").write_text("source ~/.agent-control-plane/agent-wrappers.sh", encoding="utf-8")
            walkcode = home / ".walkcode"
            walkcode.mkdir()
            (walkcode / "claude.env").write_text("FEISHU_APP_ID=cli_xxx", encoding="utf-8")
            selected_env = walkcode / "selected.env"
            selected_env.write_text("TELEGRAM_BOT_TOKEN=x\n", encoding="utf-8")

            with patch.dict(
                channel_native_debug.os.environ,
                {
                    "WALKCODE_ENV_FILE": str(selected_env),
                    "FEISHU_APP_ID": "shell-old",
                    "WALKCODE_DEFAULT_TRANSPORT": "claude_headless",
                },
                clear=True,
            ):
                remnants = channel_native_debug._detect_legacy_runtime_remnants(home=home)

        kinds = {item["kind"] for item in remnants}
        self.assertIn("legacy_launch_agent", kinds)
        self.assertIn("legacy_hook", kinds)
        self.assertIn("shell_wrapper", kinds)
        self.assertIn("codex_wrapper_approval_override", kinds)
        self.assertIn("shell_startup_wrapper_source", kinds)
        self.assertIn("legacy_feishu_env", kinds)
        self.assertIn("legacy_shell_feishu_env", kinds)
        self.assertIn("removed_runtime_env", kinds)
        self.assertIn("missing_agent_binding", kinds)

    def test_runtime_debug_allows_v3_pass_through_agent_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            wrappers = home / ".agent-control-plane"
            wrappers.mkdir()
            (wrappers / "agent-wrappers.sh").write_text(
                "\n".join(
                    [
                        'claude() { command claude "$@"; }',
                        'codex() { command codex "$@"; }',
                    ]
                ),
                encoding="utf-8",
            )
            (home / ".zshrc").write_text("source ~/.agent-control-plane/agent-wrappers.sh", encoding="utf-8")

            with patch.dict(channel_native_debug.os.environ, {}, clear=True):
                remnants = channel_native_debug._detect_legacy_runtime_remnants(home=home)

        self.assertEqual(remnants, [])

    def test_telegram_debug_blocks_when_competing_consumer_exists(self):
        class _Runtime:
            async def diagnose_telegram_ingress(self, *, limit):
                self.limit = limit
                return {
                    "bot": {"ok": True},
                    "webhook": {"ok": True},
                    "pending_updates": {"count": 0, "items": []},
                    "safe_to_run_serve_once": True,
                }

        process_report = {
            "ok": False,
            "current_pid": 99,
            "competing_consumer_count": 1,
            "competing_consumers": [
                {
                    "pid": 101,
                    "ppid": 1,
                    "kind": "legacy_walkcode_serve",
                    "command": "walkcode serve",
                }
            ],
            "warnings": ["stop competing walkcode serve process(es) before consuming IM updates"],
        }

        with (
            patch.object(channel_native_debug.ChannelNativeRuntime, "from_env", return_value=_Runtime()),
            patch.object(channel_native_debug, "debug_runtime_processes", return_value=process_report),
        ):
            payload = asyncio.run(channel_native_debug.debug_telegram(limit=5))

        self.assertFalse(payload["ok"])
        self.assertFalse(payload["safe_to_run_serve_once"])
        self.assertEqual(payload["runtime_processes"]["competing_consumer_count"], 1)
        self.assertIn("competing walkcode serve", payload["warnings"][0])

    def test_telegram_debug_treats_running_native_service_409_as_healthy(self):
        class _Runtime:
            async def diagnose_telegram_ingress(self, *, limit):
                return {
                    "bot": {"ok": True},
                    "webhook": {"ok": True, "pending_update_count": 0},
                    "pending_updates": {
                        "count": 0,
                        "limit": limit,
                        "error": "HTTPError",
                        "message": "HTTP Error 409: Conflict",
                        "items": [],
                    },
                    "safe_to_run_serve_once": False,
                    "warnings": ["could not inspect Telegram pending updates"],
                }

        process_report = {
            "ok": True,
            "current_pid": 99,
            "competing_consumer_count": 0,
            "competing_consumers": [],
            "native_consumer_count": 1,
            "native_consumers": [
                {
                    "pid": 101,
                    "ppid": 1,
                    "kind": "channel_native_serve",
                    "command": "walkcode native serve",
                }
            ],
            "legacy_remnant_count": 0,
            "legacy_remnants": [],
            "warnings": ["walkcode native serve process(es) are running"],
        }

        with (
            patch.object(channel_native_debug.ChannelNativeRuntime, "from_env", return_value=_Runtime()),
            patch.object(channel_native_debug, "debug_runtime_processes", return_value=process_report),
        ):
            payload = asyncio.run(channel_native_debug.debug_telegram(limit=5))

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["safe_to_run_serve_once"])
        self.assertTrue(payload["polling_owned_by_running_service"])
        self.assertNotIn("could not inspect Telegram pending updates", payload["warnings"])
        self.assertIn("expected to return 409", payload["warnings"][0])


if __name__ == "__main__":
    unittest.main()
