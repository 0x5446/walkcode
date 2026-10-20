"""Pure renderers from V3 view models to Slack message payloads (ADR 0070).

The Slack analogue of ``lark_cards``: turns the plain view dicts produced by
``ViewModelFactory`` / ``Orchestrator._event_to_view`` into
``{"text": ..., "blocks": [...]}`` for ``chat.postMessage`` / ``chat.update``.
No IO, no SDK import.

Agent prose goes into a ``markdown`` block (standard Markdown, tables
included); everything WalkCode itself lays out is Block Kit with ``mrkdwn``.
Untrusted text placed into mrkdwn is escaped (``& < >``) so it cannot form
links or @-mentions; agent prose has broadcast mentions defused.

Limits that shaped this module (docs.slack.dev, 2026-10):
- markdown blocks: 12,000 chars summed per message;
- section text: 3,000 chars; header: 150; button text: 75; button value: 2,000;
- 50 blocks per message; ``chat.update`` rejects ``text`` over 4,000.
"""

from __future__ import annotations

import json
import re
from typing import Any

MARKDOWN_MESSAGE_LIMIT = 12000
SECTION_TEXT_LIMIT = 3000
FALLBACK_TEXT_LIMIT = 3000
HEADER_LIMIT = 150
BUTTON_TEXT_LIMIT = 75
PLAN_BODY_LIMIT = 800
TOOL_INPUT_LIMIT = 500
TOOL_PROGRESS_MAX_LINES = 30

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
# Buttons whose click is hard to take back get Slack's native confirm dialog.
_CONFIRM_ACTIONS = {"always_allow", "acceptForSession", "confirm_takeover", "takeover_and_send"}

_HEALTH_STATUS_LABEL = {
    "running": "🟢 运行中",
    "idle": "🟦 空闲",
    "waiting_permission": "🟠 等待权限确认",
    "waiting_user": "🟠 等待你的回答",
    "stale": "🟠 长时间无进展",
    "stopped": "⚪ 已结束",
    "error": "🔴 出错",
}

_BROADCAST_RE = re.compile(r"<!(channel|here|everyone)(\|[^>]*)?>", re.IGNORECASE)
_USER_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(\|[^>]*)?>")


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as control in mrkdwn."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def defuse_mentions(text: str) -> str:
    """Neutralize broadcast and user mentions in agent prose.

    Agent output is relayed verbatim; a prompt-injected ``<!channel>`` would
    otherwise ping a whole channel in the bot's name.
    """
    text = _BROADCAST_RE.sub(lambda m: f"@{m.group(1).lower()}", str(text or ""))
    return _USER_MENTION_RE.sub(lambda m: f"@{m.group(1)}", text)


def _clip(text: str, limit: int, marker: str = "…") -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(marker))] + marker


def _inline(text: str) -> str:
    return str(text or "").replace("\r", " ").replace("\n", " ")


def _code(text: str) -> str:
    # A backtick inside would close the span early.
    return f"`{escape_mrkdwn(_inline(text).replace('`', 'ˋ'))}`"


def split_markdown(text: str, limit: int = MARKDOWN_MESSAGE_LIMIT) -> list[str]:
    """Split long Markdown into chunks of at most ``limit`` chars.

    Cuts at paragraph, then line, boundaries. A cut inside a fenced code block
    closes the fence at the end of the chunk and reopens it (same info string)
    at the start of the next, so every chunk renders on its own.
    """
    text = str(text or "")
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    fence = ""  # the opening fence line carried into the current chunk
    current = ""
    reserve = 8  # room for a closing "\n```"

    def flush() -> None:
        nonlocal current
        body = current
        if fence:
            body = body.rstrip("\n") + "\n```"
        chunks.append(body)
        current = f"{fence}\n" if fence else ""

    for line in text.splitlines(keepends=True):
        while len(line) > limit - reserve - len(fence) - 1:
            # One line longer than a chunk: hard-cut it.
            room = limit - reserve - len(current)
            if room <= 0:
                flush()
                continue
            current += line[:room]
            line = line[room:]
            flush()
        if len(current) + len(line) > limit - reserve:
            flush()
        current += line
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = "" if fence else stripped
    if current.strip() or not chunks:
        chunks.append(current)
    return chunks


