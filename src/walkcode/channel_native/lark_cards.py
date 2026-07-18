"""Pure renderers from V3 view models to Lark/Feishu message payloads.

This module is the Lark analogue of ``render_view_text``: it turns the plain
view dicts produced by ``ViewModelFactory`` / ``Orchestrator._event_to_view``
into Feishu ``interactive`` card JSON or ``post`` rich-text content. It does no
IO and never imports the Lark SDK, so it stays unit-testable everywhere.

The card layouts are ported from the battle-tested V2 builders
(``git show main:src/walkcode/server.py``): permission card, AskUserQuestion
card, and health card. V2's ``{"rid", "b"}`` button values are replaced by the
V3 callback-token contract: every button value carries ``{"token", "action"}``,
which is exactly what ``LarkChannelAdapter.parse_event`` extracts.
"""

from __future__ import annotations

import json
from typing import Any

# Length budgets ported from V2: plan body 800, tool input JSON 500. The
# overall post budget stays below Lark's 30000-char message ceiling.
PLAN_BODY_LIMIT = 800
TOOL_INPUT_LIMIT = 500
POST_TEXT_LIMIT = 28000

_BUTTON_PRIMARY_ACTIONS = {
    "allow",
    "allow_once",
    "accept",
    "acceptForSession",
    "accept_edits",
    "plan_auto_accept",
    "always_allow",
    "confirm_takeover",
    "takeover_and_send",
}
_BUTTON_DANGER_ACTIONS = {"deny", "decline", "cancel"}

_HEALTH_TEMPLATE = {
    "running": "blue",
    "idle": "turquoise",
    "waiting_permission": "orange",
    "waiting_user": "orange",
    "stale": "orange",
    "stopped": "grey",
    "error": "red",
}
_HEALTH_STATUS_LABEL = {
    "running": "🟢 运行中",
    "idle": "🟦 空闲",
    "waiting_permission": "🟠 等待权限确认",
    "waiting_user": "🟠 等待你的回答",
    "stale": "🟠 长时间无进展",
    "stopped": "⚪ 已结束",
    "error": "🔴 出错",
}


def escape_lark_md(text: str) -> str:
    """Escape lark_md structural chars so untrusted text renders literally.

    Ported from V2: option labels / tool input / agent output flow into card
    bodies; a prompt-injected agent could otherwise craft links, mentions, or
    inline formatting that impersonates system UI. Buttons use plain_text and
    need no escaping — only lark_md divs do.
    """
    if not text:
        return text
    for ch in ("\\", "`", "*", "_", "~", "[", "]", "(", ")", "<", ">", "#", "|"):
        text = text.replace(ch, "\\" + ch)
    return text


def _clip(text: str, limit: int, marker: str = "\n...") -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + marker


def _inline(text: str) -> str:
    return str(text or "").replace("\r", " ").replace("\n", " ")


def _post_message(text: str) -> dict[str, Any]:
    return {
        "msg_type": "post",
        "content": {"zh_cn": {"content": [[{"tag": "md", "text": _clip(str(text), POST_TEXT_LIMIT)}]]}},
    }


def _card_message(title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "content": {
            # update_multi makes the card "shared": required by the message
            # PATCH endpoint that flips decided prompts into result cards —
            # patching a private (default) card is rejected by Lark, which
            # left settled permission/ask cards showing live buttons.
            "config": {"wide_screen_mode": True, "update_multi": True},
            "header": {
                "title": {"tag": "plain_text", "content": _clip(_inline(title), 120, "...")},
                "template": template,
            },
            "elements": elements,
        },
    }


def _md_div(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _button(action: dict[str, Any], *, btn_type: str | None = None, label: str | None = None) -> dict[str, Any]:
    name = str(action.get("action", ""))
    if btn_type is None:
        if name in _BUTTON_PRIMARY_ACTIONS:
            btn_type = "primary"
        elif name in _BUTTON_DANGER_ACTIONS:
            btn_type = "danger"
        else:
            btn_type = "default"
    value: dict[str, Any] = {"action": name}
    token = str(action.get("token", "") or "")
    if token:
        value["token"] = token
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": _clip(_inline(label or str(action.get("label", name))), 60, "...")},
        "type": btn_type,
        "value": value,
    }


