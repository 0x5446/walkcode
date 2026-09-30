"""Channel-neutral view models and their plain-text rendering."""

from __future__ import annotations

import re

from typing import Any

from .models import SessionSummary
from .stores import HitlRequest, InteractionContext, InteractionStore


def _model_slug_matches(slug: str, current: str) -> bool:
    """True when a picker slug refers to the session's live model id.

    Assistant events report dated ids (claude-opus-4-8-20260610) or Vertex
    ids (claude-opus-4-8@20260610) while picker slugs are short aliases.
    """
    if not slug or not current:
        return False
    if current == slug:
        return True
    if not current.startswith(slug):
        return False
    # Only a release date (-20260610), a Vertex version (@20260610) or a
    # context window ([1m]) may follow — never another model number, or
    # claude-opus-4 would claim claude-opus-4-8.
    return re.fullmatch(r"(?:-\d{8}|@[^\[]+)?(?:\[[^\]]*\])?", current[len(slug):]) is not None


class ViewModelFactory:
    _ACTION_LABELS = {
        "allow": "Allow",
        "allow_once": "Allow once",
        "deny": "Deny",
        "always_allow": "Always allow",
        "accept": "Accept",
        "acceptForSession": "Accept for session",
        "decline": "Decline",
        "cancel": "Cancel",
        "accept_edits": "Accept edits",
        "plan_auto_accept": "Plan auto-accept",
        "plan_manual_approve": "Plan manual approve",
    }

    def __init__(self, interactions: InteractionStore):
        self.interactions = interactions

    def permission_prompt(self, ctx: InteractionContext) -> dict[str, Any]:
        return {
            "type": "permission_prompt",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "tool_name": ctx.tool_name,
            "tool_input": dict(ctx.tool_input),
            "high_risk": ctx.high_risk,
            "actions": [
                {
                    "action": action,
                    "label": self._ACTION_LABELS.get(action, action.replace("_", " ").title()),
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        action,
                        generation=ctx.generation,
                    ),
                }
                for action in ctx.actions
            ],
        }

    def model_choice(self, ctx: InteractionContext) -> dict[str, Any]:
        current = str(ctx.tool_input.get("current", "") or "")
        models = ctx.tool_input.get("models", [])
        entries = [
            (str(model.get("slug", "") or ""), str(model.get("display_name", "") or "") or str(model.get("slug", "") or ""))
            for model in (models if isinstance(models, list) else [])
            if isinstance(model, dict) and str(model.get("slug", "") or "")
        ]
        # Prefix-related slugs (claude-opus-4 vs claude-opus-4-8) can both
        # match a dated live id; mark only the LONGEST match as current.
        matched_slug = max(
            (slug for slug, _ in entries if _model_slug_matches(slug, current)),
            key=len,
            default="",
        )
        if current and not matched_slug:
            # The session runs a model the configured list does not name (the
            # list is hand-maintained): still show what is current.
            entries.insert(0, (current, current))
            matched_slug = current
        actions = []
        for slug, display in entries:
            actions.append(
                {
                    "action": slug,
                    "label": f"✓ {display}（当前）" if slug == matched_slug else display,
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        slug,
                        generation=ctx.generation,
                    ),
                }
            )
        return {
            "type": "model_choice",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "current": matched_slug or current,
            "actions": actions,
        }

    def ask_user_question_prompt(self, ctx: InteractionContext) -> dict[str, Any]:
        # All questions live in a single card: each question is its own section
        # with option buttons (single-select = radio, multi-select = toggle),
        # answers are changeable, and one global Submit finalizes everything.
        # (Feishu has no tab widget, so sections are stacked vertically.)
        def tok(action: str) -> str:
            return self.interactions.create_callback_token(
                ctx.interaction_id, action, generation=ctx.generation
            )

        # One simple question (single-select, no free-text) finalizes on a
        # single click — no separate Submit step. Any other shape (multiple
        # questions, multi-select, or free-text) uses the batch card where
        # answers are changeable and one Submit commits them all.
        immediate = (
            len(ctx.questions) == 1
            and not bool(ctx.questions[0].get("allow_multiple"))
            and not bool(ctx.questions[0].get("allow_other"))
        )
        questions: list[dict[str, Any]] = []
        for q_index, question in enumerate(ctx.questions):
            multi = bool(question.get("allow_multiple"))
            answer = ctx.answers.get(q_index)
            selected_set = set(answer) if isinstance(answer, list) else ({answer} if answer else set())
            options = []
            for o_index, option in enumerate(question.get("options", [])):
                if immediate:
                    action = f"answer:{q_index}:{o_index}"
                else:
                    action = f"{'toggle' if multi else 'set'}:{q_index}:{o_index}"
                options.append(
                    {
                        "action": action,
                        "label": str(option),
                        "selected": str(option) in {str(s) for s in selected_set},
                        "token": tok(action),
                    }
                )
            other = None
            if question.get("allow_other"):
                other_action = f"other:{q_index}"
                other = {"action": other_action, "token": tok(other_action)}
            # Answer chosen via free text (not one of the options) is shown too.
            answer_text = ""
            if isinstance(answer, str) and answer and answer not in {str(o["label"]) for o in options}:
                answer_text = answer
            elif isinstance(answer, list):
                answer_text = ", ".join(str(v) for v in answer)
            elif isinstance(answer, str):
                answer_text = answer
            questions.append(
                {
                    "index": q_index,
                    "prompt": str(question.get("prompt", "")),
                    "header": str(question.get("header", "") or ""),
                    "allow_multiple": multi,
                    "options": options,
                    "other": other,
                    "answer_display": answer_text,
                }
            )
        submit = None if immediate else {"action": "submit_all", "label": "Submit", "token": tok("submit_all")}
        # Flattened actions for generic button renderers (and the plain-text
        # fallback); the Lark card renderer uses the structured `questions`
        # layout instead.
        flat_actions: list[dict[str, Any]] = []
        for q in questions:
            for opt in q["options"]:
                flat_actions.append(
                    {"action": opt["action"], "label": opt["label"], "token": opt["token"]}
                )
            if q["other"]:
                flat_actions.append(
                    {"action": q["other"]["action"], "label": "Other", "token": q["other"]["token"]}
                )
        if submit is not None:
            flat_actions.append(submit)
        return {
            "type": "ask_user_question",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "questions": questions,
            "submit": submit,
            "actions": flat_actions,
        }

    def takeover_prompt_for_context(
        self,
        ctx: InteractionContext,
        *,
        recoverability: str,
        summary: str,
    ) -> dict[str, Any]:
        labels = {
            "takeover_and_send": "Take over and send" if str(summary or "").strip() else "Take over",
        }
        actions = [action for action in ctx.actions if action == "takeover_and_send"]
        return {
            "type": "takeover_prompt",
            "interaction_id": ctx.interaction_id,
            "session_id": ctx.session_id,
            "generation": ctx.generation,
            "takeover_id": str(ctx.tool_input.get("takeover_id", "")),
            "blocked_input_id": str(ctx.tool_input.get("blocked_input_id", "")),
            "recoverability": recoverability,
            "summary": summary,
            "actions": [
                {
                    "action": action,
                    "label": labels.get(action, action.replace("_", " ").title()),
                    "token": self.interactions.create_callback_token(
                        ctx.interaction_id,
                        action,
                        generation=ctx.generation,
                    ),
                }
                for action in actions
            ],
        }

    @staticmethod
    def takeover_progress(
        *,
        takeover_id: str,
        blocked_input_id: str,
        phase: str,
        summary: str = "",
        reason: str = "",
    ) -> dict[str, Any]:
        return {
            "type": "takeover_progress",
            "takeover_id": takeover_id,
            "blocked_input_id": blocked_input_id,
            "phase": phase,
            "summary": summary,
            "reason": reason,
        }

    @staticmethod
    def tui_conflict_notice(
        *,
        kind: str,
        session_id: str,
        pid: int = 0,
        command: str = "",
        detail: str = "",
    ) -> dict[str, Any]:
        """Explicit channel notice for TUI-ownership events.

        kind:
          - "handback": a live TUI (re)claimed the session; channel mirrors read-only.
          - "remnant_terminated": the post-takeover sentinel killed a surviving TUI process.
          - "remnant_detected": a surviving TUI was seen but could not be terminated.
        """
        return {
            "type": "tui_conflict_notice",
            "kind": kind,
            "session_id": session_id,
            "pid": int(pid or 0),
            "command": command,
            "detail": detail,
        }

    @staticmethod
    def manual_only(
        *,
        takeover_id: str,
        blocked_input_id: str,
        summary: str = "",
        reason: str = "no structured resume reference available",
        suggested_steps: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "type": "manual_only",
            "takeover_id": takeover_id,
            "blocked_input_id": blocked_input_id,
            "summary": summary,
            "reason": reason,
            "suggested_steps": list(
                suggested_steps
                or [
                    "Stop or finish the current TUI process.",
                    "Start a new IM-owned agent session if you want to continue from chat.",
                ]
            ),
        }

    @staticmethod
    def stale_hitl_after_takeover(
        request: HitlRequest,
        *,
        reason: str = "The prompt belonged to the read-only TUI writer before takeover.",
    ) -> dict[str, Any]:
        return {
            "type": "hitl_stale",
            "hitl_request_id": request.hitl_request_id,
            "session_id": request.session_id,
            "generation": request.generation,
            "transport_request_id": request.transport_request_id,
            "native_method": request.native_method,
            "prompt_kind": request.prompt_kind,
            "reason": reason,
        }

    @staticmethod
    def health_view(
        *,
        status: str,
        title: str,
        session_id: str,
        transport: str,
        elapsed: float,
        cwd: str,
        lifecycle_state: str = "",
        writer_owner: str = "",
        last_progress_event: str = "",
        last_event_seq: int = 0,
        readonly: bool = False,
        actions: list[dict[str, Any]] | None = None,
        model: str = "",
        context_used: int = 0,
        context_limit: int = 0,
        background_tasks: int = 0,
        agent_session_id: str = "",
    ) -> dict[str, Any]:
        return {
            "type": "health",
            "status": status,
            "title": title,
            "session_id": session_id,
            # The agent's own session id (codex threadId / claude session
            # uuid). Distinct from session_id, which is WalkCode's ledger key
            # and means nothing to `codex resume` / `claude --resume`.
            "agent_session_id": agent_session_id,
            "transport": transport,
            "elapsed": elapsed,
            "cwd": cwd,
            "lifecycle_state": lifecycle_state,
            "writer_owner": writer_owner,
            "last_progress_event": last_progress_event,
            "last_event_seq": last_event_seq,
            "readonly": readonly,
            "actions": list(actions or []),
            "model": model,
            "context_used": context_used,
            "context_limit": context_limit,
            # Background subagents still running inside the agent process.
            "background_tasks": background_tasks,
        }

    @staticmethod
    def decision_result(
        *, kind: str, tool_name: str = "", action: str = "", detail: str = ""
    ) -> dict[str, Any]:
        # Terminal card shown in place of an interactive prompt once decided, so
        # a settled request no longer shows live buttons (V2 result-card parity).
        return {
            "type": "decision_result",
            "kind": kind,
            "tool_name": tool_name,
            "action": action,
            "detail": detail,
        }

    @staticmethod
    def session_chooser(
        *,
        reason: str,
        sessions: list[SessionSummary],
    ) -> dict[str, Any]:
        return {
            "type": "session_chooser",
            "reason": reason,
            "sessions": [
                {
                    "session_id": item.session_id,
                    "transport_kind": item.transport_kind,
                    "status": item.status,
                    "lifecycle_state": item.lifecycle_state,
                    "title": item.title,
                    "root_message_id": item.root_message_id,
                    "thread_id": item.thread_id,
                    "cwd": item.cwd,
                }
                for item in sessions
            ],
        }