def _text_message(text: str) -> dict[str, Any]:
    body = defuse_mentions(text)
    return {
        "text": _clip(body, FALLBACK_TEXT_LIMIT),
        "blocks": [{"type": "markdown", "text": _clip(body, MARKDOWN_MESSAGE_LIMIT)}],
    }


def text_messages(text: str) -> list[dict[str, Any]]:
    """One or more messages carrying ``text`` as Markdown, in order."""
    return [_text_message(chunk) for chunk in split_markdown(defuse_mentions(text))]


def _header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": _clip(_inline(text) or " ", HEADER_LIMIT), "emoji": True}}


def _section(mrkdwn: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clip(mrkdwn, SECTION_TEXT_LIMIT) or " "}}


def _sections(lines: list[str]) -> list[dict[str, Any]]:
    """Pack lines into as few sections as the 3,000-char limit allows."""
    blocks: list[dict[str, Any]] = []
    current = ""
    for line in lines:
        line = _clip(line, SECTION_TEXT_LIMIT)
        if current and len(current) + 1 + len(line) > SECTION_TEXT_LIMIT:
            blocks.append(_section(current))
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        blocks.append(_section(current))
    return blocks


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clip(text, SECTION_TEXT_LIMIT)}]}


def _button(action: dict[str, Any], *, style: str | None = None, label: str | None = None, index: int = 0) -> dict[str, Any]:
    name = str(action.get("action", ""))
    value: dict[str, Any] = {"action": name}
    token = str(action.get("token", "") or "")
    if token:
        value["token"] = token
    button: dict[str, Any] = {
        "type": "button",
        "action_id": f"walkcode:{index}:{name}"[:255],
        "text": {"type": "plain_text", "text": _clip(_inline(label or str(action.get("label", name))), BUTTON_TEXT_LIMIT), "emoji": True},
        "value": json.dumps(value, ensure_ascii=False)[:2000],
    }
    if style is None:
        if name in _BUTTON_PRIMARY_ACTIONS:
            style = "primary"
        elif name in _BUTTON_DANGER_ACTIONS:
            style = "danger"
    if style in {"primary", "danger"}:
        button["style"] = style
    if name in _CONFIRM_ACTIONS:
        button["confirm"] = {
            "title": {"type": "plain_text", "text": "确认"},
            "text": {"type": "plain_text", "text": _clip(f"确定要「{_inline(label or str(action.get('label', name)))}」吗？", 300)},
            "confirm": {"type": "plain_text", "text": "确定"},
            "deny": {"type": "plain_text", "text": "再想想"},
        }
    return button


def _actions(actions: list[Any], **kwargs: Any) -> list[dict[str, Any]]:
    buttons = [
        _button(action, index=index, **kwargs)
        for index, action in enumerate(actions)
        if isinstance(action, dict)
    ]
    # Slack allows 25 elements per actions block; split to stay valid.
    return [{"type": "actions", "elements": buttons[i : i + 25]} for i in range(0, len(buttons), 25)]


def _card(title: str, blocks: list[dict[str, Any]], *, fallback: str = "") -> dict[str, Any]:
    return {
        "text": _clip(_inline(fallback or title), FALLBACK_TEXT_LIMIT),
        "blocks": [_header(title), *blocks][:50],
    }


def _permission_card(view: dict[str, Any]) -> dict[str, Any]:
    tool_name = str(view.get("tool_name", "") or "")
    tool_input = view.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {"value": tool_input}
    plan = str(tool_input.get("plan", "") or "")
    if plan and tool_name in {"ExitPlanMode", "exit_plan_mode"}:
        title = "📋 计划待确认"
        body = [_section(escape_mrkdwn(_clip(plan, PLAN_BODY_LIMIT)))]
    else:
        title = "🔐 权限请求（高风险）" if view.get("high_risk") else "🔐 权限请求"
        input_str = _clip(json.dumps(tool_input, indent=2, ensure_ascii=False), TOOL_INPUT_LIMIT)
        body = [
            _section(f"*Tool:* {_code(tool_name)}"),
            _section(f"```{escape_mrkdwn(input_str.replace('```', 'ˋˋˋ'))}```"),
        ]
    return _card(title, [*body, *_actions(view.get("actions", []))], fallback=f"{title}: {tool_name}")