def _action_row(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "action", "actions": buttons}


def _note(text: str) -> dict[str, Any]:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": text}]}


def _permission_card(view: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(view.get("tool_name", "") or "")
    tool_input = view.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}

    plan = str(tool_input.get("plan", "") or "")
    if plan and tool_name in {"ExitPlanMode", "exit_plan_mode"}:
        content = escape_lark_md(_clip(plan, PLAN_BODY_LIMIT))
        title = "📋 计划待确认"
        template = "blue"
    else:
        input_str = _clip(json.dumps(tool_input, indent=2, ensure_ascii=False), TOOL_INPUT_LIMIT)
        content = f"**Tool:** `{escape_lark_md(tool_name)}`\n**Input:**\n```json\n{input_str}\n```"
        title = "🔐 权限请求（高风险）" if view.get("high_risk") else "🔐 权限请求"
        template = "red" if view.get("high_risk") else "orange"

    buttons = [_button(action) for action in view.get("actions", []) if isinstance(action, dict)]
    elements: list[dict[str, Any]] = [_md_div(content)]
    if buttons:
        elements.append(_action_row(buttons))
    if view.get("dual_surface"):
        elements.append(_dual_surface_note())
    return _card_message(title, template, elements)


def _dual_surface_note() -> dict[str, Any]:
    # v3 true dual-surface (ADR 0046 v3): the native terminal dialog renders
    # at the same time as this card; whichever side answers first wins.
    return _note("💡 终端与飞书均可回答，先答先生效。")


def _ask_user_question_card(view: dict[str, Any]) -> dict[str, Any]:
    questions = view.get("questions")
    if not isinstance(questions, list):
        return _card_message("选择一个选项", "blue", [_md_div("⚠️ 无可用问题。")])
    submit = view.get("submit")
    if isinstance(submit, dict) and submit.get("token"):
        card = _ask_user_form_card(questions, submit)
    else:
        card = _ask_user_button_card(questions)
    if view.get("dual_surface"):
        card["content"]["elements"].append(_dual_surface_note())
    return card


def _ask_user_button_card(questions: list[Any]) -> dict[str, Any]:
    # Immediate mode (one simple single-select question): plain buttons, one
    # click answers everything — no form round trip needed.
    elements: list[dict[str, Any]] = []
    for q_index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        title = str(question.get("header") or question.get("prompt") or f"问题 {q_index + 1}")
        elements.append(_md_div(f"**{escape_lark_md(_inline(title))}**"))
        option_buttons = [
            _button(option)
            for option in question.get("options", [])
            if isinstance(option, dict)
        ]
        if option_buttons:
            elements.append(_action_row(option_buttons))
    return _card_message("请选择", "blue", elements)


