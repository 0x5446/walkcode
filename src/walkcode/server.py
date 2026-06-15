"""WalkCode server: FastAPI for hooks + Feishu WebSocket for events."""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from os.path import basename
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    GetMessageResourceRequest,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.api.im.v1.model.emoji import Emoji
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
    CallBackCard,
)
from importlib.metadata import version as pkg_version

from fastapi import FastAPI, Request

from .agent import AgentAdapter, get_agent
from .config import Config
from .i18n import t
from .permreg import CardStatus, PermissionRegistry
from .state import Session, SessionStore
from .tty import inject, validate_target, get_session_activity, kill_session, capture_pane, is_agent_alive, wait_until_input_ready

logger = logging.getLogger("walkcode")

app = FastAPI(title="WalkCode", version=pkg_version("walkcode"))

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
agent_adapter: AgentAdapter = None  # type: ignore
_IDLE_TIMEOUT = 7200  # 2h — kill tmux sessions idle longer than this
_REAPER_INTERVAL = 600  # 10min — how often the idle reaper runs
_WATCHDOG_INTERVAL = 120  # 2min — how often the stuck-turn watchdog scans
# Warn on the Feishu thread when a turn has been "Working" this long with no
# result (the agent may be wedged on an interactive step, e.g. a browser/login
# that never returns). Env-overridable for tuning without a code change.
_STUCK_THRESHOLD = int(os.environ.get("WALKCODE_STUCK_THRESHOLD", "1800"))  # 30min


# --- Permission request state ---
# All permission state lives in one PermissionRegistry behind a single lock (see
# permreg.py): write-once decisions (no allow→deny tearing), mutually-exclusive
# poll-vs-tmux-fallback, and codex 0.135 double-fire dedupe by tool_use_id (the
# duplicate reuses the first request_id; both pollers read the SAME decision).
# AskUserQuestion is Claude-only (no tool_use_id → key None) → never deduped, so
# its multi-step / Other flow is untouched.
registry = PermissionRegistry()


# --- Feishu reply injection observation ---
# For user-visible delivery, the boundary is tmux accepting paste+Enter. Claude
# Code may process it immediately or queue it behind the current turn; WalkCode
# should not second-guess that with its own busy/idle model.
#
# UserPromptSubmit/Stop still provide useful observability, so we keep a small
# in-memory pending list for debug logs. It never drives a user-visible
# "swallowed" or "queued" verdict.
_session_last_ups: dict[str, float] = {}   # session_id → ts of last UserPromptSubmit
_session_last_stop: dict[str, float] = {}  # session_id → ts of last Stop
_ups_capable_sessions: set[str] = set()    # sessions seen to emit UserPromptSubmit
_pending_injects: list[dict] = []          # optional observations only
_pending_lock = threading.Lock()
_INJECT_OBSERVATION_TTL = 3600.0
_SWEEPER_INTERVAL = 60.0       # s between observation cleanup sweeps


# --- Hook delivery dedupe ---
# codex CLI (>=0.135) fires each hook event TWICE: two identical hook processes
# launched microseconds apart with the same payload (same turn_id). Each /hook
# POST would otherwise produce a duplicate Feishu reply. Hook delivery is
# "at-least-once" by nature, so we dedupe on the consumer side — a turn ends once
# → one notification.
#
# A key registers ONLY after a successful send (see _hook_mark_delivered), never
# before. So if the first delivery's _reply fails, the slot stays open and
# codex's duplicate re-sends instead of being silently dropped — at-least-once is
# preserved. Check-then-send-then-mark is atomic because receive_hook runs on the
# asyncio loop with NO `await` between the dedupe check and the send (the lark
# calls are synchronous and block the loop), so a second request cannot interleave
# until the first has marked the key. Keys are tuples (no string-concat collision
# from attacker-controlled session_id/turn_id).
_recent_hook_keys: dict[tuple, float] = {}  # dedupe key (tuple) → delivered-at ts
_hook_dedupe_lock = threading.Lock()
# turn_id keys are precise — a long TTL never false-dedupes distinct turns.
_HOOK_DEDUPE_TTL_TURN = 30.0
# message-hash fallback (Claude, no turn_id) is coarse: keep the window tiny so it
# only collapses near-simultaneous re-delivery, never two genuine identical replies
# minutes apart. Claude never duplicates anyway, so this is pure defense.
_HOOK_DEDUPE_TTL_HASH = 2.0


# --- Unified permission card builder ---
# Each perm_type defines: behavior values per button position, button-text i18n keys, header, template.
#
# Button text is intentionally NOT derived from terminal screen scraping. The
# old implementation captured numbered lines from `tmux capture-pane`, which
# misidentified any preceding Claude output (plan steps, todos, etc.) as
# permission options. Button semantics are owned by walkcode (see _PERM_BEHAVIORS)
# and do not depend on what Claude Code happens to render in the TUI.

_PERM_BEHAVIORS = {
    "plan":     ["plan_auto_accept", "plan_manual_approve", "deny"],
    "setMode":  ["allow", "accept_edits", "deny"],
    "addRules": ["allow", "always_allow", "deny"],
}
_PERM_BUTTON_LABELS = {
    "plan":     ["feishu.plan.auto_accept", "feishu.plan.manual_approve", "feishu.plan.tell_claude"],
    "setMode":  ["feishu.setmode.yes", "feishu.setmode.accept_edits", "feishu.setmode.no"],
    "addRules": ["feishu.perm.allow", "feishu.perm.always_allow", "feishu.perm.deny"],
}
_PERM_BUTTON_TYPES = ["primary", "default", "danger"]

_DESTINATION_LABELS = {
    "session":        "feishu.perm.dest_session",
    "localSettings":  "feishu.perm.dest_local",
    "userSettings":   "feishu.perm.dest_user",
    "projectSettings": "feishu.perm.dest_project",
}


def _format_permission_suggestions(suggestions: list) -> str:
    """Render permission_suggestions as a short markdown block for the card body.

    Returns empty string when there is nothing to show. Each suggestion item is
    classified by its `type` and rendered with the most user-relevant fields:
    addRules → toolName + ruleContent; setMode → mode name; addDirectories →
    directory list. The destination (session / localSettings / ...) is shown
    so the user understands the scope of "always allow".
    """
    if not suggestions:
        return ""
    lines = []
    for s in suggestions:
        stype = s.get("type")
        dest = s.get("destination", "")
        dest_label = t(_DESTINATION_LABELS[dest]) if dest in _DESTINATION_LABELS else dest
        if stype == "addRules":
            for r in s.get("rules", []):
                tool = r.get("toolName", "")
                rule = r.get("ruleContent")
                if rule:
                    lines.append(f"- `{tool}` `{rule}` _({dest_label})_")
                else:
                    lines.append(f"- `{tool}` _({dest_label})_")
        elif stype == "setMode":
            mode = s.get("mode", "")
            lines.append(f"- setMode: `{mode}` _({dest_label})_")
        elif stype == "addDirectories":
            for d in s.get("directories", []):
                lines.append(f"- addDirectory: `{d}` _({dest_label})_")
        else:
            lines.append(f"- {stype} _({dest_label})_")
    if not lines:
        return ""
    return f"\n\n**{t('feishu.perm.suggestion_label')}**\n" + "\n".join(lines)


def _build_permission_card(
    request_id: str, perm_type: str, tool_name: str,
    tool_input: dict, permission_suggestions: list | None = None,
) -> dict:
    """Build a Feishu permission card.

    Button labels come from i18n (never from terminal screen scraping).
    When `permission_suggestions` is present, the rule scope is rendered in
    the card body so the user knows what "always allow" will cover.
    """
    behaviors = _PERM_BEHAVIORS.get(perm_type, _PERM_BEHAVIORS["addRules"])
    label_keys = _PERM_BUTTON_LABELS.get(perm_type, _PERM_BUTTON_LABELS["addRules"])

    buttons = []
    for behavior, btn_type, key in zip(behaviors, _PERM_BUTTON_TYPES, label_keys):
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": t(key)},
            "type": btn_type,
            "value": {"rid": request_id, "b": behavior},
        })

    # Content: plan shows markdown, others show tool + input JSON
    if perm_type == "plan":
        plan = tool_input.get("plan", "")
        if len(plan) > 800:
            plan = plan[:800] + "\n..."
        content = plan
        header = t("feishu.plan.header")
        template = "blue"
    else:
        input_str = json.dumps(tool_input, indent=2, ensure_ascii=False)
        if len(input_str) > 500:
            input_str = input_str[:500] + "\n..."
        content = f"**Tool:** `{tool_name}`\n**Input:**\n```json\n{input_str}\n```"
        content += _format_permission_suggestions(permission_suggestions or [])
        header = t("feishu.setmode.header") if perm_type == "setMode" else t("feishu.perm.header")
        template = "orange"

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": header}, "template": template},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "action", "actions": buttons},
        ],
    }