def _ask_user_question_card(view: dict[str, Any]) -> dict[str, Any]:
    questions = view.get("questions")
    if not isinstance(questions, list):
        return _card("请选择", [_section("⚠️ 无可用问题。")])
    submit = view.get("submit")
    if isinstance(submit, dict) and submit.get("token"):
        return _ask_user_form_card(questions, submit)
    blocks: list[dict[str, Any]] = []
    for q_index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        title = str(question.get("header") or question.get("prompt") or f"问题 {q_index + 1}")
        blocks.append(_section(f"*{escape_mrkdwn(_inline(title))}*"))
        blocks.extend(_actions(question.get("options", [])))
    return _card("请选择", blocks)


def _option(label: str, value: str) -> dict[str, Any]:
    return {"text": {"type": "plain_text", "text": _clip(_inline(label), 75) or " ", "emoji": True}, "value": value}


def _ask_user_form_card(questions: list[Any], submit: dict[str, Any]) -> dict[str, Any]:
    # Batch mode: input blocks stay client-side; one submit button delivers
    # every field through block_actions state.values. block_ids q{i} /
    # q{i}_other mirror the Lark form field names, so the adapter folds them
    # into the same callback["form"] shape.
    blocks: list[dict[str, Any]] = []
    for q_index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        header = str(question.get("header") or "")
        prompt = str(question.get("prompt") or "")
        title = header or prompt or f"问题 {q_index + 1}"
        multi = bool(question.get("allow_multiple"))
        options = [
            _option(str(option.get("label", "")), str(o_index))
            for o_index, option in enumerate(question.get("options", []))
            if isinstance(option, dict)
        ][:100]
        hint = escape_mrkdwn(_inline(prompt)) if header and prompt and prompt != header else ""
        if options:
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"q{q_index}",
                    "optional": True,
                    "label": {"type": "plain_text", "text": _clip(f"{_inline(title)}（{'多选' if multi else '单选'}）", 2000)},
                    "element": {
                        "type": "multi_static_select" if multi else "static_select",
                        "action_id": "value",
                        "placeholder": {"type": "plain_text", "text": "请选择"},
                        "options": options,
                    },
                    **({"hint": {"type": "plain_text", "text": _clip(_inline(prompt), 2000)}} if hint else {}),
                }
            )
        if question.get("other") or not options:
            blocks.append(
                {
                    "type": "input",
                    "block_id": f"q{q_index}_other",
                    "optional": True,
                    "label": {"type": "plain_text", "text": _clip("✏️ 其他答案（选填，优先于上面的选择）" if options else _inline(title), 2000)},
                    "element": {"type": "plain_text_input", "action_id": "value", "multiline": True},
                }
            )
    blocks.extend(
        _actions(
            [{"action": str(submit.get("action", "submit_all")), "token": str(submit.get("token", "")), "label": "✅ 提交全部"}],
            style="primary",
        )
    )
    blocks.append(_context("选择先暂存在本地，点「提交全部」才会一次性提交。"))
    return _card("请选择", blocks)


def _session_ids_line(agent_session_id: str, session_id: str) -> str:
    bits = []
    if agent_session_id:
        bits.append(f"*Session*: {_code(agent_session_id)}")
    if session_id:
        bits.append(f"*WalkCode*: {_code(session_id)}")
    return "　".join(bits) if bits else "*Session*: —"