def _ask_user_form_card(questions: list[Any], submit: dict[str, Any]) -> dict[str, Any]:
    # Batch mode uses a form container: every dropdown/input interaction stays
    # on the client, and one form_submit callback delivers all field values at
    # once (field names q{i} / q{i}_other — the server maps them back by
    # question index).
    form_elements: list[dict[str, Any]] = []
    for q_index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        header = str(question.get("header") or "")
        prompt = str(question.get("prompt") or "")
        title = header or prompt or f"问题 {q_index + 1}"
        multi = bool(question.get("allow_multiple"))
        hint = "（多选）" if multi else "（单选）"
        rows = [f"**{escape_lark_md(_inline(title))}** {hint}"]
        if header and prompt and prompt != header:
            rows.append(escape_lark_md(_inline(prompt)))
        # Feishu silently drops the ENTIRE form element when it contains a div
        # child (live-verified 2026-07-03: div → empty card; markdown renders).
        form_elements.append({"tag": "markdown", "content": "\n".join(rows)})
        options = [
            {
                "text": {"tag": "plain_text", "content": _clip(_inline(str(option.get("label", ""))), 60, "...")},
                "value": str(o_index),
            }
            for o_index, option in enumerate(question.get("options", []))
            if isinstance(option, dict)
        ]
        if options:
            form_elements.append(
                {
                    "tag": "multi_select_static" if multi else "select_static",
                    "name": f"q{q_index}",
                    "placeholder": {"tag": "plain_text", "content": "请选择"},
                    "options": options,
                }
            )
        if question.get("other"):
            form_elements.append(
                {
                    "tag": "input",
                    "name": f"q{q_index}_other",
                    "placeholder": {
                        "tag": "plain_text",
                        "content": "✏️ 其他答案（选填，优先于上面的选择）",
                    },
                }
            )
    form_elements.append(
        {
            "tag": "button",
            "action_type": "form_submit",
            "name": "submit_all",
            "text": {"tag": "plain_text", "content": "✅ 提交全部"},
            "type": "primary",
            "value": {"action": str(submit.get("action", "submit_all")), "token": str(submit.get("token", ""))},
        }
    )
    elements: list[dict[str, Any]] = [
        {"tag": "form", "name": "ask_form", "elements": form_elements},
        _note("选择先暂存在本地，点「提交全部」才会一次性提交。"),
    ]
    return _card_message("请选择", "blue", elements)


def _health_card(view: dict[str, Any]) -> dict[str, Any]:
    status = str(view.get("status", "") or "running")
    template = _HEALTH_TEMPLATE.get(status, "blue")
    status_label = _HEALTH_STATUS_LABEL.get(status, status)
    elapsed = int(float(view.get("elapsed", 0.0) or 0.0))
    minutes, seconds = divmod(elapsed, 60)
    duration = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
    session_id = str(view.get("session_id", "") or "")
    # Model ids come from SDK events / config; strip backticks so a hostile
    # value cannot break out of the code span (V2 escape rationale applies).
    model = _inline(str(view.get("model", "") or "")).replace("`", "")
    context_used = int(view.get("context_used", 0) or 0)
    context_limit = int(view.get("context_limit", 0) or 0)
    if context_used and context_limit:
        percent = round(context_used * 100 / context_limit)
        context_label = f"{context_used / 1000:.1f}k / {context_limit // 1000}k（{percent}%）"
    elif context_used:
        context_label = f"{context_used / 1000:.1f}k"
    else:
        context_label = "—"
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**状态**: {status_label}　**Agent**: {view.get('transport', '') or '—'}"},
        {"tag": "markdown", "content": f"**模型**: {f'`{model}`' if model else '—'}　**上下文**: {context_label}"},
        {"tag": "markdown", "content": f"**Session**: `{session_id}`" if session_id else "**Session**: —"},
        {"tag": "markdown", "content": f"**时长**: {duration}　**目录**: {escape_lark_md(str(view.get('cwd', '') or '—'))}"},
    ]
    detail_bits = []
    if view.get("lifecycle_state"):
        detail_bits.append(f"**阶段**: {view['lifecycle_state']}")
    background_tasks = int(view.get("background_tasks", 0) or 0)
    if background_tasks:
        detail_bits.append(f"**后台**: {background_tasks} 个任务进行中")
    if view.get("writer_owner"):
        detail_bits.append(f"**写者**: {view['writer_owner']}")
    if view.get("last_progress_event"):
        detail_bits.append(f"**进展**: {escape_lark_md(_inline(str(view['last_progress_event'])))}")
    if detail_bits:
        elements.append({"tag": "markdown", "content": "　".join(detail_bits)})
    if view.get("direct_write"):
        elements.append(_md_div("🔁 双端同步中：这里发消息会直达终端会话。"))
    elif view.get("readonly"):
        elements.append(_md_div("👀 只读观察中：接管后才能从这里发消息。"))
    reason = str(view.get("reason", "") or "")
    if status in {"error", "stale"} and reason:
        elements.append(_md_div(f"**原因**: {escape_lark_md(_inline(reason))}"))
    buttons = [_button(action) for action in view.get("actions", []) if isinstance(action, dict)]
    if buttons:
        elements.append(_action_row(buttons))
    title = str(view.get("title", "") or "WalkCode 会话")
    return _card_message(title, template, elements)