def _build_askuserquestion_card(
    request_id: str,
    questions: list,
    question_index: int = 0,
    selected_indices: list[int] | None = None,
    other_pending: bool = False,
) -> dict:
    """Build the Feishu card for one AskUserQuestion question.

    Supports three interaction modes per question:
    - single-select: clicking an option finalizes the answer immediately
    - multi-select: clicking an option toggles its selection; a Submit button
      finalizes the chosen subset (joined with comma in updatedInput.answers)
    - Other: a button asking the user to reply with custom text in the thread.
      The next plain-text reply on the thread becomes the answer for this
      question.

    selected_indices: for multiSelect, current toggled set (1-based option idx);
                      ignored otherwise.
    other_pending: when True, render an instruction card asking the user to
                   send a thread reply.
    """
    if not questions:
        return _empty_card("Question", "No questions found")

    if question_index >= len(questions):
        question_index = 0

    q = questions[question_index]
    question_text = q.get("question", "Choose an option:")
    options = q.get("options", [])
    multi_select = bool(q.get("multiSelect"))

    total_questions = len(questions)
    progress = f"({question_index + 1}/{total_questions})" if total_questions > 1 else ""
    title = f"{question_text} {progress}".strip()

    if other_pending:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "yellow",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            "✏️ **请在本条消息所在的话题里直接回复你想要的自定义答案文本**。\n"
                            "下一条文本回复会作为该问题的答案提交给 Claude。"
                        ),
                    },
                },
            ],
        }

    if not options:
        return _empty_card(title, "⚠️ No options available for this question")

    selected = set(selected_indices or [])

    # Each option = a button. For multiSelect, prefix selected ones with ✓ and
    # use a different button type so the toggle state is visible. The button
    # value's `action` field tells _on_card_action whether this is a final
    # selection (single-select) or a toggle (multi-select).
    buttons = []
    for j, opt in enumerate(options):
        idx = j + 1  # 1-based option position (kept stable across turns)
        label = opt.get("label", opt.get("value", ""))
        if multi_select:
            checked = idx in selected
            text = f"{'✓ ' if checked else ''}{label}"
            btn_type = "primary" if checked else "default"
            action = "toggle"
        else:
            text = label
            btn_type = "primary"
            action = "select"
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": btn_type,
            "value": {
                "rid": request_id,
                "action": action,
                "answer": label,
                "option_idx": idx,
                "question_index": question_index,
                "total_questions": total_questions,
            },
        })

    # Bottom row: per-question control buttons.
    # - multiSelect: Submit (finalizes selected list)
    # - Any: Other (prompts thread reply for free-form text)
    control_buttons = []
    if multi_select:
        control_buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"✅ 提交所选 ({len(selected)})"},
            "type": "primary",
            "value": {
                "rid": request_id,
                "action": "submit_multi",
                "question_index": question_index,
                "total_questions": total_questions,
            },
        })
    control_buttons.append({
        "tag": "button",
        "text": {"tag": "plain_text", "content": "✏️ 其他（自定义文本）"},
        "type": "default",
        "value": {
            "rid": request_id,
            "action": "request_other",
            "question_index": question_index,
            "total_questions": total_questions,
        },
    })

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": [
            {"tag": "action", "actions": buttons},
            {"tag": "hr"},
            {"tag": "action", "actions": control_buttons},
        ],
    }


def _empty_card(title: str, message: str) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": message}}],
    }


def _build_permission_result_card(tool_name: str, behavior: str) -> dict:
    """Build a result card showing the permission decision."""
    if behavior == "always_allow":
        label = t("feishu.perm.always_allowed")
        template = "green"
    elif behavior == "accept_edits":
        label = t("feishu.setmode.accepted")
        template = "green"
    elif behavior in ("allow", "plan_auto_accept", "plan_manual_approve"):
        label = t("feishu.perm.allowed")
        template = "green"
    else:
        label = t("feishu.perm.denied")
        template = "red"
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": label},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**Tool:** `{tool_name}`"}},
        ],
    }


def _labels() -> dict[str, str]:
    return {
        "stop": t("feishu.label.stop"),
        "permission_prompt": t("feishu.label.permission"),
        "idle_prompt": t("feishu.label.idle"),
        "elicitation_dialog": t("feishu.label.elicitation"),
    }


def _resolve_session_id(msg) -> str | None:
    return session_store.resolve(
        root_id=getattr(msg, "root_id", ""),
        parent_id=getattr(msg, "parent_id", ""),
    )


def _load_reply_session(session_id: str) -> tuple[Session | None, str | None]:
    """Load a session for replying. Returns (session, error).

    When tmux is dead, still returns the session data so callers can resume.
    """
    session = session_store.get(session_id)
    if not session:
        return None, None

    error = validate_target(session.tty)
    if error:
        logger.warning("Session %s target invalid: %s", session_id[:8], error)
        return session, error

    return session_store.touch(session_id), None


# --- Feishu helpers ---

_SUCCESS_EMOJIS = [
    "THUMBSUP", "OK", "JIAYI", "MUSCLE", "DONE", "YEAH", "APPLAUSE",
    "Fire", "LGTM", "CheckMark", "Hundred", "SMILE", "Get", "OnIt",
    "HEART", "CLAP", "FISTBUMP", "HIGHFIVE",
]
_FAILURE_EMOJIS = [
    "FACEPALM", "CRY", "SOB", "CrossMark", "FROWN", "Sigh",
    "SWEAT", "WRONGED", "TERROR",
]

_MENTION_RE = re.compile(r"@_user_\d+\s*")
_IMAGE_DIR = Path.home() / ".walkcode" / "images"

# Single-worker executor so the Lark SDK callback returns immediately and the
# SDK can ack the WebSocket frame without waiting for tmux/HTTP work. Single
# worker preserves FIFO ordering of the original synchronous dispatch.
_msg_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkcode-msg")


_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG": "png",
    b"GIF8": "gif",
    b"RIFF": "webp",  # RIFF....WEBP
}


def _detect_image_ext(data: bytes) -> str:
    """Detect image format from magic bytes."""
    for magic, ext in _IMAGE_MAGIC.items():
        if data[:len(magic)] == magic:
            return ext
    return "png"


def _download_image(message_id: str, image_key: str) -> str | None:
    """Download an image from Feishu and save to local disk. Returns absolute path or None."""
    _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        request = GetMessageResourceRequest.builder() \
            .message_id(message_id) \
            .file_key(image_key) \
            .type("image") \
            .build()
        resp = lark_client.im.v1.message_resource.get(request)
        if not resp.success():
            logger.error(f"Download image failed: {resp.code} {resp.msg}")
            return None
        data = resp.file.read()
        ext = _detect_image_ext(data)
        filename = f"{int(time.time())}_{image_key[:16]}.{ext}"
        filepath = _IMAGE_DIR / filename
        filepath.write_bytes(data)
        logger.info(f"Downloaded image: {filepath} ({len(data)} bytes)")
        return str(filepath)
    except Exception as e:
        logger.error(f"Download image error: {e}")
        return None


def _parse_message_content(msg, message_id: str) -> str | None:
    """Parse Feishu message content (text/image/post) into injectable text.

    Returns the text to inject (may include markdown image refs), or None if empty/unsupported.
    """
    msg_type = msg.message_type
    try:
        content = json.loads(msg.content)
    except (json.JSONDecodeError, TypeError):
        return None

    if msg_type == "text":
        text = content.get("text", "").strip()
        return _MENTION_RE.sub("", text).strip() or None

    if msg_type == "image":
        image_key = content.get("image_key", "")
        if not image_key:
            return None
        path = _download_image(message_id, image_key)
        if not path:
            return None
        return f"[{t('image.label', n=1)}]({path})"

    if msg_type == "post":
        return _parse_post_content(content, message_id)

    return None


def _parse_post_content(content: dict, message_id: str) -> str | None:
    """Parse a post (rich text) message, preserving text and image positions."""
    # post content structure: {"title": "...", "content": [[{tag, ...}], ...]}
    # content may be localized: {"zh_cn": {"title": ..., "content": ...}}
    post_body = content
    if "content" not in post_body:
        # Try localized keys
        for key in ("zh_cn", "en_us", "ja_jp"):
            if key in content:
                post_body = content[key]
                break
    paragraphs = post_body.get("content", [])
    if not paragraphs:
        return None

    parts: list[str] = []
    title = post_body.get("title", "").strip()
    if title:
        parts.append(title)

    img_counter = 0
    for paragraph in paragraphs:
        line_parts: list[str] = []
        for element in paragraph:
            tag = element.get("tag", "")
            if tag == "text":
                t_text = element.get("text", "")
                if t_text:
                    line_parts.append(t_text)
            elif tag == "at":
                # skip @mentions
                pass
            elif tag == "a":
                href = element.get("href", "")
                a_text = element.get("text", href)
                line_parts.append(f"[{a_text}]({href})" if href else a_text)
            elif tag == "img":
                image_key = element.get("image_key", "")
                if image_key:
                    img_counter += 1
                    path = _download_image(message_id, image_key)
                    if path:
                        line_parts.append(f"[{t('image.label', n=img_counter)}]({path})")
                    else:
                        line_parts.append(f"[{t('image.download_failed')}]")
        if line_parts:
            parts.append("".join(line_parts))

    result = "\n".join(parts).strip()
    result = _MENTION_RE.sub("", result).strip()
    return result or None


def _make_title(cwd: str, session_id: str = "", message: str = "") -> str:
    project = basename(cwd) if cwd else "unknown"
    parts = [project]
    if session_id:
        parts.append(session_id[:8])
    if message:
        snippet = message[:22].rstrip()
        ellipsis = "..." if len(message) > 22 else ""
        parts.append(f"{snippet}{ellipsis}")
    return " | ".join(parts)


