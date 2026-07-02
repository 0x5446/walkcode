import io
import json
import sys
import unittest
from unittest.mock import patch

from walkcode import __main__ as main
from walkcode.channel_native import ChannelConfigError, SubmitResult
from walkcode import channel_native_runtime


class _FakeRuntime:
    def __init__(self, hook_result=None):
        self.polled = []
        self.hooks = []
        self.deferred_hooks = []
        self.hook_result = hook_result or SubmitResult(True)

    def describe(self):
        return {
            "channel": {"kind": "telegram", "live_ingress": "polling", "configured": True},
            "agent": "claude",
            "e2e_gates": {
                "telegram": {
                    "enabled": False,
                    "missing": ["WALKCODE_E2E_TELEGRAM_CHAT_ID"],
                    "reason": "missing required env for telegram E2E: WALKCODE_E2E_TELEGRAM_CHAT_ID",
                }
            },
            "agent_status": {
                "available": True,
            },
            "state_path": "/tmp/state.json",
            "cwd": "/tmp/project",
        }

    async def poll_telegram_once(self, *, timeout, limit):
        self.polled.append({"timeout": timeout, "limit": limit})
        return 2

    async def diagnose_telegram_ingress(self, *, limit):
        return {
            "channel": {
                "kind": "telegram",
                "polling_enabled": True,
                "allowlist_configured": True,
                "allowlist_count": 1,
                "allowlist_matches_existing_session": True,
            },
            "bot": {"ok": True, "username": "walkcode_test_bot"},
            "webhook": {"ok": True, "has_url": False, "pending_update_count": 1, "last_error_present": False},
            "pending_updates": {
                "count": 1,
                "limit": limit,
                "items": [
                    {
                        "index": 0,
                        "event_kind": "message",
                        "chat_allowed": True,
                        "chat_matches_existing_session": True,
                        "text_present": True,
                        "attachment_count": 0,
                    }
                ],
            },
            "safe_to_run_serve_once": True,
            "warnings": [],
            "note": "diagnostic getUpdates does not advance Telegram offset",
        }

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

        self.assertEqual(payload["channel"]["kind"], "telegram")
        self.assertEqual(payload["channel"]["live_ingress"], "polling")
        self.assertEqual(payload["agent"], "claude")
        self.assertFalse(payload["e2e_gates"]["telegram"]["enabled"])

    def test_native_serve_once_polls_once_and_exits(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "serve", "--once", "--poll-timeout", "0", "--limit", "7"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        self.assertEqual(runtime.polled, [{"timeout": 0, "limit": 7}])
        self.assertIn("processed 2 update(s)", stdout.getvalue())

    def test_native_debug_telegram_json_peeks_without_serving(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "debug", "telegram", "--json", "--limit", "3"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["bot"]["username"], "walkcode_test_bot")
        self.assertEqual(payload["pending_updates"]["limit"], 3)
        self.assertTrue(payload["safe_to_run_serve_once"])
        self.assertEqual(runtime.polled, [])

    def test_native_doctor_text_reports_e2e_gate_status(self):
        runtime = _FakeRuntime()
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "doctor"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout:
            main.main()

        output = stdout.getvalue()
        self.assertIn("e2e_gates:", output)
        self.assertIn("telegram: enabled=False", output)
        self.assertIn("WALKCODE_E2E_TELEGRAM_CHAT_ID", output)

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
        runtime = _FakeRuntime()
        stdin = io.StringIO(json.dumps({"session_id": "claude-session-1", "message": "done"}))
        with patch.object(channel_native_runtime.ChannelNativeRuntime, "from_env", return_value=runtime), \
             patch.object(sys, "argv", ["walkcode", "native", "hook", "Stop", "--agent", "claude", "--defer"]), \
             patch("sys.stdin", stdin), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch("sys.stderr", new_callable=io.StringIO) as stderr:
            with self.assertRaises(SystemExit) as raised:
                main.main()

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(runtime.hooks, [])
        self.assertEqual(runtime.deferred_hooks[0]["hook_type"], "Stop")
        self.assertTrue(runtime.deferred_hooks[0]["payload"]["_walkcode_infer_tui_pid"])
        self.assertIn("_walkcode_hook_parent_pid", runtime.deferred_hooks[0]["payload"])
        self.assertIn("_walkcode_hook_process_tree", runtime.deferred_hooks[0]["payload"])

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
