import unittest

from walkcode.channel_native import ChannelNativeE2EGates


class ChannelNativeE2EGateTests(unittest.TestCase):
    def test_all_gates_are_closed_by_default_with_actionable_reasons(self):
        gates = ChannelNativeE2EGates.from_env({})

        for name in ("telegram", "lark", "claude_headless", "codex_app_server"):
            result = gates.evaluate(name)
            self.assertFalse(result.enabled)
            self.assertIn("set", result.reason)
            self.assertIn("WALKCODE_E2E_", result.reason)

    def test_opted_in_gate_reports_missing_required_variables(self):
        result = ChannelNativeE2EGates.from_env({"WALKCODE_E2E_TELEGRAM": "1"}).evaluate("telegram")

        self.assertFalse(result.enabled)
        self.assertEqual(
            result.missing,
            ("TELEGRAM_BOT_TOKEN", "WALKCODE_E2E_TELEGRAM_CHAT_ID"),
        )
        self.assertIn("TELEGRAM_BOT_TOKEN", result.reason)

    def test_gate_enables_when_flag_and_requirements_are_present(self):
        result = ChannelNativeE2EGates.from_env(
            {
                "WALKCODE_E2E_TELEGRAM": "1",
                "TELEGRAM_BOT_TOKEN": "token",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_E2E_TELEGRAM_CHAT_ID": "chat",
            }
        ).evaluate("telegram")

        self.assertTrue(result.enabled)
        self.assertEqual(result.missing, ())
        self.assertEqual(result.reason, "")

    def test_lark_claude_and_codex_gates_have_separate_requirements(self):
        env = {
            "WALKCODE_E2E_LARK": "1",
            "LARK_APP_ID": "app-id",
            "WALKCODE_AGENT": "claude",
            "LARK_APP_SECRET": "secret",
            "WALKCODE_E2E_LARK_CHAT_ID": "chat",
            "WALKCODE_E2E_CLAUDE_HEADLESS": "1",
            "WALKCODE_E2E_CWD": "/tmp/project",
            "WALKCODE_E2E_CODEX_APP_SERVER": "1",
        }
        gates = ChannelNativeE2EGates.from_env(env)

        self.assertTrue(gates.evaluate("lark").enabled)
        self.assertTrue(gates.evaluate("claude_headless").enabled)
        self.assertTrue(gates.evaluate("codex_app_server").enabled)

    def test_unknown_gate_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown E2E gate"):
            ChannelNativeE2EGates.from_env({}).evaluate("unknown")


if __name__ == "__main__":
    unittest.main()