def _post_content(text: str) -> str:
    return json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": text}]]}})


def _send(text: str) -> str | None:
    if not config.feishu_receive_id:
        logger.warning("Cannot send: FEISHU_RECEIVE_ID not configured")
        return None
    body = CreateMessageRequestBody.builder() \
        .receive_id(config.feishu_receive_id) \
        .msg_type("post") \
        .content(_post_content(text)) \
        .build()
    req = CreateMessageRequest.builder() \
        .receive_id_type(config.feishu_receive_id_type) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"Send failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _reply(message_id: str, text: str, reply_in_thread: bool = False) -> str | None:
    builder = ReplyMessageRequestBody.builder() \
        .msg_type("post") \
        .content(_post_content(text))
    if reply_in_thread:
        builder = builder.reply_in_thread(True)
    body = builder.build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.reply(req)
    if not resp.success():
        logger.error(f"Reply failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _edit_message(message_id: str, text: str):
    body = PatchMessageRequestBody.builder() \
        .content(_post_content(text)) \
        .build()
    req = PatchMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.patch(req)
    if not resp.success():
        logger.error(f"Edit message failed: {resp.code} {resp.msg}")


def _add_reaction(message_id: str, emoji_type: str):
    emoji = Emoji.builder().emoji_type(emoji_type).build()
    body = CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
    req = CreateMessageReactionRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message_reaction.create(req)
    if not resp.success():
        logger.error(f"Add reaction failed: {resp.code} {resp.msg}")


def _reply_card(message_id: str, card: dict, reply_in_thread: bool = False) -> str | None:
    """Reply with an interactive card message."""
    content = json.dumps(card)
    builder = ReplyMessageRequestBody.builder() \
        .msg_type("interactive") \
        .content(content)
    if reply_in_thread:
        builder = builder.reply_in_thread(True)
    body = builder.build()
    req = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.reply(req)
    if not resp.success():
        logger.error(f"Reply card failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _send_card(card: dict) -> str | None:
    """Send an interactive card as a new message."""
    if not config.feishu_receive_id:
        logger.warning("Cannot send card: FEISHU_RECEIVE_ID not configured")
        return None
    content = json.dumps(card)
    body = CreateMessageRequestBody.builder() \
        .receive_id(config.feishu_receive_id) \
        .msg_type("interactive") \
        .content(content) \
        .build()
    req = CreateMessageRequest.builder() \
        .receive_id_type(config.feishu_receive_id_type) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.create(req)
    if not resp.success():
        logger.error(f"Send card failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _edit_card(message_id: str, card: dict):
    """Update an interactive card message."""
    body = PatchMessageRequestBody.builder() \
        .content(json.dumps(card)) \
        .build()
    req = PatchMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(body) \
        .build()
    resp = lark_client.im.v1.message.patch(req)
    if not resp.success():
        logger.error(f"Edit card failed: {resp.code} {resp.msg}")


# --- tmux send-keys fallback ---

def _tmux_fallback(request_id: str, behavior: str, req_data: dict):
    """Fallback: if hook process timed out, send key via tmux to answer terminal prompt."""
    tty = req_data.get("tty", "")
    if not tty:
        return

    tool_name = req_data.get("tool_name", "")
    perm_suggestions = req_data.get("permission_suggestions", [])
    suggestion_type = perm_suggestions[0].get("type") if perm_suggestions else "addRules"
    hook_data_full = req_data.get("hook_data_full", {})
    permission_mode = hook_data_full.get("permission_mode", "")

    # Determine perm_type → build key_map from _PERM_BEHAVIORS
    if permission_mode == "plan" and not perm_suggestions:
        perm_type = "plan"
    elif suggestion_type == "setMode":
        perm_type = "setMode"
    else:
        perm_type = "addRules"
    behaviors = _PERM_BEHAVIORS.get(perm_type, _PERM_BEHAVIORS["addRules"])
    key_map = {b: str(i + 1) for i, b in enumerate(behaviors)}

    key = key_map.get(behavior, "3")

    error = validate_target(tty)
    if error:
        logger.warning(f"tmux fallback: session {tty} not found (rid={request_id[:8]})")
        return

    try:
        inject(tty, key, enter=True, menu_key=True)
        logger.info(f"tmux fallback: sent '{key}' to {tty} for {tool_name} behavior={behavior} (rid={request_id[:8]})")
    except Exception as e:
        logger.error(f"tmux fallback failed: {e} (rid={request_id[:8]})")


def _finalize_askuser_answer(
    resp,
    request_id: str,
    questions: list,
    question_index: int,
    total_questions: int,
    final_answer,
):
    """Persist the answer for question_index and either show next question or
    finalize the decision (signal the hook process)."""
    answers_list = registry.askuser_record_answer(request_id, question_index, final_answer)
    if answers_list is None:
        resp.toast = CallBackToast()
        resp.toast.type = "info"
        resp.toast.content = t("feishu.perm.expired")
        return resp

    logger.info(f"AskUserQuestion answer[{question_index}]: {final_answer!r} (rid={request_id[:8]})")

    has_next = question_index + 1 < total_questions
    if has_next:
        next_card = _build_askuserquestion_card(request_id, questions, question_index + 1)
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = next_card
        resp.toast = CallBackToast()
        resp.toast.type = "success"
        resp.toast.content = f"Q{question_index + 1}/{total_questions} answered"
        return resp

    # All answered → build final decision with updatedInput (write-once)
    final_decision = {
        "behavior": "allow",
        "answers": answers_list,
        "updatedInput": _build_askuser_updated_input(questions, answers_list),
    }
    registry.set_decision_once(request_id, final_decision)

    resp.card = CallBackCard()
    resp.card.type = "raw"
    resp.card.data = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "All questions answered"},
            "template": "green",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": f"✓ All {total_questions} question(s) answered"}},
        ],
    }
    resp.toast = CallBackToast()
    resp.toast.type = "success"
    resp.toast.content = "All answers submitted"
    return resp


def _build_askuser_updated_input(questions: list, answers: list) -> dict:
    """Build PermissionRequest decision.updatedInput payload for AskUserQuestion.

    Per Claude Code hook spec, returning the original `questions` array along
    with an `answers` map (question text → selected label) makes Claude consume
    the answers directly without rendering its native TUI prompt. This replaces
    the old tmux send-keys injection path entirely.

    For multiSelect questions, Claude expects labels joined by commas.
    """
    answers_map: dict[str, str] = {}
    for i, q in enumerate(questions):
        question_text = q.get("question", "")
        if not question_text or i >= len(answers):
            continue
        ans = answers[i]
        if ans is None:
            continue
        # multiSelect: hook layer may send list; spec says join with comma.
        if isinstance(ans, list):
            ans = ",".join(str(x) for x in ans)
        answers_map[question_text] = ans
    return {"questions": questions, "answers": answers_map}


def _maybe_tmux_fallback(request_id: str, behavior: str, req_data: dict):
    """Inject the decision via tmux iff NO poller consumed it (hook died/timed out).

    claim_fallback atomically settles the poll-vs-fallback race: it succeeds only
    when no poller has consumed the decision AND the request has been quiet (no
    poll) for _FALLBACK_QUIESCE, so an actively-polling hook is never raced. Once
    claimed, try_consume can no longer hand the decision to a late poller, so the
    decision is never both delivered AND injected.
    """
    if not registry.claim_fallback(request_id):
        return
    logger.info(f"Hook timed out, tmux fallback (rid={request_id[:8]})")
    _tmux_fallback(request_id, behavior, req_data)
    registry.remove(request_id)


def _schedule_tmux_fallback(request_id: str, behavior: str, req_data: dict):
    """Wait 5s, then run the tmux fallback backstop if still unconsumed."""
    def _check():
        time.sleep(5)
        _maybe_tmux_fallback(request_id, behavior, req_data)
    threading.Thread(target=_check, daemon=True).start()


# --- Card action handler ---