def _health_card(view: dict[str, Any]) -> dict[str, Any]:
    status = str(view.get("status", "") or "running")
    status_label = _HEALTH_STATUS_LABEL.get(status, status)
    elapsed = int(float(view.get("elapsed", 0.0) or 0.0))
    minutes, seconds = divmod(elapsed, 60)
    duration = f"{minutes}分{seconds:02d}秒" if minutes else f"{seconds}秒"
    model = str(view.get("model", "") or "")
    context_used = int(view.get("context_used", 0) or 0)
    context_limit = int(view.get("context_limit", 0) or 0)
    if context_used and context_limit:
        percent = round(context_used * 100 / context_limit)
        context_label = f"{context_used / 1000:.1f}k / {context_limit // 1000}k（{percent}%）"
    elif context_used:
        context_label = f"{context_used / 1000:.1f}k"
    else:
        context_label = "—"
    lines = [
        f"*状态*: {status_label}　*Agent*: {escape_mrkdwn(str(view.get('transport', '') or '—'))}",
        f"*模型*: {_code(model) if model else '—'}　*上下文*: {context_label}",
        _session_ids_line(str(view.get("agent_session_id", "") or ""), str(view.get("session_id", "") or "")),
        f"*时长*: {duration}　*目录*: {_code(str(view.get('cwd', '') or '—'))}",
    ]
    blocks: list[dict[str, Any]] = [_section("\n".join(lines))]
    detail_bits = []
    if view.get("lifecycle_state"):
        detail_bits.append(f"*阶段*: {escape_mrkdwn(str(view['lifecycle_state']))}")
    background_tasks = int(view.get("background_tasks", 0) or 0)
    if background_tasks:
        detail_bits.append(f"*后台*: {background_tasks} 个任务进行中")
    if view.get("writer_owner"):
        detail_bits.append(f"*写者*: {escape_mrkdwn(str(view['writer_owner']))}")
    if view.get("last_progress_event"):
        detail_bits.append(f"*进展*: {escape_mrkdwn(_inline(str(view['last_progress_event'])))}")
    if detail_bits:
        blocks.append(_context("　".join(detail_bits)))
    if view.get("readonly"):
        blocks.append(_section("👀 只读观察中：接管后才能从这里发消息。"))
    reason = str(view.get("reason", "") or "")
    if status in {"error", "stale"} and reason:
        blocks.append(_section(f"*原因*: {escape_mrkdwn(_inline(reason))}"))
    blocks.extend(_actions(view.get("actions", [])))
    title = str(view.get("title", "") or "WalkCode 会话")
    return _card(title, blocks, fallback=f"{title} · {status_label}")


def _tool_progress_line(entry: dict[str, Any]) -> str:
    if str(entry.get("kind", "") or "") == "narration":
        text = _inline(str(entry.get("text", "") or "").strip())
        return f"💬 {escape_mrkdwn(_clip(text, 300))}"
    status = str(entry.get("status", "") or "running")
    icon = {"running": "⏳", "completed": "✅", "failed": "❌"}.get(status, "⏳")
    row = f"{icon} {_code(str(entry.get('tool_name', '') or 'tool'))}"
    summary = str(entry.get("summary", "") or "").strip()
    if summary:
        row += f" — {escape_mrkdwn(_clip(_inline(summary), 300))}"
    return row


def _tool_progress_card(view: dict[str, Any]) -> dict[str, Any]:
    lines = view.get("lines")
    entries = [ln for ln in lines if isinstance(ln, dict)] if isinstance(lines, list) and lines else [view]
    tool_entries = [e for e in entries if str(e.get("kind", "") or "") != "narration"]
    statuses = {str(e.get("status", "") or "running") for e in tool_entries}
    if statuses == {"completed"}:
        title = "🔧 工具执行 ✅"
    elif "failed" in statuses:
        title = "🔧 工具执行 ❌"
    else:
        title = "🔧 工具执行"
    body_lines: list[str] = []
    if len(entries) > TOOL_PROGRESS_MAX_LINES:
        folded = len(entries) - TOOL_PROGRESS_MAX_LINES
        entries = entries[-TOOL_PROGRESS_MAX_LINES:]
        body_lines.append(f"…（已折叠前 {folded} 行）")
    body_lines.extend(_tool_progress_line(e) for e in entries)
    return _card(title, _sections(body_lines or ["⏳ `tool`"]), fallback=title)


def _takeover_prompt_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = ["这个会话当前由本机 TUI 持有，从这里发消息需要先接管。"]
    summary = str(view.get("summary", "") or "").strip()
    if summary:
        rows.append(f"*待发送*: {escape_mrkdwn(_inline(summary))}")
    recoverability = str(view.get("recoverability", "") or "")
    if recoverability:
        rows.append(f"*可恢复性*: {escape_mrkdwn(recoverability)}")
    return _card("🔁 需要接管会话", [_section("\n".join(rows)), *_actions(view.get("actions", []))])


