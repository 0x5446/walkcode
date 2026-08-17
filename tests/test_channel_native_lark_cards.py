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
    def test_single_question_renders_option_buttons(self):
        message = render_lark_message(
            {
                "type": "ask_user_question",
                "questions": [
                    {
                        "index": 0,
                        "prompt": "选择部署环境",
                        "header": "部署环境",
                        "allow_multiple": False,
                        "options": [
                            {"action": "answer:0:0", "label": "staging", "selected": False, "token": "t1"},
                            {"action": "answer:0:1", "label": "production", "selected": False, "token": "t2"},
                        ],
                        "other": None,
                        "answer_display": "",
                    }
                ],
                "submit": None,
            }
        )

        buttons = _buttons(message["content"])
        self.assertEqual(buttons[0]["value"], {"action": "answer:0:0", "token": "t1"})
        self.assertEqual([b["text"]["content"] for b in buttons], ["staging", "production"])

    def test_batch_questions_render_as_local_state_form(self):
        message = render_lark_message(
            {
                "type": "ask_user_question",
                "questions": [
                    {
                        "index": 0, "prompt": "周末?", "header": "周末计划", "allow_multiple": False,
                        "options": [
                            {"action": "set:0:0", "label": "宅", "selected": False, "token": "a"},
                            {"action": "set:0:1", "label": "出门浪", "selected": False, "token": "b"},
                        ],
                        "other": None, "answer_display": "",
                    },
                    {
                        "index": 1, "prompt": "口味?", "header": "吃啥", "allow_multiple": True,
                        "options": [
                            {"action": "toggle:1:0", "label": "辣", "selected": False, "token": "c"},
                            {"action": "toggle:1:1", "label": "清淡", "selected": False, "token": "d"},
                        ],
                        "other": {"action": "other:1", "token": "e"}, "answer_display": "",
                    },
                ],
                "submit": {"action": "submit_all", "label": "Submit", "token": "s"},
            }
        )

        # Batch mode must be a form container: selections stage client-side and
        # one form_submit callback carries every field at once.
        form = message["content"]["elements"][0]
        self.assertEqual(form["tag"], "form")
        by_tag: dict[str, list] = {}
        for element in form["elements"]:
            by_tag.setdefault(element.get("tag", ""), []).append(element)
        # Feishu silently drops the WHOLE form if it contains a div child;
        # question titles must use the markdown component (live-verified).
        self.assertNotIn("div", by_tag)
        self.assertIn("markdown", by_tag)
        self.assertEqual([e["name"] for e in by_tag["select_static"]], ["q0"])
        self.assertEqual([e["name"] for e in by_tag["multi_select_static"]], ["q1"])
        self.assertEqual([e["name"] for e in by_tag["input"]], ["q1_other"])
        option_values = [o["value"] for o in by_tag["select_static"][0]["options"]]
        self.assertEqual(option_values, ["0", "1"])
        submit = by_tag["button"][0]
        self.assertEqual(submit["action_type"], "form_submit")
        self.assertEqual(submit["value"], {"action": "submit_all", "token": "s"})


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

    def test_health_card_shows_agent_session_id_next_to_walkcode_id(self):
        # The card used to print only the WalkCode ledger key, so anyone who
        # copied it into `codex resume` got "no such session".
        message = render_lark_message(
            {
                "type": "health",
                "status": "idle",
                "title": "t",
                "session_id": "sess-51cb47e7",
                "agent_session_id": "01a00de8-62bc-73e3-bd3c-1fda27f272ac",
                "transport": "codex_app_server",
                "elapsed": 5.0,
                "cwd": "/tmp",
            }
        )

        rendered = json.dumps(message["content"], ensure_ascii=False)
        self.assertIn("**Session**: `01a00de8-62bc-73e3-bd3c-1fda27f272ac`", rendered)
        self.assertIn("**WalkCode**: `sess-51cb47e7`", rendered)

    def test_health_card_without_agent_session_id_keeps_walkcode_id(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "t",
                "session_id": "sess-1234",
                "transport": "codex_app_server",
                "elapsed": 5.0,
                "cwd": "/tmp",
            }
        )

        rendered = json.dumps(message["content"], ensure_ascii=False)
        self.assertIn("**WalkCode**: `sess-1234`", rendered)
        self.assertNotIn("**Session**: `", rendered)

    def test_health_card_strips_backticks_from_agent_session_id(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "t",
                "session_id": "sess-1",
                "agent_session_id": "abc`**bold**`",
                "transport": "codex_app_server",
                "elapsed": 1.0,
                "cwd": "/tmp",
            }
        )

        rendered = json.dumps(message["content"], ensure_ascii=False)
        self.assertNotIn("abc`", rendered)

    def test_health_card_shows_model_and_context_usage(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "t",
                "session_id": "s1",
                "transport": "claude_headless",
                "elapsed": 5.0,
                "cwd": "/tmp",
                "model": "claude-opus-4-8-20260610",
                "context_used": 123_456,
                "context_limit": 200_000,
            }
        )

        rendered = json.dumps(message["content"], ensure_ascii=False)
        self.assertIn("claude-opus-4-8-20260610", rendered)
        self.assertIn("123.5k / 200k（62%）", rendered)

    def test_health_card_without_model_shows_placeholder(self):
        message = render_lark_message(
            {
                "type": "health",
                "status": "running",
                "title": "t",
                "session_id": "s1",
                "transport": "codex_app_server",
                "elapsed": 5.0,
                "cwd": "/tmp",
            }
        )

        rendered = json.dumps(message["content"], ensure_ascii=False)
        self.assertIn("**模型**: —", rendered)
        self.assertIn("**上下文**: —", rendered)

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

    def test_tool_progress_renders_editable_card(self):
        message = render_lark_message(
            {"type": "tool_progress", "status": "completed", "tool_name": "Bash", "summary": "ls -la"}
        )

        # Must be an interactive card so a burst can be patched in place
        # (post messages are not patchable via im.message.patch).
        self.assertEqual(message["msg_type"], "interactive")
        body = message["content"]["elements"][0]["text"]["content"]
        self.assertIn("✅", body)
        self.assertIn("Bash", body)

    def test_tool_progress_lines_coalesce_into_one_card(self):
        message = render_lark_message(
            {
                "type": "tool_progress",
                "lines": [
                    {"tool_name": "Bash", "status": "completed", "summary": "ls"},
                    {"tool_name": "Read", "status": "running", "summary": ""},
                ],
            }
        )

        self.assertEqual(message["msg_type"], "interactive")
        body = message["content"]["elements"][0]["text"]["content"]
        self.assertIn("✅ `Bash`", body)
        self.assertIn("⏳ `Read`", body)

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

    def test_model_choice_decision_result_renders_green_switch_card(self):
        message = render_lark_message(
            {
                "type": "decision_result",
                "kind": "model_choice",
                "action": "claude-fable-5",
                "detail": "模型已切换：claude-fable-5",
            }
        )

        content = message["content"]
        self.assertEqual(content["header"]["template"], "green")
        rendered = json.dumps(content, ensure_ascii=False)
        self.assertIn("claude-fable-5", rendered)

    def test_stale_decision_result_renders_orange_expired_card(self):
        message = render_lark_message(
            {
                "type": "decision_result",
                "kind": "ask_user_question",
                "action": "stale",
                "detail": "会话进程已重启，这张卡片已失效。",
            }
        )

        content = message["content"]
        self.assertEqual(content["header"]["template"], "orange")
        rendered = json.dumps(content, ensure_ascii=False)
        self.assertIn("已失效", rendered)
        # stale must win over the ask_user_question green "已回答" branch
        self.assertNotIn("已回答", rendered)

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