def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """Handle Feishu card button clicks for permission decisions and AskUserQuestion answers."""
    resp = P2CardActionTriggerResponse()
    try:
        event = data.event
        if not event or not event.action:
            return resp

        value = event.action.value or {}
        request_id = value.get("rid", "")

        if not request_id:
            return resp

        req_data = registry.get(request_id)
        if req_data is None:
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = t("feishu.perm.expired")
            return resp

        tool_name = req_data.tool_name or "unknown"

        # Handle AskUserQuestion: select / toggle / submit_multi / request_other
        if tool_name == "AskUserQuestion":
            action = value.get("action", "select")
            question_index = value.get("question_index", 0)
            total_questions = value.get("total_questions", 1)

            questions = req_data.tool_input.get("questions", [])

            if action == "toggle":
                # multiSelect: toggle option_idx in pending_selections[question_index]
                option_idx = value.get("option_idx")
                if option_idx is None:
                    resp.toast = CallBackToast()
                    resp.toast.type = "warning"
                    resp.toast.content = "Missing option_idx"
                    return resp
                selected = registry.askuser_toggle(request_id, question_index, option_idx)
                if selected is None:
                    resp.toast = CallBackToast()
                    resp.toast.type = "info"
                    resp.toast.content = t("feishu.perm.expired")
                    return resp
                logger.info(f"AskUser toggle Q{question_index+1} idx={option_idx} → selected={selected} (rid={request_id[:8]})")
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = _build_askuserquestion_card(
                    request_id, questions, question_index, selected_indices=selected,
                )
                return resp

            if action == "request_other":
                # Mark this rid+question waiting for a thread text reply.
                registry.askuser_set_awaiting_other(
                    request_id, question_index, req_data.feishu_root_msg_id,
                )
                logger.info(f"AskUser request_other Q{question_index+1} (rid={request_id[:8]})")
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = _build_askuserquestion_card(
                    request_id, questions, question_index, other_pending=True,
                )
                resp.toast = CallBackToast()
                resp.toast.type = "info"
                resp.toast.content = "请在话题里回复你的自定义文本"
                return resp

            # action == "select" (single-select final) or "submit_multi"
            if action == "submit_multi":
                selected = registry.askuser_get_selected(request_id, question_index)
                if not selected:
                    resp.toast = CallBackToast()
                    resp.toast.type = "warning"
                    resp.toast.content = "未选择任何选项"
                    return resp
                # Map selected option indices back to labels (1-based → 0-based)
                q = questions[question_index]
                opts = q.get("options", [])
                labels = [opts[i - 1].get("label", opts[i - 1].get("value", ""))
                          for i in selected if 0 < i <= len(opts)]
                final_answer = labels  # list → joined with comma in updatedInput
            else:
                # action == "select" — single-select option click
                final_answer = value.get("answer")
                if final_answer is None:
                    resp.toast = CallBackToast()
                    resp.toast.type = "warning"
                    resp.toast.content = "No answer provided"
                    return resp

            return _finalize_askuser_answer(
                resp, request_id, questions, question_index,
                total_questions, final_answer,
            )

        # Handle permission decisions
        behavior = value.get("b", "")
        if not behavior:
            return resp

        decision_behavior = "allow" if behavior in ("allow", "always_allow", "accept_edits", "plan_auto_accept", "plan_manual_approve") else "deny"
        perm_suggestions = req_data.permission_suggestions

        # Build the decision. `_button` records the clicked button so a later
        # (losing) click can echo the SAME verdict; clients read only behavior /
        # updatedPermissions / updatedInput and ignore the extra key.
        decision_dict = {
            "behavior": decision_behavior,
            "_button": behavior,
        }
        # Include updatedPermissions using original permission_suggestions from Claude Code
        if behavior == "always_allow":
            decision_dict["updatedPermissions"] = perm_suggestions if perm_suggestions else [{"type": "addRules", "rules": [{"toolName": tool_name}], "behavior": "allow", "destination": "localSettings"}]
        elif behavior == "accept_edits":
            decision_dict["updatedPermissions"] = perm_suggestions if perm_suggestions else [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
        elif behavior == "plan_auto_accept":
            decision_dict["updatedPermissions"] = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]

        # A (decision tearing): the FIRST click wins and is the only one that runs
        # side effects (signal hook, schedule fallback, persist rule). A double
        # click / codex double-fire / re-delivered callback loses and only echoes
        # the already-decided verdict — never overwrites allow↔deny.
        won = registry.set_decision_once(request_id, decision_dict)
        if not won:
            existing = req_data.decision or {}
            shown = existing.get("_button") or existing.get("behavior") or behavior
            logger.info(f"Permission decision ignored (already decided) for {tool_name} (rid={request_id[:8]})")
            resp.card = CallBackCard()
            resp.card.type = "raw"
            resp.card.data = _build_permission_result_card(tool_name, shown)
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = t("feishu.perm.already_decided")
            return resp

        # Schedule tmux fallback in case hook already timed out
        _schedule_tmux_fallback(request_id, behavior, req_data.snapshot())

        logger.info(f"Permission decision: {behavior} for {tool_name} (rid={request_id[:8]})")

        result_card = _build_permission_result_card(tool_name, behavior)

        # Return updated card inline (replaces buttons within 3s)
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = result_card

        # Toast notification
        if behavior == "always_allow":
            toast_text = t("feishu.perm.always_allowed")
        elif behavior == "accept_edits":
            toast_text = t("feishu.setmode.accepted")
        elif behavior in ("allow", "plan_auto_accept", "plan_manual_approve"):
            toast_text = t("feishu.perm.allowed")
        else:
            toast_text = t("feishu.perm.denied")
        resp.toast = CallBackToast()
        resp.toast.type = "success" if decision_behavior == "allow" else "warning"
        resp.toast.content = toast_text

        # If "always allow", add rule to settings.json (accept_edits is session-scoped, no persist)
        if behavior == "always_allow":
            _add_permission_rule(tool_name)

        return resp

    except Exception as e:
        logger.error(f"Card action error: {e}")
        return resp


def _add_permission_rule(tool_name: str):
    """Add a tool to ~/.claude/settings.json permissions.allow."""
    try:
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return
        settings = json.loads(settings_path.read_text())
        allow = settings.setdefault("permissions", {}).setdefault("allow", [])
        if tool_name not in allow:
            allow.append(tool_name)
            settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
            logger.info(f"Added permission rule: {tool_name}")
    except Exception as e:
        logger.error(f"Failed to add permission rule: {e}")


# --- Feishu WebSocket event handlers ---

def _auth_recovery_check(tmux_name: str, prompt: str, message_id: str, image_path: str | None):
    """Background: detect auth failure after agent start, run device-auth if supported."""
    if not agent_adapter.device_auth_command:
        return

    time.sleep(5)  # wait for agent to start and potentially fail

    # Check if session still exists
    if validate_target(tmux_name):
        return  # session already dead (handled elsewhere)

    # Capture pane output and check for auth errors
    output = capture_pane(tmux_name, 30)
    if not output:
        return

    matched = any(re.search(p, output, re.IGNORECASE) for p in agent_adapter.auth_error_patterns)
    if not matched:
        return  # no auth error, agent started normally

    logger.warning(f"Auth error detected in {tmux_name}, starting device-auth recovery")
    kill_session(tmux_name)
    session_store.pop_pending(tmux_name)

    # Run device-auth with Popen to stream stdout
    try:
        proc = subprocess.Popen(
            list(agent_adapter.device_auth_command),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )

        # Read output lines to find URL + code
        url = ""
        code = ""
        lines_read = []
        for line in proc.stdout:
            lines_read.append(line.rstrip())
            # Look for URL
            url_match = re.search(r'(https?://\S+)', line)
            if url_match and not url:
                url = url_match.group(1)
            # Look for device code (typically uppercase letters/digits with dash)
            code_match = re.search(r'\b([A-Z0-9]{4,}-[A-Z0-9]{4,})\b', line)
            if code_match and not code:
                code = code_match.group(1)
            # If we have both, notify user
            if url and code:
                break

        if url:
            auth_msg = t("feishu.auth_expired", url=url, code=code or "—")
            _reply(message_id, auth_msg, reply_in_thread=True)
            logger.info(f"Auth recovery: sent device-auth URL to Feishu (code={code})")
        else:
            # Couldn't parse URL, send raw output
            raw = "\n".join(lines_read[:10])
            _reply(message_id, t("feishu.auth_failed", error=raw), reply_in_thread=True)
            proc.terminate()
            return

        # Wait for user to complete auth (up to 5 min)
        try:
            proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            proc.terminate()
            _reply(message_id, t("feishu.auth_failed", error="timeout"), reply_in_thread=True)
            return

        if proc.returncode == 0:
            _reply(message_id, t("feishu.auth_success"), reply_in_thread=True)
            logger.info("Auth recovery: device-auth succeeded, restarting agent")
            # Restart the agent with original prompt
            _start_agent(prompt, message_id, image_path)
        else:
            _reply(message_id, t("feishu.auth_failed", error=f"exit code {proc.returncode}"), reply_in_thread=True)

    except Exception as e:
        logger.error(f"Auth recovery failed: {e}")
        _reply(message_id, t("feishu.auth_failed", error=str(e)), reply_in_thread=True)


def _start_agent(prompt: str, message_id: str, image_path: str | None = None):
    """Start an agent instance in a tmux session, triggered from Feishu."""
    cwd = config.default_cwd
    os.makedirs(cwd, exist_ok=True)
    tmux_name = f"walkcode-{int(time.time())}"
    env_exports = agent_adapter.build_env_exports()
    cmd = f"{env_exports}{agent_adapter.build_start_cmd(prompt, cwd, image_path)}"

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, cmd],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.error(f"tmux new-session failed: {result.stderr.strip()}")
            _reply(message_id, t("feishu.start_failed", error=result.stderr.strip()), reply_in_thread=True)
            return
    except Exception as e:
        logger.error(f"Start {agent_adapter.name} failed: {e}")
        _reply(message_id, t("feishu.start_failed", error=e), reply_in_thread=True)
        return

    session_store.add_pending(tmux_name, message_id)
    reply_id = _reply(message_id, t("feishu.started", agent=agent_adapter.name.title(), tmux=tmux_name), reply_in_thread=True)
    if reply_id:
        session_store.update_pending_reply(tmux_name, reply_id)
    logger.info(f"Started {agent_adapter.name}: tmux={tmux_name} cwd={cwd} prompt={prompt[:50]}")

    # Background: detect auth failure and recover via device-auth
    if agent_adapter.device_auth_command:
        threading.Thread(
            target=_auth_recovery_check,
            args=(tmux_name, prompt, message_id, image_path),
            daemon=True,
        ).start()


