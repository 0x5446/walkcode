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
            "config": {"wide_screen_mode": True},
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
    return _card_message(title, template, elements)


def _ask_user_question_card(view: dict[str, Any]) -> dict[str, Any]:
    prompt = str(view.get("prompt", "") or "选择一个选项")
    option_buttons: list[dict[str, Any]] = []
    control_buttons: list[dict[str, Any]] = []
    for action in view.get("actions", []):
        if not isinstance(action, dict):
            continue
        name = str(action.get("action", ""))
        label = str(action.get("label", ""))
        if name.startswith("toggle:"):
            selected = label.startswith("[x] ")
            display = f"✓ {label[4:]}" if selected else label
            option_buttons.append(
                _button(action, btn_type="primary" if selected else "default", label=display)
            )
        elif name.startswith("answer:"):
            option_buttons.append(_button(action, btn_type="primary"))
        elif name.startswith("submit:"):
            control_buttons.append(_button(action, btn_type="primary", label=f"✅ {label}"))
        elif name.startswith("other:"):
            control_buttons.append(_button(action, btn_type="default", label="✏️ 其他（自定义文本）"))
        else:
            control_buttons.append(_button(action))

    elements: list[dict[str, Any]] = []
    if option_buttons:
        elements.append(_action_row(option_buttons))
    if control_buttons:
        if option_buttons:
            elements.append({"tag": "hr"})
        elements.append(_action_row(control_buttons))
    if not elements:
        elements.append(_md_div("⚠️ 该问题没有可选项，请在话题里直接回复文本。"))
    return _card_message(prompt, "blue", elements)


def _health_card(view: dict[str, Any]) -> dict[str, Any]:
    status = str(view.get("status", "") or "running")
    template = _HEALTH_TEMPLATE.get(status, "blue")
    status_label = _HEALTH_STATUS_LABEL.get(status, status)
    elapsed = int(float(view.get("elapsed", 0.0) or 0.0))
    minutes, seconds = divmod(elapsed, 60)
    duration = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
    session_id = str(view.get("session_id", "") or "")
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**状态**: {status_label}　**Agent**: {view.get('transport', '') or '—'}"},
        {"tag": "markdown", "content": f"**Session**: `{session_id}`" if session_id else "**Session**: —"},
        {"tag": "markdown", "content": f"**时长**: {duration}　**目录**: {escape_lark_md(str(view.get('cwd', '') or '—'))}"},
    ]
    detail_bits = []
    if view.get("lifecycle_state"):
        detail_bits.append(f"**阶段**: {view['lifecycle_state']}")
    if view.get("writer_owner"):
        detail_bits.append(f"**写者**: {view['writer_owner']}")
    if view.get("last_progress_event"):
        detail_bits.append(f"**进展**: {escape_lark_md(_inline(str(view['last_progress_event'])))}")
    if detail_bits:
        elements.append({"tag": "markdown", "content": "　".join(detail_bits)})
    if view.get("readonly"):
        elements.append(_md_div("👀 只读观察中：接管后才能从这里发消息。"))
    reason = str(view.get("reason", "") or "")
    if status in {"error", "stale"} and reason:
        elements.append(_md_div(f"**原因**: {escape_lark_md(_inline(reason))}"))
    buttons = [_button(action) for action in view.get("actions", []) if isinstance(action, dict)]
    if buttons:
        elements.append(_action_row(buttons))
    title = str(view.get("title", "") or "WalkCode 会话")
    return _card_message(title, template, elements)


def _tool_progress_card(view: dict[str, Any]) -> dict[str, Any]:
    status = str(view.get("status", "") or "running")
    icon = {"running": "⏳", "completed": "✅", "failed": "❌"}.get(status, "⏳")
    rows = [f"{icon} `{escape_lark_md(str(view.get('tool_name', '') or 'tool'))}`"]
    summary = str(view.get("summary", "") or "").strip()
    if summary:
        rows.append(escape_lark_md(_inline(summary)))
    return _post_message("\n".join(rows))


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
        "completed": "接管完成，可以直接在这个话题里发消息了。",
        "failed": "接管失败",
    }
    text = labels.get(phase, "接管进行中…")
    reason = str(view.get("reason", "") or "")
    if reason and phase != "completed":
        text = f"{text}\n**原因**: {escape_lark_md(_inline(reason))}"
    template = "green" if phase == "completed" else ("red" if phase == "failed" else "grey")
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