def _tool_progress_line(entry: dict[str, Any]) -> str:
    status = str(entry.get("status", "") or "running")
    icon = {"running": "⏳", "completed": "✅", "failed": "❌"}.get(status, "⏳")
    row = f"{icon} `{escape_lark_md(str(entry.get('tool_name', '') or 'tool'))}`"
    summary = str(entry.get("summary", "") or "").strip()
    if summary:
        row += f" — {escape_lark_md(_inline(summary))}"
    return row


def _tool_progress_card(view: dict[str, Any]) -> dict[str, Any]:
    # A burst of consecutive tool calls/results is coalesced into one card that
    # is patched in place (interactive cards are patchable via im.message.patch;
    # a post message is not, which is why appending was the only prior option).
    lines = view.get("lines")
    if isinstance(lines, list) and lines:
        entries = [ln for ln in lines if isinstance(ln, dict)]
    else:
        entries = [view]
    statuses = {str(e.get("status", "") or "running") for e in entries}
    if statuses == {"completed"}:
        template = "green"
    elif "failed" in statuses:
        template = "red"
    else:
        template = "grey"
    body = "\n".join(_tool_progress_line(e) for e in entries) or "⏳ `tool`"
    return _card_message("🔧 工具执行", template, [_md_div(body)])


def _takeover_prompt_card(view: dict[str, Any]) -> dict[str, Any]:
    summary = str(view.get("summary", "") or "").strip()
    rows = ["这个会话当前由本机 TUI 持有，从这里发消息需要先接管。"]
    if summary:
        rows.append(f"**待发送**: {escape_lark_md(_inline(summary))}")
    recoverability = str(view.get("recoverability", "") or "")
    if recoverability:
        rows.append(f"**可恢复性**: {recoverability}")
    buttons = [_button(action) for action in view.get("actions", []) if isinstance(action, dict)]
    elements: list[dict[str, Any]] = [_md_div("\n".join(rows))]
    if buttons:
        elements.append(_action_row(buttons))
    return _card_message("🔁 需要接管会话", "orange", elements)


def _takeover_confirmation_card(view: dict[str, Any]) -> dict[str, Any]:
    summary = str(view.get("summary", "") or "").strip()
    rows = ["确认接管会终止本机 TUI 进程，并把会话切到 IM 这边继续。"]
    if summary:
        rows.append(f"**待发送**: {escape_lark_md(_inline(summary))}")
    buttons = [_button(action) for action in view.get("actions", []) if isinstance(action, dict)]
    elements: list[dict[str, Any]] = [_md_div("\n".join(rows))]
    if buttons:
        elements.append(_action_row(buttons))
    return _card_message("⚠️ 确认接管", "orange", elements)


def _takeover_progress_card(view: dict[str, Any]) -> dict[str, Any]:
    phase = str(view.get("phase", "") or "")
    labels = {
        "terminating_external_tui": "正在停止 TUI 进程…",
        "resuming_structured": "正在接管会话…",
        "submitting_blocked_input": "正在发送你的消息…",
        "submitted_blocked_input": "接管完成，消息已发出，等待回复（首个回复可能需要几分钟）。",
        "completed": "接管完成，可以直接在这个话题里发消息了。",
        "failed": "接管失败",
    }
    text = labels.get(phase, "接管进行中…")
    reason = str(view.get("reason", "") or "")
    if phase == "failed" and reason == "external_tui_still_running":
        # Self-respawning terminal agent: kill-and-resume takeover can't win.
        text = (
            "接管失败：终端会话仍在运行（关掉后又自动重启），无法从这里接管。\n"
            "请直接在终端里操作，或先彻底结束终端会话再试。"
        )
    elif reason and phase not in ("completed", "submitted_blocked_input"):
        text = f"{text}\n**原因**: {escape_lark_md(_inline(reason))}"
    template = (
        "green"
        if phase in ("completed", "submitted_blocked_input")
        else ("red" if phase == "failed" else "grey")
    )
    return _card_message("🔁 接管进度", template, [_md_div(text)])