def _resume_agent(session_id: str, old_session: Session, reply_text: str, message_id: str):
    """Resume a dead agent session in a new tmux, reusing the Feishu thread."""
    # Double-instance guard: only resume if the old tmux is really gone. If it is
    # still alive with an agent running, the "dead" verdict was wrong (e.g. a tty
    # mapping cleared by a nested child) and spawning a fresh `resume` would create
    # a SECOND instance writing the same session rollout. Deliver into the live
    # window instead — which is what the caller actually wanted.
    if old_session.tty and validate_target(old_session.tty) is None and is_agent_alive(old_session.tty):
        logger.warning(
            "Resume aborted: session=%s tmux=%s still alive; injecting instead of double-resuming",
            session_id[:8], old_session.tty,
        )
        if reply_text.strip():
            try:
                inject(old_session.tty, reply_text)
                logger.info(
                    "Resume-guard injected '%s' -> %s; tmux accepted",
                    reply_text[:50], old_session.tty,
                )
                _ack_inject_accepted(message_id)
                _register_pending_inject(session_id, old_session.tty, reply_text, message_id)
            except Exception as e:
                logger.error(f"Resume-guard inject failed: {e}")
                _reply(message_id, t("feishu.inject_timeout"), reply_in_thread=True)
                _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
        return
    cwd = old_session.cwd or config.default_cwd
    tmux_name = f"walkcode-{int(time.time())}"
    env_exports = agent_adapter.build_env_exports()
    cmd = f"{env_exports}{agent_adapter.build_resume_cmd(session_id, cwd)}"

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, cmd],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.error(f"tmux new-session for resume failed: {result.stderr.strip()}")
            _reply(message_id, t("feishu.resume_failed", error=result.stderr.strip()))
            return
    except Exception as e:
        logger.error(f"Resume {agent_adapter.name} failed: {e}")
        _reply(message_id, t("feishu.resume_failed", error=e))
        return

    session_store.upsert(session_id, tty=tmux_name, cwd=cwd, root_msg_id=old_session.root_msg_id)
    _reply(message_id, t("feishu.resumed", agent=agent_adapter.name.title(), tmux=tmux_name))
    logger.info(f"Resumed {agent_adapter.name}: session={session_id[:8]} tmux={tmux_name} cwd={cwd}")

    if reply_text.strip():
        def _delayed_inject():
            # Wait for the resumed TUI to finish replaying/re-rendering its
            # history (a 100%-context session can take a minute-plus) before
            # injecting. A fixed sleep raced the render: the paste landed but the
            # Enter was dropped, so the message was never submitted and got
            # reported as "not delivered". See wait_until_input_ready.
            ready = wait_until_input_ready(tmux_name)
            if not ready:
                logger.warning(
                    f"Resume {tmux_name}: TUI not confirmed input-ready within "
                    f"timeout; injecting anyway"
                )
            else:
                # The TUI settled at an idle prompt, so this inject goes into an
                # idle session — clear any stale busy state carried over from the
                # session that died mid-turn, so it isn't mislabeled "queued"
                # (and confirmation uses the normal idle grace).
                _mark_session_idle(session_id)
            try:
                inject(tmux_name, reply_text)
            except Exception as e:
                logger.error(f"Delayed inject after resume failed: {e}")
                _reply(message_id, t("feishu.inject_timeout"), reply_in_thread=True)
                _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
                return
            logger.info(
                "Delayed inject after resume '%s' -> %s; tmux accepted",
                reply_text[:50], tmux_name,
            )
            _ack_inject_accepted(message_id)
            _register_pending_inject(session_id, tmux_name, reply_text, message_id)
        threading.Thread(target=_delayed_inject, daemon=True).start()


def _find_askuser_awaiting_other(thread_root: str | None) -> str | None:
    """Return the rid whose AskUserQuestion awaits an Other thread-reply in this
    Feishu thread root, or None (delegates to the registry)."""
    return registry.find_awaiting_other(thread_root)


def _consume_other_answer(request_id: str, text: str, message_id: str):
    """Use `text` as the AskUserQuestion answer for the question currently
    awaiting Other input. Advances to next question or finalizes."""
    req_data = registry.get(request_id)
    if req_data is None:
        _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
        return
    questions = req_data.tool_input.get("questions", [])
    awaiting = req_data.awaiting_other or {}
    qi = awaiting.get("question_index", 0)
    total = len(questions)

    # record_answer extends the answers list and clears awaiting_other atomically
    answers_list = registry.askuser_record_answer(request_id, qi, text)
    if answers_list is None:
        _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
        return
    logger.info(f"AskUser other answer Q{qi+1}={text!r} (rid={request_id[:8]})")

    has_next = qi + 1 < total
    if has_next:
        # Send next question card as a fresh thread reply since we don't have a
        # CallBackCard channel for the original card.
        next_card = _build_askuserquestion_card(request_id, questions, qi + 1)
        root = req_data.feishu_root_msg_id
        if root:
            _reply_card(root, next_card, reply_in_thread=True)
        _add_reaction(message_id, random.choice(_SUCCESS_EMOJIS))
        return

    final_decision = {
        "behavior": "allow",
        "answers": answers_list,
        "updatedInput": _build_askuser_updated_input(questions, answers_list),
    }
    registry.set_decision_once(request_id, final_decision)
    _add_reaction(message_id, random.choice(_SUCCESS_EMOJIS))


def _norm(s: str) -> str:
    """Collapse whitespace for tolerant prompt matching."""
    return " ".join((s or "").split())


def _mark_session_busy(session_id: str):
    if session_id:
        with _pending_lock:
            _session_last_ups[session_id] = time.time()
            # Receiving any UserPromptSubmit proves this session has the hook
            # installed — so a *missing* one later is a real swallow, not just a
            # legacy session that predates the hook.
            _ups_capable_sessions.add(session_id)


def _mark_session_idle(session_id: str):
    if session_id:
        with _pending_lock:
            _session_last_stop[session_id] = time.time()
        # Turn ended → clear any stuck-turn alert state so the next turn that
        # wedges alerts fresh (covers turns that start and stall between scans).
        with _stuck_lock:
            _stuck_alerted.pop(session_id, None)


def _is_session_busy(session_id: str) -> bool:
    """A turn is in progress if the last UserPromptSubmit is newer than the last Stop."""
    if not session_id:
        return False
    with _pending_lock:
        return _session_last_ups.get(session_id, 0.0) > _session_last_stop.get(session_id, 0.0)


def _ack_inject_accepted(message_id: str):
    """Mark a Feishu reply as handed to tmux.

    This is the user-visible delivery boundary. Anything after this is Claude
    Code's own prompt queue / TUI state, not a WalkCode delivery failure.
    """
    try:
        _add_reaction(message_id, random.choice(_SUCCESS_EMOJIS))
    except Exception as e:
        logger.error(f"Failed to mark inject accepted: {e}")


def _register_pending_inject(session_id: str | None, tty: str, text: str, message_id: str):
    """Record an injected message for optional UserPromptSubmit observation.

    This state is intentionally not authoritative. tmux accepting the inject is
    the delivery boundary; missing or late UserPromptSubmit hooks must not create
    user-visible "queued" or "swallowed" notices.
    """
    with _pending_lock:
        _pending_injects.append({
            "session_id": session_id or "",
            "tty": tty,
            "text": text,
            "message_id": message_id,
            "injected_at": time.time(),
        })


def _confirm_pending_inject(session_id: str, tty: str, prompt: str):
    """Match UserPromptSubmit against an observed inject and log it."""
    np = _norm(prompt)
    if not np:
        return
    hit = None
    with _pending_lock:
        for p in _pending_injects:
            same = (session_id and p["session_id"] and p["session_id"] == session_id) or \
                   (tty and p["tty"] == tty)
            if not same:
                continue
            nt = _norm(p["text"])
            if nt and (nt in np or np in nt):
                hit = p
                break
        if hit:
            _pending_injects.remove(hit)
    if hit:
        sid = (session_id or "")[:8]
        logger.info(f"Inject observed by UserPromptSubmit: '{hit['text'][:40]}' session={sid or '-'} tty={tty}")


def _sweep_pending_injects():
    """Drop old inject observations.

    This cleanup is silent by design. Lack of a matching UserPromptSubmit is not
    a WalkCode delivery failure after tmux has accepted the input.
    """
    now = time.time()
    expired = []
    with _pending_lock:
        for p in list(_pending_injects):
            if (now - p["injected_at"]) > _INJECT_OBSERVATION_TTL:
                _pending_injects.remove(p)
                expired.append(p)
    for p in expired:
        logger.info(
            "Inject observation expired without UserPromptSubmit: '%s' tty=%s session=%s",
            p["text"][:40], p["tty"], (p["session_id"] or "-")[:8],
        )


def _start_inject_sweeper():
    """Background thread that cleans up inject observation entries."""
    def _loop():
        while True:
            time.sleep(_SWEEPER_INTERVAL)
            try:
                _sweep_pending_injects()
            except Exception as e:
                logger.error(f"Inject sweeper error: {e}")
    threading.Thread(target=_loop, daemon=True).start()
    logger.info("Inject observation sweeper started (ttl=%.0fs)", _INJECT_OBSERVATION_TTL)


