import io
import json
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from walkcode import __main__ as main
from walkcode.channel_native import ChannelConfigError, SubmitResult
from walkcode import channel_native_runtime


class _FakeRuntime:
    def __init__(self, hook_result=None):
        self.served = []
        self.hooks = []
        self.deferred_hooks = []
        self.hook_result = hook_result or SubmitResult(True)
        self.config = SimpleNamespace(
            channel_kind="lark",
            channel=SimpleNamespace(kind="lark"),
            agent="claude",
            profile="work",
        )

    def describe(self):
        return {
            "channel": {"kind": "lark", "live_ingress": "websocket", "configured": True},
            "agent": "claude",
            "e2e_gates": {
                "lark": {
                    "enabled": False,
                    "missing": ["WALKCODE_E2E_LARK_CHAT_ID"],
                    "reason": "missing required env for lark E2E: WALKCODE_E2E_LARK_CHAT_ID",
                }
            },
            "agent_status": {
                "available": True,
            },
            "state_path": "/tmp/state.json",
            "cwd": "/tmp/project",
        }

    async def serve_lark_ws(self):
        self.served.append("lark_ws")

    async def process_tui_hook(self, *, hook_type, payload, agent=""):
        self.hooks.append({"hook_type": hook_type, "payload": dict(payload), "agent": agent})
        return self.hook_result

    def defer_tui_hook(self, *, hook_type, payload, agent=""):
        self.deferred_hooks.append({"hook_type": hook_type, "payload": dict(payload), "agent": agent})
        return {"queued": True, "id": "queued-1", "path": "/tmp/state.json.tui-hooks.d/queued-1.json"}


class ChannelNativeCliTests(unittest.TestCase):
    def test_native_doctor_json_reports_v3_runtime_status(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "doctor", "--json"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["channel"]["kind"], "lark")
        self.assertEqual(payload["channel"]["live_ingress"], "websocket")
        self.assertEqual(payload["agent"], "claude")
        self.assertFalse(payload["e2e_gates"]["lark"]["enabled"])

    def test_native_serve_runs_lark_websocket_ingress(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.dict("os.environ", {}, clear=False), \
             patch.object(sys, "argv", ["walkcode", "native", "serve"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        self.assertEqual(runtime.served, ["lark_ws"])
        self.assertIn("listening via Lark WebSocket", stdout.getvalue())

    def test_native_serve_rejects_retired_polling_flags(self):
        for flag in ("--once", "--poll-timeout", "--limit"):
            with patch.object(sys, "argv", ["walkcode", "native", "serve", flag, "1"]), \
                 patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    main.main()
            self.assertEqual(raised.exception.code, 2)

    def test_native_debug_telegram_is_gone(self):
        with patch.object(sys, "argv", ["walkcode", "native", "debug", "telegram"]), \
             patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as raised:
                main.main()
        self.assertEqual(raised.exception.code, 2)

    def test_native_doctor_text_reports_e2e_gate_status(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "doctor"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        output = stdout.getvalue()
        self.assertIn("e2e_gates:", output)
        self.assertIn("lark: enabled=False", output)
        self.assertIn("WALKCODE_E2E_LARK_CHAT_ID", output)

    def test_native_config_error_exits_without_traceback(self):
        with patch.object(
            channel_native_runtime.ChannelNativeRuntime,
            "from_env",
            side_effect=ChannelConfigError("no channel configured"),
        ), patch.object(sys, "argv", ["walkcode", "native", "doctor"]), \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("channel-native config error: no channel configured", stderr.getvalue())

    def test_native_hook_reads_json_stdin_and_dispatches_to_runtime(self):
        runtime = _FakeRuntime()
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "hook", "stop", "--agent", "claude", "--json"]), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(payload["accepted"])
        self.assertEqual(runtime.hooks[0]["hook_type"], "stop")
        self.assertEqual(runtime.hooks[0]["agent"], "claude")
        self.assertEqual(runtime.hooks[0]["payload"]["session_id"], "claude-session-1")
        self.assertTrue(runtime.hooks[0]["payload"]["_walkcode_infer_tui_pid"])
        self.assertIn("_walkcode_hook_parent_pid", runtime.hooks[0]["payload"])
        self.assertIn("_walkcode_hook_process_tree", runtime.hooks[0]["payload"])

    def test_native_hook_accepts_migrated_tui_hook_names(self):
        runtime = _FakeRuntime()
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "prompt": "hello"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(
                 sys,
                 "argv",
                 ["walkcode", "native", "hook", "user-prompt-submit", "--agent", "claude", "--json"],
             ), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(payload["accepted"])
        self.assertEqual(runtime.hooks[0]["hook_type"], "user-prompt-submit")

    def test_native_hook_accept_without_json_is_stdout_silent_for_tui_hooks(self):
        runtime = _FakeRuntime()
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "hook", "stop", "--agent", "claude"]), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(runtime.hooks[0]["hook_type"], "stop")

    def test_native_hook_defer_queues_locally_and_stays_stdout_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text("invalid state must never be loaded by defer")
            stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
            with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", side_effect=AssertionError("runtime initialized")), \
                 patch.object(channel_native_runtime.ChannelNativeConfig, "from_env", return_value=SimpleNamespace(state_path=str(state_path))), \
                 patch.object(channel_native_runtime, "_load_native_env", return_value={}), \
                 patch.object(sys, "argv", ["walkcode", "native", "hook", "Stop", "--agent", "claude", "--defer"]), \
                 patch("sys.stdin", stdin), \
                 patch("sys.stdout", new_callable=io.StringIO) as stdout, \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with self.assertRaises(SystemExit) as raised:
                    main.main()
            self.assertEqual(raised.exception.code, 0)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")
            files = list((Path(directory) / "state.json.tui-hooks.d").glob("*.json"))
            self.assertEqual(len(files), 1)
            queued = json.loads(files[0].read_text())
            self.assertEqual(queued["hook_type"], "Stop")
            self.assertTrue(queued["payload"]["_walkcode_infer_tui_pid"])
            self.assertIn("_walkcode_hook_parent_pid", queued["payload"])
            self.assertIn("_walkcode_hook_process_tree", queued["payload"])
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_native_hook_reject_without_json_uses_stderr_only(self):
        runtime = _FakeRuntime(hook_result=SubmitResult(False, "duplicate_inbound"))
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "hook", "stop", "--agent", "claude"]), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("native hook rejected: duplicate_inbound", stderr.getvalue())

    def test_native_hook_accepts_raw_claude_hook_names_before_runtime(self):
        runtime = _FakeRuntime()
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "hook", "Stop", "--agent", "claude", "--json"]), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 0)
        self.assertTrue(payload["accepted"])
        self.assertEqual(runtime.hooks[0]["hook_type"], "Stop")


if __name__ == "__main__":
    unittest.main()