def _takeover_confirmation_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = ["确认接管会终止本机 TUI 进程，并把会话切到 Slack 这边继续。"]
    summary = str(view.get("summary", "") or "").strip()
    if summary:
        rows.append(f"*待发送*: {escape_mrkdwn(_inline(summary))}")
    return _card("⚠️ 确认接管", [_section("\n".join(rows)), *_actions(view.get("actions", []))])


_TAKEOVER_PHASE_LABELS = {
    "terminating_external_tui": "正在停止 TUI 进程…",
    "resuming_structured": "正在接管会话…",
    "submitting_blocked_input": "正在发送你的消息…",
    "submitted_blocked_input": "接管完成，消息已发出，等待回复（首个回复可能需要几分钟）。",
    "completed": "接管完成，可以直接在这个 thread 里发消息了。",
    "failed": "接管失败",
}


def _takeover_progress_card(view: dict[str, Any]) -> dict[str, Any]:
    phase = str(view.get("phase", "") or "")
    text = _TAKEOVER_PHASE_LABELS.get(phase, "接管进行中…")
    reason = str(view.get("reason", "") or "")
    if reason and phase not in ("completed", "submitted_blocked_input"):
        text = f"{text}\n*原因*: {escape_mrkdwn(_inline(reason))}"
    return _card("🔁 接管进度", [_section(text)])


def _manual_only_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = [f"*原因*: {escape_mrkdwn(_inline(str(view.get('reason', '') or '')))}"]
    steps = view.get("suggested_steps", [])
    if isinstance(steps, list) and steps:
        rows.append("*手动步骤*:")
        rows.extend(f"{idx}. {escape_mrkdwn(_inline(str(step)))}" for idx, step in enumerate(steps, 1))
    return _card("🛑 无法自动接管", [_section("\n".join(rows))])


def _hitl_stale_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = [
        "接管前的这个人工确认请求已失效，不能再回答。",
        f"*类型*: {escape_mrkdwn(str(view.get('prompt_kind', '') or '—'))}",
        f"*原因*: {escape_mrkdwn(_inline(str(view.get('reason', '') or '—')))}",
    ]
    return _card("⌛ 请求已失效", [_section("\n".join(rows))])


_DECISION_LABELS = {
    "allow": "✅ 已允许",
    "allow_once": "✅ 已允许（本次）",
    "always_allow": "✅ 已始终允许",
    "accept": "✅ 已接受",
    "acceptForSession": "✅ 本会话内接受",
    "deny": "🚫 已拒绝",
    "decline": "🚫 已拒绝",
    "cancel": "🚫 已取消",
}


def _tui_conflict_notice_card(view: dict[str, Any]) -> dict[str, Any]:
    kind = str(view.get("kind", "") or "")
    pid = int(view.get("pid", 0) or 0)
    detail = str(view.get("detail", "") or "")
    extra = [_context(escape_mrkdwn(_clip(_inline(detail), 200)))] if detail else []
    if kind == "handback":
        return _card(
            "🖥️ 终端已接回会话",
            [
                _section("终端 TUI 已重新接管这个会话，Slack 转为*只读镜像*。"),
                _context("需要继续用 Slack 驱动，请对新消息重新接管。"),
                *extra,
            ],
        )
    if kind == "remnant_terminated":
        return _card(
            "🧹 已清理终端进程",
            [_section(f"该会话由频道驱动，检测到终端进程 (pid {pid}) 双写，已终止以避免冲突。"), *extra],
        )
    return _card(
        "⚠️ 检测到终端进程双写",
        [
            _section(f"终端进程 (pid {pid}) 与频道同时挂在该会话上，但未能自动终止。"),
            _context("请在终端手动退出它，避免双写。"),
            *extra,
        ],
    )


def _tui_permission_notice_card(view: dict[str, Any]) -> dict[str, Any]:
    tool = str(view.get("tool_name", "") or "工具")
    summary = str(view.get("summary", "") or "")
    rows = [f"终端里的会话正在等你确认一个操作：*{_code(tool)}*"]
    if summary:
        rows.append(escape_mrkdwn(_clip(_inline(summary), 300)))
    return _card(
        "⏳ 终端在等你确认",
        [_section("\n".join(rows)), _context("这个确认未走 Slack 审批通道，需要在终端里处理。")],
    )