def _on_message(data: P2ImMessageReceiveV1):
    # Dispatch to background worker so the SDK returns immediately and can ack
    # the WebSocket frame. Doing tmux/HTTP work in the SDK callback blocks the
    # event loop, misses PING/PONG heartbeats, and causes Feishu to redeliver
    # the same message after reconnect.
    _msg_executor.submit(_handle_message_safe, data)


def _handle_message_safe(data: P2ImMessageReceiveV1):
    try:
        _handle_message(data)
    except Exception:
        message_id = ""
        sender = ""
        try:
            message_id = data.event.message.message_id or ""
            sender = data.event.sender.sender_id.open_id or ""
        except Exception:
            pass
        logger.exception(
            "Unhandled error in _handle_message message_id=%s open_id=%s",
            message_id, sender,
        )


def _handle_message(data: P2ImMessageReceiveV1):
    sender_id = data.event.sender.sender_id

    if not config.feishu_receive_id:
        logger.info("Message from open_id=%s", sender_id.open_id)
        print(t("serve.received_open_id", open_id=sender_id.open_id))
        return

    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    logger.info(
        "Message from open_id=%s message_id=%s parent=%s root=%s",
        sender_id.open_id, message_id, parent_id or "-", root_id or "-",
    )

    # --- Parse message content early ---
    if msg.message_type not in ("text", "image", "post"):
        if parent_id or root_id:
            _reply(message_id, t("feishu.unsupported_type"))
        return

    text = _parse_message_content(msg, message_id)
    if not text:
        return

    # --- AskUserQuestion "Other" path: thread reply becomes the answer ---
    if (parent_id or root_id) and msg.message_type == "text":
        thread_root = root_id or parent_id
        rid = _find_askuser_awaiting_other(thread_root)
        if rid:
            _consume_other_answer(rid, text, message_id)
            return

    # --- New message: start a new agent instance ---
    if not parent_id and not root_id:
        # For agents with native --image flag (e.g. Codex), extract first image path
        image_path = None
        if agent_adapter.image_flag and msg.message_type == "image":
            try:
                content = json.loads(msg.content)
                image_key = content.get("image_key", "")
                if image_key:
                    image_path = _download_image(message_id, image_key)
                    text = ""  # prompt is just the image
            except Exception:
                pass
        _start_agent(text, message_id, image_path=image_path)
        return

    # --- Reply: route to existing session ---
    session_id = _resolve_session_id(msg)
    tty = None
    project = "?"

    if session_id:
        session, session_error = _load_reply_session(session_id)
        if session_error:
            if session:
                # tmux dead but session data exists → resume
                _resume_agent(session_id, session, text, message_id)
            else:
                _reply(message_id, t("feishu.session_expired"))
            return
        if not session:
            return
        tty = session.tty
        project = basename(session.cwd) if session.cwd else "?"
    else:
        # Check pending Feishu-initiated sessions (hook not yet received)
        _root = root_id or parent_id
        _tmux = session_store.resolve_pending_tty(_root) if _root else None
        if _tmux:
            error = validate_target(_tmux)
            if error:
                _reply(message_id, t("feishu.stale_session"))
                return
            tty = _tmux
            project = basename(config.default_cwd) if config.default_cwd else "?"
        else:
            logger.warning(
                "Reply to unknown thread message root=%s parent=%s (mapping lost or reply target never registered)",
                root_id or "-",
                parent_id or "-",
            )
            _reply(message_id, t("feishu.session_not_found"))
            return

    # Check if agent process is still running (not just tmux session alive)
    if not is_agent_alive(tty):
        logger.info(f"Agent not alive in {tty}, triggering resume")
        if session_id:
            session = session_store.get(session_id)
            if session:
                _resume_agent(session_id, session, text, message_id)
                return
        _reply(message_id, t("feishu.stale_session"))
        return

    try:
        inject(tty, text)
    except Exception as e:
        # send-keys timed out / session gone → immediate, honest failure
        logger.error(f"Inject failed: {e}")
        _reply(message_id, t("feishu.inject_timeout"), reply_in_thread=True)
        _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
        return
    # tmux accepted paste+Enter. From WalkCode's perspective this is delivered;
    # Claude Code owns whether the prompt runs immediately or waits behind a turn.
    logger.info(f"Injected '{text[:50]}' -> {tty} ({project}); tmux accepted")
    _ack_inject_accepted(message_id)
    _register_pending_inject(session_id, tty, text, message_id)


# --- FastAPI routes ---

def _hook_dedupe_key(session_id: str, hook_type: str, turn_id: str, message: str) -> tuple | None:
    """Build the dedupe key for a hook delivery, or None when dedupe doesn't apply.

    Tuple (not string concat) so attacker-/bug-controlled session_id or turn_id
    can't collide two distinct deliveries into one key. turn_id (codex) is exact;
    Claude has none, so fall back to a hash of the user-visible message.
    """
    if not session_id:
        return None
    if turn_id:
        return (session_id, hook_type, "turn", turn_id)
    digest = hashlib.sha256((message or "").encode("utf-8", "replace")).hexdigest()[:16]
    return (session_id, hook_type, "msg", digest)


def _hook_key_ttl(key: tuple) -> float:
    return _HOOK_DEDUPE_TTL_TURN if key[2] == "turn" else _HOOK_DEDUPE_TTL_HASH


def _hook_already_delivered(key: tuple) -> bool:
    """Read-only: True if this key was delivered within its TTL. Prunes expired.

    Does NOT register the key — registration happens only after a confirmed send
    (_hook_mark_delivered), so a failed first delivery leaves the slot open for
    codex's duplicate to retry rather than swallowing the turn entirely.
    """
    now = time.time()
    with _hook_dedupe_lock:
        for k in [k for k, ts in _recent_hook_keys.items() if now - ts > _hook_key_ttl(k)]:
            del _recent_hook_keys[k]
        return key in _recent_hook_keys


def _hook_mark_delivered(key: tuple) -> None:
    """Register a key as delivered — call only after the Feishu send succeeded."""
    with _hook_dedupe_lock:
        _recent_hook_keys[key] = time.time()


def _remember_delivery(dedupe_key: tuple | None, hook_type: str, session_id: str) -> None:
    """Mark a successful Feishu send for dedupe, with a log line sharing a key
    fingerprint with the 'Hook deduped' line so the two can be correlated."""
    if dedupe_key is None:
        return
    _hook_mark_delivered(dedupe_key)
    logger.info(
        f"Hook delivered: {hook_type} session={session_id[:8] or '-'} "
        f"key={dedupe_key[2]}:{dedupe_key[3][:12]}"
    )


@app.post("/hook")
async def receive_hook(request: Request):
    body = await request.json()
    hook_type = body.get("type", "unknown")
    tty = body.get("tty", "")
    cwd = body.get("cwd", "")
    matcher = body.get("matcher", "")
    # Normalize: a JSON null arrives as None (the "" default only applies when the
    # key is absent), which would break str slicing / hashing downstream.
    session_id = body.get("session_id") or ""
    turn_id = body.get("turn_id") or ""
    message = body.get("message") or ""
    title = body.get("title") or ""

    # Debug: Log all hook inputs to see elicitation_dialog structure
    logger.info(f"[DEBUG HOOK] type={hook_type} matcher={matcher}")
    logger.debug(f"[HOOK BODY] {json.dumps(body, indent=2, ensure_ascii=False)}")

    if not tty:
        return {"ok": False, "error": "missing tty (not in tmux?)"}

    # Stop = turn ended → session is idle. Mark idle BEFORE the dedupe gate so
    # busy/idle observability still sees every Stop; dedupe only suppresses the
    # duplicate user-facing message.
    if hook_type == "stop" and session_id:
        _mark_session_idle(session_id)

    # codex (>=0.135) fires each hook twice with an identical payload — a turn
    # ends once, so suppress the duplicate before it becomes a second Feishu reply.
    # Read-only check here; the key is registered only AFTER a successful send
    # below, so a failed first delivery still lets codex's duplicate through.
    dedupe_key = (
        _hook_dedupe_key(session_id, hook_type, turn_id, message)
        if hook_type in ("stop", "notification") else None
    )
    if dedupe_key is not None and _hook_already_delivered(dedupe_key):
        logger.info(
            f"Hook deduped: {hook_type} session={session_id[:8] or '-'} "
            f"key={dedupe_key[2]}:{dedupe_key[3][:12]}"
        )
        return {"ok": True, "deduped": True}

    effective_type = matcher or hook_type
    labels = _labels()
    label = labels.get(effective_type, "")
    if title and message:
        display_message = f"**{title}**\n{message}"
    elif label and message:
        display_message = f"{label}\n{message}"
    else:
        display_message = message or label or effective_type
    project = basename(cwd) if cwd else "unknown"
    logger.info(f"Hook: [{project}] {effective_type} | tmux={tty} session={session_id[:8] if session_id else '-'}")

    session = session_store.get(session_id) if session_id else None

    if session and session.root_msg_id:
        # Existing session: reply to thread root
        session_store.upsert(session_id, tty=tty, cwd=cwd)
        text = display_message
        need_subscribe = not session.subscribed and config.feishu_receive_id
        if need_subscribe:
            text = f'<at user_id="{config.feishu_receive_id}"></at> {text}'
        msg_id = _reply(session.root_msg_id, text=text, reply_in_thread=True)
        if msg_id:
            if need_subscribe:
                session_store.mark_subscribed(session_id)
            # Register dedupe ONLY now that the send succeeded (F1: a failed
            # _reply must leave the slot open for codex's duplicate to retry).
            _remember_delivery(dedupe_key, hook_type, session_id)
            return {"ok": True, "msg_id": msg_id, "thread": session.root_msg_id}
    else:
        # New session: check if Feishu-initiated (pending root exists)
        pending_root, reply_id = session_store.pop_pending(tty)
        if pending_root:
            # Feishu-initiated: reuse existing thread
            root_id = pending_root
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
                # Update the launch reply with session info
                if reply_id:
                    _edit_message(reply_id, t("feishu.started_with_session", agent=agent_adapter.name.title(), session_id=session_id[:8], tmux=tty))
            text = display_message
            if config.feishu_receive_id:
                text = f'<at user_id="{config.feishu_receive_id}"></at> {text}'
            msg_id = _reply(root_id, text=text, reply_in_thread=True)
            if msg_id:
                if session_id:
                    session_store.mark_subscribed(session_id)
                _remember_delivery(dedupe_key, hook_type, session_id)
                return {"ok": True, "msg_id": root_id}
            # E: reply to the pending root failed — do NOT fall through to creating
            # a new thread (the first reply would land in the wrong place). The
            # session was upserted with root_msg_id above, so codex's duplicate
            # retries into the same thread via the existing-session branch.
            return {"ok": False, "error": "reply to pending root failed"}

        # User-initiated: send title as thread root, reply with content
        thread_title = _make_title(cwd, session_id, message)
        root_id = _send(text=thread_title)
        if root_id:
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            text = display_message
            if config.feishu_receive_id:
                text = f'<at user_id="{config.feishu_receive_id}"></at> {text}'
            msg_id = _reply(root_id, text=text, reply_in_thread=True)
            if msg_id:
                if session_id:
                    session_store.mark_subscribed(session_id)
                # Register dedupe only on a confirmed send (F1 consistency). The
                # thread root + session.root_msg_id persist, so a failed content
                # reply is retried by codex's duplicate via the existing-session path.
                _remember_delivery(dedupe_key, hook_type, session_id)
                return {"ok": True, "msg_id": root_id}

    return {"ok": False, "error": "send failed"}