def render_view_text(view_model: dict[str, Any]) -> str:
    if view_model.get("type") == "background_tasks":
        # Ledger beat for the status card; never rendered as channel text.
        return ""
    if "text" in view_model:
        return str(view_model["text"])
    if "message" in view_model:
        return str(view_model["message"])
    view_type = view_model.get("type")
    if view_type == "permission_prompt":
        return f"Permission requested: {view_model.get('tool_name', '')}"
    if view_type == "ask_user_question":
        questions = view_model.get("questions")
        if isinstance(questions, list) and questions:
            titles = [
                str(q.get("header") or q.get("prompt") or "")
                for q in questions
                if isinstance(q, dict)
            ]
            return "请选择：" + " / ".join(t for t in titles if t)
        return str(view_model.get("prompt", "请选择"))
    if view_type == "tui_user_input":
        # Terminal-side keystrokes mirrored to the channel (channel-originated
        # input is deduped upstream and never rendered as an echo).
        value = str(view_model.get("input", "") or "").strip()
        return f"⌨️ 终端输入\n\n{value}" if value else "⌨️ 终端输入"
    if view_type == "tui_conflict_notice":
        kind = str(view_model.get("kind", "") or "")
        pid = int(view_model.get("pid", 0) or 0)
        detail = str(view_model.get("detail", "") or "")
        if kind == "handback":
            rows = ["🖥️ 终端 TUI 已接回会话，本频道转为只读镜像。"]
            rows.append("需要继续用频道驱动，请对新消息重新接管。")
        elif kind == "remnant_terminated":
            rows = [f"🧹 已终止终端进程 (pid {pid})：该会话由频道驱动，检测到终端进程双写，已清理。"]
        else:
            rows = [f"⚠️ 检测到终端进程 (pid {pid}) 与频道同时挂在该会话上，但未能终止。"]
            rows.append("请在终端手动退出它，避免双写。")
        if detail:
            rows.append(detail)
        return "\n".join(rows)
    if view_type == "tui_permission_notice":
        tool = str(view_model.get("tool_name", "") or "tool")
        summary = str(view_model.get("summary", "") or "")
        rows = [f"⏳ TUI is waiting for your approval: {tool}"]
        if summary:
            rows.append(summary)
        rows.append("Answer in the terminal, or take over this session.")
        return "\n".join(rows)
    if view_type == "tool_progress":
        def _status_label(value: str) -> str:
            return {
                "running": "RUNNING",
                "completed": "COMPLETED",
                "failed": "FAILED",
            }.get(value, value.upper())

        lines = view_model.get("lines")
        entries = (
            [e for e in lines if isinstance(e, dict)]
            if isinstance(lines, list) and lines
            else [view_model]
        )
        # One tool keeps the original single-block layout; a coalesced burst
        # lists each tool on its own line. Narration entries (ADR 0055)
        # render as plain quoted lines.
        if len(entries) == 1 and str(entries[0].get("kind", "") or "") != "narration":
            entry = entries[0]
            rows = [
                "Agent activity",
                f"Status: {_status_label(str(entry.get('status', '') or 'running'))}",
                f"Tool: {entry.get('tool_name', '') or 'tool'}",
            ]
            summary = str(entry.get("summary", "") or "").strip()
            if summary:
                rows.append(f"Summary: {summary}")
            return "\n".join(rows)
        rows = ["Agent activity"]
        for entry in entries:
            if str(entry.get("kind", "") or "") == "narration":
                text = str(entry.get("text", "") or "").strip()
                if len(text) > 300:
                    text = text[:299] + "…"
                # Every physical line gets the quote prefix, or multi-line
                # narration bleeds into the tool-status rows.
                rows.extend(f"> {line}" for line in text.splitlines() if line.strip())
                continue
            label = _status_label(str(entry.get("status", "") or "running"))
            row = f"Status: {label} — {entry.get('tool_name', '') or 'tool'}"
            summary = str(entry.get("summary", "") or "").strip()
            if summary:
                row += f" — {summary}"
            rows.append(row)
        return "\n".join(rows)
    if view_type == "health":
        elapsed = float(view_model.get("elapsed", 0.0) or 0.0)
        context_used = int(view_model.get("context_used", 0) or 0)
        context_limit = int(view_model.get("context_limit", 0) or 0)
        if context_used and context_limit:
            context_text = f"{context_used}/{context_limit} ({round(context_used * 100 / context_limit)}%)"
        elif context_used:
            context_text = str(context_used)
        else:
            context_text = "-"
        rows = [
            f"WalkCode session: {view_model.get('title', '')}".strip(),
            f"Status: {view_model.get('status', '')}",
            f"Agent: {view_model.get('transport', '')}",
            f"Model: {view_model.get('model', '') or '-'}",
            f"Context: {context_text}",
            # Agent-native id first: it is the one `codex resume` /
            # `claude --resume` accept. The WalkCode ledger key stays visible
            # right under it for state/log lookups.
            f"Session: {view_model.get('agent_session_id', '') or '-'}",
            f"WalkCode: {view_model.get('session_id', '') or '-'}",
            f"State: {view_model.get('lifecycle_state', '') or '-'}",
            f"Writer: {view_model.get('writer_owner', '') or '-'}",
            f"Duration: {int(elapsed)}s",
            f"Progress: {view_model.get('last_progress_event', '') or '-'}",
            f"Seq: {view_model.get('last_event_seq', 0)}",
            f"Cwd: {view_model.get('cwd', '')}",
        ]
        background_tasks = int(view_model.get("background_tasks", 0) or 0)
        if background_tasks:
            rows.append(f"Background tasks: {background_tasks}")
        if view_model.get("readonly"):
            rows.append("Input: read-only until takeover")
        reason = str(view_model.get("reason", "") or "")
        if reason:
            rows.append(f"Reason: {reason}")
        return "\n".join(rows)
    if view_type == "error":
        return f"{view_model.get('code', 'error')}: {view_model.get('message', '')}"
    if view_type == "model_choice":
        return "Choose a model"
    if view_type == "decision_result":
        base = f"{view_model.get('action', 'decided')}: {view_model.get('tool_name', '')}".strip()
        detail = str(view_model.get("detail", "") or "")
        return f"{base.rstrip(':')} — {detail}" if detail else base
    if view_type == "session_chooser":
        rows = [
            "Multiple active sessions match this chat.",
            "Reply inside the target session topic/thread, or start a new task from the agent bot's root chat.",
        ]
        for item in view_model.get("sessions", [])[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("session_id") or "session")
            transport = str(item.get("transport_kind", "") or "agent")
            lifecycle = str(item.get("lifecycle_state", "") or item.get("status", ""))
            root = str(item.get("root_message_id", "") or "")
            suffix = f" root={root}" if root else ""
            rows.append(f"- {title} [{transport} {lifecycle}]{suffix}")
        return "\n".join(rows)
    if view_type == "takeover_prompt":
        return f"Takeover required: {view_model.get('summary', '')}"
    if view_type == "takeover_confirmation":
        return f"Confirm takeover: {view_model.get('summary', '')}"
    if view_type == "takeover_progress":
        phase = str(view_model.get("phase", ""))
        reason = str(view_model.get("reason", ""))
        labels = {
            "terminating_external_tui": "Stopping the TUI session...",
            "resuming_structured": "Taking over the session...",
            "submitting_blocked_input": "Sending your message...",
            "submitted_blocked_input": (
                "Takeover completed. Message sent; waiting for the reply."
            ),
            "failed": "Takeover failed",
        }
        if phase == "completed":
            return "Takeover completed. You can now send messages in this topic."
        label = labels.get(phase, "Takeover in progress")
        if reason:
            return f"{label}: {reason}"
        return label
    if view_type == "manual_only":
        return f"Cannot take over automatically: {view_model.get('reason', '')}"
    if view_type == "hitl_stale":
        return (
            "Previous human input request is no longer answerable after the session handoff.\n"
            f"Type: {view_model.get('prompt_kind', '')}\n"
            f"Reason: {view_model.get('reason', '')}"
        )
    if view_model.get("type") == "unknown_event":
        return f"[{view_model.get('event_type')}] {view_model}"
    return str(view_model)