def _manual_only_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = [f"**原因**: {escape_lark_md(_inline(str(view.get('reason', '') or '')))}"]
    steps = view.get("suggested_steps", [])
    if isinstance(steps, list) and steps:
        rows.append("**手动步骤**:")
        rows.extend(f"{idx}. {escape_lark_md(_inline(str(step)))}" for idx, step in enumerate(steps, 1))
    return _card_message("🛑 无法自动接管", "red", [_md_div("\n".join(rows))])


def _hitl_stale_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = [
        "接管前的这个人工确认请求已失效，不能再回答。",
        f"**类型**: {escape_lark_md(str(view.get('prompt_kind', '') or '—'))}",
        f"**原因**: {escape_lark_md(_inline(str(view.get('reason', '') or '—')))}",
    ]
    return _card_message("⌛ 请求已失效", "grey", [_md_div("\n".join(rows))])


_DECISION_LABELS = {
    "allow": ("✅ 已允许", "green"),
    "allow_once": ("✅ 已允许（本次）", "green"),
    "always_allow": ("✅ 已始终允许", "green"),
    "accept": ("✅ 已接受", "green"),
    "acceptForSession": ("✅ 本会话内接受", "green"),
    "deny": ("🚫 已拒绝", "grey"),
    "decline": ("🚫 已拒绝", "grey"),
    "cancel": ("🚫 已取消", "grey"),
}


def _tui_permission_notice_card(view: dict[str, Any]) -> dict[str, Any]:
    tool = str(view.get("tool_name", "") or "工具")
    summary = str(view.get("summary", "") or "")
    rows = [f"终端里的会话正在等你确认一个操作：**`{escape_lark_md(_inline(tool))}`**"]
    if summary:
        rows.append(escape_lark_md(_clip(_inline(summary), 300, "...")))
    return _card_message(
        "⏳ 终端在等你确认",
        "orange",
        [
            _md_div("\n".join(rows)),
            # Post-gate this card only appears for confirmations that did NOT
            # route to a Feishu approval card (tool outside the gate set, gate
            # off/ask_only, or the gate abstained) — so the terminal is the
            # only place to answer. No takeover pitch: daemon-native sessions
            # are already dual-writable.
            _note("这个确认未走飞书审批通道，需要在终端里处理。"),
        ],
    )


def _decision_result_card(view: dict[str, Any]) -> dict[str, Any]:
    action = str(view.get("action", "") or "")
    detail = str(view.get("detail", "") or "")
    if action == "stale":
        # Decision was recorded but could not reach the worker (e.g. the
        # runtime restarted and the in-flight prompt died with it).
        body = escape_lark_md(_inline(detail)) if detail else "会话进程已重启，这张卡片已失效。"
        return _card_message("⚠️ 卡片已失效", "orange", [_md_div(body)])
    if action == "degraded":
        # v3 keystroke injection missed; the native dialog is still waiting.
        body = escape_lark_md(_inline(detail)) if detail else "注入未生效，请在终端操作。"
        return _card_message("⚠️ 请在终端操作", "orange", [_md_div(body)])
    if action == "terminal":
        body = escape_lark_md(_inline(detail)) if detail else "已在终端处理。"
        return _card_message("✅ 已在终端处理", "green", [_md_div(body)])
    if str(view.get("kind", "")) == "model_choice":
        body = f"✅ {escape_lark_md(_inline(detail))}" if detail else "✅ 模型已切换"
        return _card_message("🧠 模型已切换", "green", [_md_div(body)])
    if str(view.get("kind", "")) == "ask_user_question" or action == "answers":
        body = f"✅ {escape_lark_md(_inline(detail))}" if detail else "✅ 已回答"
        return _card_message("✅ 已回答", "green", [_md_div(body)])
    label, template = _DECISION_LABELS.get(action, (f"已处理：{action}" if action else "已处理", "grey"))
    tool = str(view.get("tool_name", "") or "")
    body = f"**Tool:** `{escape_lark_md(tool)}`" if tool else label
    return _card_message(label, template, [_md_div(body)])