@app.post("/hook/sync")
async def receive_sync_hook(request: Request):
    """Lightweight tty mapping update — called by SessionStart hook."""
    body = await request.json()
    tty = body.get("tty", "")
    session_id = body.get("session_id", "")
    cwd = body.get("cwd", "")

    if not tty or not session_id:
        return {"ok": False, "error": "missing tty or session_id"}

    session = session_store.get(session_id)
    if session and session.root_msg_id:
        session_store.upsert(session_id, tty=tty, cwd=cwd)
        logger.info(f"Sync: session={session_id[:8]} tty={tty} (updated)")
    else:
        # New session or no Feishu thread yet — store tty+cwd so first hook finds it
        session_store.upsert(session_id, tty=tty, cwd=cwd)
        logger.info(f"Sync: session={session_id[:8]} tty={tty} (new, no thread yet)")

    return {"ok": True}


@app.post("/hook/prompt")
async def receive_prompt_hook(request: Request):
    """UserPromptSubmit hook — records prompts accepted by Claude.

    Used for per-session busy state and best-effort inject observation. It does
    not decide whether a Feishu reply was delivered; tmux acceptance does.
    """
    body = await request.json()
    tty = body.get("tty", "")
    session_id = body.get("session_id", "")
    prompt = body.get("prompt", "")
    if session_id:
        _mark_session_busy(session_id)
    _confirm_pending_inject(session_id, tty, prompt)
    return {"ok": True}


def _perm_dedupe_key(session_id: str, tool_use_id: str) -> tuple | None:
    """Dedupe key for a permission request. tool_use_id identifies ONE tool call
    (turn_id would wrongly merge a whole turn's requests). None → not deduped
    (e.g. AskUserQuestion, which is Claude-only and carries no tool_use_id)."""
    if not session_id or not tool_use_id:
        return None
    return (session_id, tool_use_id)


@app.post("/hook/permission")
async def receive_permission_hook(request: Request):
    """Receive a PermissionRequest hook, send Feishu card, return request_id."""
    body = await request.json()
    tty = body.get("tty", "")
    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")
    tool_name = body.get("tool_name", "")
    tool_input = body.get("tool_input", {})
    hook_data_full = body.get("hook_data_full", {})

    if not tty:
        return {"ok": False, "error": "missing tty"}

    # DEBUG: log full hook data for analysis
    if hook_data_full:
        extra_keys = sorted(set(hook_data_full.keys()) - {"tool_name", "tool_input", "cwd", "session_id"})
        logger.info(f"[HOOK_DEBUG] tool={tool_name} extra_keys={extra_keys}")
        if hook_data_full.get("permission_suggestions"):
            logger.info(f"[HOOK_DEBUG] permission_suggestions={json.dumps(hook_data_full['permission_suggestions'], ensure_ascii=False)}")
        for k in extra_keys:
            if k != "permission_suggestions":
                logger.info(f"[HOOK_DEBUG] {k}={json.dumps(hook_data_full.get(k), ensure_ascii=False)}")

    perm_suggestions = hook_data_full.get("permission_suggestions", [])

    # Dedupe codex 0.135's double-fired PreToolUse by tool_use_id: the duplicate
    # reuses the first rid (no second card), and both hook processes long-poll the
    # same decision. AskUserQuestion has no tool_use_id → key None → not deduped.
    tool_use_id = body.get("tool_use_id") or hook_data_full.get("tool_use_id") or ""
    dedupe_key = _perm_dedupe_key(session_id, tool_use_id)

    # Two passes at most: a duplicate that finds the first sender's card FAILED
    # takes over as the sender (register_or_get released the key on card_failed).
    # With synchronous lark IO the first sender always finishes before the
    # duplicate runs, so await_send_result returns immediately.
    for _attempt in (0, 1):
        req, is_new = registry.register_or_get(dedupe_key)
        request_id = req.rid

        if not is_new:
            status = registry.await_send_result(request_id)
            if status is CardStatus.READY:
                logger.info(f"Permission dedupe: reuse rid={request_id[:8]} tool={tool_name} (codex double-fire)")
                return {"ok": True, "request_id": request_id, "deduped": True}
            # The first sender's card FAILED and its key is released → loop and
            # re-register as the new sender.
            continue

        # We are the sender. Fill request-side fields (feishu_root_msg_id is set
        # after the thread root is known, so AskUserQuestion "Other" can locate us).
        registry.fill_request(
            request_id,
            tool_name=tool_name, tool_input=tool_input, tty=tty,
            hook_data_full=hook_data_full, permission_suggestions=perm_suggestions,
            feishu_root_msg_id="",
        )

        # Find the Feishu thread to reply in
        session = session_store.get(session_id) if session_id else None
        root_msg_id = None
        if session and session.root_msg_id:
            root_msg_id = session.root_msg_id
            session_store.upsert(session_id, tty=tty, cwd=cwd)
        else:
            pending_root, reply_id = session_store.pop_pending(tty)
            if pending_root:
                root_msg_id = pending_root
                if session_id:
                    session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_msg_id)
                    if reply_id:
                        _edit_message(reply_id, t("feishu.started_with_session", agent=agent_adapter.name.title(), session_id=session_id[:8], tmux=tty))

        # Generate appropriate card based on tool type and permission_suggestions
        permission_mode = hook_data_full.get("permission_mode", "")
        if tool_name == "AskUserQuestion":
            questions = tool_input.get("questions", [])
            card = _build_askuserquestion_card(request_id, questions)
        else:
            if permission_mode == "plan" and not perm_suggestions:
                perm_type = "plan"
            elif perm_suggestions and perm_suggestions[0].get("type") == "setMode":
                perm_type = "setMode"
            else:
                perm_type = "addRules"
            logger.info(f"[PERM_DEBUG] perm_type={perm_type} suggestions={len(perm_suggestions)}")
            card = _build_permission_card(request_id, perm_type, tool_name, tool_input, perm_suggestions)

        if root_msg_id:
            logger.info(f"[CARD_DEBUG] Replying card in thread root_msg_id={root_msg_id} for {tool_name}")
            card_msg_id = _reply_card(root_msg_id, card, reply_in_thread=True)
        else:
            logger.info(f"[CARD_DEBUG] Sending card as root message for {tool_name} (no root_msg_id, session={session_id[:8] if session_id else 'none'}, tty={tty})")
            card_msg_id = _send_card(card)

        if not card_msg_id:
            # The card never reached Feishu. card_failed releases the dedupe slot so
            # codex's duplicate comes through as is_new and re-sends, instead of
            # being deduped onto a card nobody can see (which would strand both
            # hooks until timeout). This sender's own client fails open.
            registry.card_failed(request_id)
            logger.error(f"Permission card send failed, released rid={request_id[:8]} tool={tool_name}")
            return {"ok": False, "error": "card send failed"}

        if root_msg_id:
            registry.fill_request(request_id, feishu_root_msg_id=root_msg_id)
        registry.card_sent(request_id)

        project = basename(cwd) if cwd else "unknown"
        logger.info(f"Permission request: {tool_name} | rid={request_id[:8]} tmux={tty} ({project})")
        return {"ok": True, "request_id": request_id}

    return {"ok": False, "error": "card send failed after retry"}