def _decision_result_card(view: dict[str, Any]) -> dict[str, Any]:
    action = str(view.get("action", "") or "")
    detail = str(view.get("detail", "") or "")
    if action == "stale":
        body = escape_mrkdwn(_inline(detail)) if detail else "会话进程已重启，这张卡片已失效。"
        return _card("⚠️ 卡片已失效", [_section(body)])
    if action == "terminal":
        body = escape_mrkdwn(_inline(detail)) if detail else "已在终端处理。"
        return _card("✅ 已在终端处理", [_section(body)])
    if str(view.get("kind", "")) == "model_choice":
        body = f"✅ {escape_mrkdwn(_inline(detail))}" if detail else "✅ 模型已切换"
        return _card("🧠 模型已切换", [_section(body)])
    if str(view.get("kind", "")) == "ask_user_question" or action == "answers":
        body = f"✅ {escape_mrkdwn(_inline(detail))}" if detail else "✅ 已回答"
        return _card("✅ 已回答", [_section(body)])
    label = _DECISION_LABELS.get(action, f"已处理：{action}" if action else "已处理")
    tool = str(view.get("tool_name", "") or "")
    body = f"*Tool:* {_code(tool)}" if tool else escape_mrkdwn(label)
    decided_by = str(view.get("decided_by", "") or "")
    blocks = [_section(body)]
    if decided_by:
        blocks.append(_context(f"由 <@{escape_mrkdwn(decided_by)}> 处理"))
    return _card(label, blocks)


def _error_card(view: dict[str, Any]) -> dict[str, Any]:
    code = str(view.get("code", "") or "error")
    message = escape_mrkdwn(str(view.get("message", "") or ""))
    body = f"{_code(code)}\n{message}" if message else _code(code)
    if view.get("retryable"):
        body += "\n可以稍后重试。"
    return _card("🔴 出错了", [_section(body)])


def _session_chooser_card(view: dict[str, Any]) -> dict[str, Any]:
    rows = ["这里有多个进行中的任务，请在目标任务的 thread 里回复，或在频道里 @ 我开新任务。"]
    for item in view.get("sessions", [])[:8]:
        if not isinstance(item, dict):
            continue
        title = escape_mrkdwn(_inline(str(item.get("title") or item.get("session_id") or "session")))
        transport = escape_mrkdwn(str(item.get("transport_kind", "") or "agent"))
        lifecycle = escape_mrkdwn(str(item.get("lifecycle_state", "") or item.get("status", "")))
        rows.append(f"• *{title}* ({transport} {lifecycle})")
    return _card("🧭 选择会话", [_section("\n".join(rows))])


def _model_choice_card(view: dict[str, Any]) -> dict[str, Any]:
    current = str(view.get("current", "") or "")
    actions = [a for a in view.get("actions", []) if isinstance(a, dict) and a.get("token")]
    if not actions:
        return _card("🧠 切换模型", [_section("当前没有可切换的模型。")])
    buttons = [
        _button(action, style="primary" if str(action.get("action", "")) == current else "", index=i)
        for i, action in enumerate(actions)
    ]
    rows = [{"type": "actions", "elements": buttons[i : i + 25]} for i in range(0, len(buttons), 25)]
    return _card("🧠 切换模型", [_section("选择要切换到的模型："), *rows])


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
    "model_choice": _model_choice_card,
    "decision_result": _decision_result_card,
    "tui_conflict_notice": _tui_conflict_notice_card,
    "tui_permission_notice": _tui_permission_notice_card,
    "tool_progress": _tool_progress_card,
}


def render_slack_messages(view: dict[str, Any], *, fallback_text: str = "") -> list[dict[str, Any]]:
    """Render one V3 view model into one or more ``{"text", "blocks"}`` messages.

    Cards are always one message. Prose (``text`` / ``message`` views and
    anything without a card layout) may span several messages when it exceeds
    Slack's per-message Markdown budget — never truncated.
    """
    if isinstance(view, dict):
        renderer = _CARD_RENDERERS.get(str(view.get("type", "")))
        if renderer is not None:
            return [renderer(view)]
        for key in ("text", "message"):
            if key in view:
                return text_messages(str(view[key]))
    return text_messages(fallback_text or str(view))


def is_card(view: dict[str, Any]) -> bool:
    return isinstance(view, dict) and str(view.get("type", "")) in _CARD_RENDERERS