def _error_card(view: dict[str, Any]) -> dict[str, Any]:
    code = str(view.get("code", "") or "error")
    message = escape_lark_md(str(view.get("message", "") or ""))
    body = f"`{escape_lark_md(code)}`\n{message}" if message else f"`{escape_lark_md(code)}`"
    if view.get("retryable"):
        body += "\n可以稍后重试。"
    return _card_message("🔴 出错了", "red", [_md_div(body)])


def _session_chooser_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = ["这个会话里有多个进行中的任务，请在目标任务的话题里回复，或在根会话发新任务。"]
    for item in view.get("sessions", [])[:8]:
        if not isinstance(item, dict):
            continue
        title = escape_lark_md(_inline(str(item.get("title") or item.get("session_id") or "session")))
        transport = str(item.get("transport_kind", "") or "agent")
        lifecycle = str(item.get("lifecycle_state", "") or item.get("status", ""))
        rows.append(f"- **{title}** ({transport} {lifecycle})")
    return _card_message("🧭 选择会话", "blue", [_md_div("\n".join(rows))])


def _model_choice_card(view: dict[str, Any]) -> dict[str, Any]:
    current = str(view.get("current", "") or "")
    buttons = [
        _button(action, btn_type="primary" if str(action.get("action", "")) == current else "default")
        for action in view.get("actions", [])
        if isinstance(action, dict) and action.get("token")
    ]
    elements: list[dict[str, Any]] = [_md_div("选择要切换到的模型：")]
    if buttons:
        elements.append(_action_row(buttons))
    else:
        elements = [_md_div("当前没有可切换的模型。")]
    return _card_message("🧠 切换模型", "blue", elements)


def _command_menu_card(view: dict[str, Any]) -> dict[str, Any]:
    buttons = [
        _button(action)
        for action in view.get("actions", [])
        if isinstance(action, dict) and action.get("token")
    ]
    if buttons:
        return _card_message("⌨️ 会话操作", "blue", [_action_row(buttons)])
    rows = [
        f"- `{escape_lark_md(str(action.get('action', '')))}` {escape_lark_md(_inline(str(action.get('label', ''))))}"
        for action in view.get("actions", [])
        if isinstance(action, dict)
    ]
    return _card_message("⌨️ 会话操作", "blue", [_md_div("\n".join(rows) or "—")])


_CARD_RENDERERS = {
    "permission_prompt": _permission_card,
    "ask_user_question": _ask_user_question_card,
    "health": _health_card,
    "takeover_prompt": _takeover_prompt_card,
    "takeover_confirmation": _takeover_confirmation_card,
    "takeover_progress": _takeover_progress_card,
    "manual_only": _manual_only_card,
    "hitl_stale": _hitl_stale_card,
    "error": _error_card,
    "session_chooser": _session_chooser_card,
    "command_menu": _command_menu_card,
    "model_choice": _model_choice_card,
    "decision_result": _decision_result_card,
    "tui_permission_notice": _tui_permission_notice_card,
}


def render_lark_message(view: dict[str, Any], *, fallback_text: str = "") -> dict[str, Any]:
    """Render one V3 view model into ``{"msg_type", "content"}``.

    ``fallback_text`` is the pre-rendered ``render_view_text`` output the
    adapter already puts in the payload; it backs every view type that has no
    dedicated card layout, so new view types degrade to readable text instead
    of failing delivery.
    """
    if isinstance(view, dict):
        renderer = _CARD_RENDERERS.get(str(view.get("type", "")))
        if renderer is not None:
            return renderer(view)
        if view.get("type") == "tool_progress":
            return _tool_progress_card(view)
        for key in ("text", "message"):
            if key in view:
                return _post_message(str(view[key]))
    return _post_message(fallback_text or str(view))
