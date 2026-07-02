import json
import unittest

from walkcode.channel_native.lark_cards import (
    PLAN_BODY_LIMIT,
    POST_TEXT_LIMIT,
    TOOL_INPUT_LIMIT,
    escape_lark_md,
    render_lark_message,
)


def _buttons(card_content: dict) -> list[dict]:
    buttons = []
    for element in card_content.get("elements", []):
        if element.get("tag") == "action":
            buttons.extend(element.get("actions", []))
    return buttons


class EscapeLarkMdTests(unittest.TestCase):
    def test_escapes_link_mention_and_inline_format_markers(self):
        hostile = "[click](http://evil) <at id=1> **bold** `code`"

        escaped = escape_lark_md(hostile)

        self.assertNotIn("[click](", escaped)
        self.assertIn("\\[click\\]\\(", escaped)
        self.assertIn("\\<at", escaped)
        self.assertIn("\\*\\*", escaped)
        self.assertIn("\\`", escaped)

    def test_empty_text_passes_through(self):
        self.assertEqual(escape_lark_md(""), "")


class PermissionCardTests(unittest.TestCase):
    def _view(self, **overrides):
        view = {
            "type": "permission_prompt",
            "interaction_id": "i1",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf /tmp/x"},
            "high_risk": False,
            "actions": [
                {"action": "allow_once", "label": "Allow once", "token": "tok-allow"},
                {"action": "always_allow", "label": "Always allow", "token": "tok-always"},
                {"action": "deny", "label": "Deny", "token": "tok-deny"},
            ],
        }
        view.update(overrides)
        return view

    def test_renders_interactive_card_with_token_button_values(self):
        message = render_lark_message(self._view())

        self.assertEqual(message["msg_type"], "interactive")
        content = message["content"]
        self.assertEqual(content["header"]["template"], "orange")
        buttons = _buttons(content)
        self.assertEqual(
            [button["value"] for button in buttons],
            [
                {"action": "allow_once", "token": "tok-allow"},
                {"action": "always_allow", "token": "tok-always"},
                {"action": "deny", "token": "tok-deny"},
            ],
        )
        self.assertEqual([button["type"] for button in buttons], ["primary", "primary", "danger"])
        body = content["elements"][0]["text"]["content"]
        self.assertIn("rm -rf /tmp/x", body)

    def test_high_risk_uses_red_template(self):
        message = render_lark_message(self._view(high_risk=True))

        self.assertEqual(message["content"]["header"]["template"], "red")
        self.assertIn("高风险", message["content"]["header"]["title"]["content"])

    def test_plan_input_renders_plan_body_with_blue_template(self):
        long_plan = "步骤\n" * 600
        message = render_lark_message(
            self._view(tool_name="ExitPlanMode", tool_input={"plan": long_plan})
        )

        content = message["content"]
        self.assertEqual(content["header"]["template"], "blue")
        body = content["elements"][0]["text"]["content"]
        self.assertLessEqual(len(body), PLAN_BODY_LIMIT + 200)

    def test_tool_input_json_is_truncated(self):
        message = render_lark_message(self._view(tool_input={"data": "x" * 2000}))

        body = message["content"]["elements"][0]["text"]["content"]
        self.assertLessEqual(len(body), TOOL_INPUT_LIMIT + 200)
        self.assertIn("...", body)


class AskUserQuestionCardTests(unittest.TestCase):
    def test_single_select_renders_primary_option_buttons(self):
        message = render_lark_message(
            {
                "type": "ask_user_question",
                "prompt": "选择部署环境",
                "actions": [
                    {"action": "answer:0:0", "label": "staging", "token": "t1"},
                    {"action": "answer:0:1", "label": "production", "token": "t2"},
                    {"action": "other:0", "label": "Other", "token": "t3"},
                ],
            }
        )

        content = message["content"]
        self.assertEqual(content["header"]["title"]["content"], "选择部署环境")
        buttons = _buttons(content)
        self.assertEqual(buttons[0]["value"], {"action": "answer:0:0", "token": "t1"})
        self.assertEqual(buttons[0]["type"], "primary")
        self.assertIn("其他", buttons[2]["text"]["content"])

    def test_multi_select_marks_toggled_options_and_submit(self):
        message = render_lark_message(
            {
                "type": "ask_user_question",
                "prompt": "选择要启用的功能",
                "actions": [
                    {"action": "toggle:0:0", "label": "[x] cache", "token": "t1"},
                    {"action": "toggle:0:1", "label": "metrics", "token": "t2"},
                    {"action": "submit:0", "label": "Submit", "token": "t3"},
                ],
            }
        )

        buttons = _buttons(message["content"])
        self.assertEqual(buttons[0]["text"]["content"], "✓ cache")
        self.assertEqual(buttons[0]["type"], "primary")
        self.assertEqual(buttons[1]["text"]["content"], "metrics")
        self.assertEqual(buttons[1]["type"], "default")
        self.assertIn("✅", buttons[2]["text"]["content"])

    def test_question_without_options_prompts_thread_reply(self):
        message = render_lark_message({"type": "ask_user_question", "prompt": "只能自由回答"})

        body = message["content"]["elements"][0]["text"]["content"]
        self.assertIn("回复文本", body)