@app.get("/hook/permission/{request_id}/decision")
async def get_permission_decision(request_id: str):
    """Long-poll for a permission decision (up to 30s per call)."""
    req = registry.get(request_id)
    if req is None:
        return {"status": "not_found"}

    # Record liveness: a still-polling hook keeps its rid alive against the TTL GC,
    # so a slow user's valid permission card is never reaped out from under them.
    registry.mark_poll(request_id)

    loop = asyncio.get_event_loop()
    decided = await loop.run_in_executor(None, req.decided.wait, 30)

    if decided:
        # try_consume reads (not pops) the decision so codex's two pollers both get
        # the SAME verdict; it stamps consumed_at (lazy GC reaps after GRACE) and is
        # mutually exclusive with the tmux fallback's claim.
        decision = registry.try_consume(request_id)
        if decision is not None:
            return {"status": "decided", "decision": decision}

    return {"status": "pending"}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": session_store.count()}


# --- Idle reaper ---

def _reap_idle_sessions():
    """Check all tracked sessions and kill idle tmux sessions.

    Only Feishu-initiated sessions (tmux name starts with "walkcode-") are
    reaped.  Locally-initiated sessions (e.g. "claude-project-12345") are
    left alone — the user opened them on their own machine and may simply
    be away; killing them silently would be a bad experience.
    """
    now = time.time()
    for session_id, session in session_store.items():
        if not session.tty:
            continue
        # Skip locally-initiated sessions — only reap Feishu-initiated ones
        if not session.tty.startswith("walkcode-"):
            continue
        activity = get_session_activity(session.tty)
        if activity is None:
            # tmux session doesn't exist (already dead), skip
            continue
        idle_seconds = now - activity
        if idle_seconds > _IDLE_TIMEOUT:
            logger.info(f"Reaping idle session {session_id[:8]} tmux={session.tty} idle={idle_seconds:.0f}s")
            kill_session(session.tty)
            if session.root_msg_id:
                try:
                    _reply(
                        session.root_msg_id,
                        t("feishu.idle_killed"),
                        reply_in_thread=True,
                    )
                except Exception as e:
                    logger.error(f"Failed to notify idle kill: {e}")


def _start_idle_reaper():
    """Start a background thread that periodically kills idle tmux sessions."""
    def _loop():
        while True:
            time.sleep(_REAPER_INTERVAL)
            try:
                _reap_idle_sessions()
            except Exception as e:
                logger.error(f"Idle reaper error: {e}")

    reaper = threading.Thread(target=_loop, daemon=True)
    reaper.start()
    logger.info("Idle reaper started (timeout=%ds, interval=%ds)", _IDLE_TIMEOUT, _REAPER_INTERVAL)


# --- Stuck-turn watchdog ---
# A turn that runs very long with no Stop is usually wedged on an interactive step
# the agent can't get past (the incident: codex sat 6.5h on a Playwright Google
# login that never returned). There is no "still working" hook (codex has no
# UserPromptSubmit hook at all), so detect it from the TUI: both Claude and Codex
# render an "(… esc to interrupt)" footer with an elapsed timer while a turn runs.
# Parse that elapsed time; once it crosses the threshold, warn on the Feishu
# thread so a human can attach and unstick it.
_INTERRUPT_TIME_RE = re.compile(
    r"\((?:(\d+)h\s+)?(?:(\d+)m\s+)?(\d+)s\b[^)]*esc to interrupt",
    re.IGNORECASE,
)
# session_id → {"alerted": bool, "last_secs": int}. One warning per stuck turn:
# a turn is "new" when the elapsed timer drops below last_secs (it restarts each
# turn); idle/dead clears the entry; the flag flips only after a confirmed send.
_stuck_alerted: dict[str, dict] = {}
_stuck_lock = threading.Lock()


# How many trailing non-blank pane lines count as the live footer. The active
# "esc to interrupt" timer sits on the bottom line(s); scanning the whole capture
# would match a stale timer left in scrollback and read an idle pane as busy.
_FOOTER_LINES = 6


def _parse_working_seconds(pane: str) -> int | None:
    """Elapsed seconds of the *current* turn from the live TUI footer, or None.

    Both Claude and Codex render an "(Xh Ym Zs … esc to interrupt)" timer on the
    bottom line while a turn runs. Only the last few non-blank lines are scanned,
    so a stale timer further up in scrollback isn't mistaken for the current turn.
    """
    lines = [ln for ln in pane.splitlines() if ln.strip()]
    for ln in reversed(lines[-_FOOTER_LINES:]):
        m = _INTERRUPT_TIME_RE.search(ln)
        if m:
            return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3))
    return None


def _check_stuck_sessions():
    """Warn on the Feishu thread for any turn stuck past _STUCK_THRESHOLD.

    One warning per stuck turn. Per-session state is {"alerted", "last_secs"}: a
    new turn is detected when the elapsed timer drops below last_secs (it restarts
    each turn), and idle/dead clears the entry. The alert flag flips only after a
    confirmed Feishu send, so a failed send is retried on the next scan instead of
    being silently swallowed.
    """
    for session_id, session in session_store.items():
        # Only Feishu-bound, walkcode-managed, live sessions are watched.
        if not session.root_msg_id or not session.tty:
            continue
        if not session.tty.startswith("walkcode-"):
            continue
        if validate_target(session.tty) is not None or not is_agent_alive(session.tty):
            with _stuck_lock:
                _stuck_alerted.pop(session_id, None)
            continue
        pane = capture_pane(session.tty, lines=40)
        if not pane.strip():
            # Capture failed / empty (a tmux hiccup) — NOT proof of idle. Keep the
            # alert state so one transient failure doesn't reset dedup and fire a
            # duplicate alert next scan. Skip this round.
            continue
        secs = _parse_working_seconds(pane)
        if secs is None:
            # Readable pane with no live timer → genuinely idle / between turns;
            # reset so the next stuck turn alerts fresh.
            with _stuck_lock:
                _stuck_alerted.pop(session_id, None)
            continue
        with _stuck_lock:
            st = _stuck_alerted.get(session_id)
            if st is not None and secs < st["last_secs"]:
                st = None  # timer went backwards → a new turn started; reset
            if st is None:
                st = {"alerted": False, "last_secs": secs}
                _stuck_alerted[session_id] = st
            st["last_secs"] = secs
            already = st["alerted"]
        if secs < _STUCK_THRESHOLD or already:
            continue
        msg_id = None
        try:
            msg_id = _reply(
                session.root_msg_id,
                t("feishu.stuck_warning", minutes=secs // 60, tmux=session.tty),
                reply_in_thread=True,
            )
        except Exception as e:
            logger.error(f"Stuck watchdog alert failed: {e}")
        if msg_id:
            with _stuck_lock:
                cur = _stuck_alerted.get(session_id)
                if cur is not None:
                    cur["alerted"] = True
            logger.warning(
                "Stuck watchdog: session=%s tmux=%s working=%ds — warned",
                session_id[:8], session.tty, secs,
            )
        else:
            logger.error(
                "Stuck watchdog: alert NOT delivered for session=%s tmux=%s working=%ds; will retry",
                session_id[:8], session.tty, secs,
            )


def _start_stuck_watchdog():
    """Background thread that warns about turns wedged past _STUCK_THRESHOLD."""
    def _loop():
        while True:
            time.sleep(_WATCHDOG_INTERVAL)
            try:
                _check_stuck_sessions()
            except Exception as e:
                logger.error(f"Stuck watchdog error: {e}")

    threading.Thread(target=_loop, daemon=True).start()
    logger.info(
        "Stuck watchdog started (threshold=%ds, interval=%ds)",
        _STUCK_THRESHOLD, _WATCHDOG_INTERVAL,
    )


# --- Init ---

def init(cfg: Config):
    global config, lark_client, session_store, agent_adapter
    config = cfg
    agent_adapter = get_agent(cfg.agent)
    lark_client = lark.Client.builder() \
        .app_id(cfg.feishu_app_id) \
        .app_secret(cfg.feishu_app_secret) \
        .domain(cfg.openapi_domain) \
        .log_level(lark.LogLevel.INFO) \
        .build()
    session_store = SessionStore(cfg.state_path)
    session_store.load()
    logger.info("Using OpenAPI domain: %s", cfg.openapi_domain)
    logger.info("Loaded %s persisted sessions from %s", session_store.count(), cfg.state_path)


def start_ws_client(cfg: Config):
    handler = lark.EventDispatcherHandler.builder(
        "", ""
    ).register_p2_im_message_receive_v1(
        _on_message
    ).register_p2_card_action_trigger(
        _on_card_action
    ).build()

    cli = lark.ws.Client(
        cfg.feishu_app_id, cfg.feishu_app_secret,
        event_handler=handler, log_level=lark.LogLevel.INFO,
        domain=cfg.openapi_domain,
    )
    threading.Thread(target=cli.start, daemon=True).start()
    logger.info("Feishu WebSocket client started")

    _start_idle_reaper()
    _start_stuck_watchdog()
    _start_inject_sweeper()