class HealthCardTests(unittest.TestCase):
    def test_running_health_card_shows_status_session_and_duration(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "walkcode | fix bug",
                "session_id": "sess-1234",
                "transport": "claude_headless",
                "elapsed": 125.0,
                "cwd": "/tmp/project",
                "lifecycle_state": "RUNNING_TURN",
                "writer_owner": "channel",
                "readonly": False,
                "actions": [],
            }
        )

        content = message["content"]
        self.assertEqual(content["header"]["template"], "blue")
        rendered = json.dumps(content, ensure_ascii=False)
        self.assertIn("运行中", rendered)
        self.assertIn("sess-1234", rendered)
        self.assertIn("2分05秒", rendered)

    def test_readonly_observed_session_mentions_takeover(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "tui session",
                "session_id": "s1",
                "transport": "codex_app_server",
                "elapsed": 5.0,
                "cwd": "/tmp",
                "readonly": True,
            }
        )

        self.assertIn("只读", json.dumps(message["content"], ensure_ascii=False))

    def test_error_health_card_uses_red_and_shows_reason(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "error",
                "title": "t",
                "session_id": "s1",
                "transport": "claude_headless",
                "elapsed": 1.0,
                "cwd": "/tmp",
                "reason": "agent crashed",
            }
        )

        self.assertEqual(message["content"]["header"]["template"], "red")
        self.assertIn("agent crashed", json.dumps(message["content"], ensure_ascii=False))


class OtherViewTests(unittest.TestCase):
    def test_turn_completed_message_renders_as_post_md(self):
        message = render_lark_message({"type": "turn_completed", "message": "**done**"})

        self.assertEqual(message["msg_type"], "post")
        self.assertEqual(
            message["content"]["zh_cn"]["content"][0][0],
            {"tag": "md", "text": "**done**"},
        )

    def test_long_post_text_is_clipped_below_lark_limit(self):
        message = render_lark_message({"type": "turn_delta", "text": "x" * 40000})

        text = message["content"]["zh_cn"]["content"][0][0]["text"]
        self.assertLessEqual(len(text), POST_TEXT_LIMIT + 10)

    def test_tool_progress_renders_compact_post(self):
        message = render_lark_message(
            {"type": "tool_progress", "status": "completed", "tool_name": "Bash", "summary": "ls -la"}
        )

        self.assertEqual(message["msg_type"], "post")
        text = message["content"]["zh_cn"]["content"][0][0]["text"]
        self.assertIn("✅", text)
        self.assertIn("Bash", text)

    def test_takeover_confirmation_renders_all_three_buttons(self):
        message = render_lark_message(
            {
                "type": "takeover_confirmation",
                "summary": "fix the login bug",
                "actions": [
                    {"action": "confirm_takeover", "label": "Confirm takeover and send", "token": "t1"},
                    {"action": "keep_readonly", "label": "Keep read-only", "token": "t2"},
                    {"action": "manual_instructions", "label": "Manual steps", "token": "t3"},
                ],
            }
        )

        buttons = _buttons(message["content"])
        self.assertEqual(len(buttons), 3)
        self.assertEqual(buttons[0]["type"], "primary")

    def test_unknown_view_falls_back_to_text(self):
        message = render_lark_message(
            {"type": "some_new_view"}, fallback_text="fallback body"
        )

        self.assertEqual(message["msg_type"], "post")
        self.assertIn("fallback body", message["content"]["zh_cn"]["content"][0][0]["text"])

    def test_hostile_reason_is_escaped_in_card_body(self):
        message = render_lark_message(
            {
                "type": "manual_only",
                "reason": "[bold](http://evil) <at id=1>",
                "suggested_steps": ["step **one**"],
            }
        )

        body = message["content"]["elements"][0]["text"]["content"]
        self.assertNotIn("[bold](", body)
        self.assertIn("\\<at", body)


if __name__ == "__main__":
    unittest.main()
