"""WalkCode server: FastAPI for hooks + Feishu WebSocket for events."""

import asyncio
import json
import logging
import os
import random
import re
import subprocess
import threading
import time
import uuid
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
from .state import Session, SessionStore
from .tty import inject, validate_target, get_session_activity, kill_session, capture_pane

logger = logging.getLogger("walkcode")

app = FastAPI(title="WalkCode", version=pkg_version("walkcode"))

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
agent_adapter: AgentAdapter = None  # type: ignore
_IDLE_TIMEOUT = 7200  # 2h — kill tmux sessions idle longer than this
_REAPER_INTERVAL = 600  # 10min — how often the idle reaper runs


# --- Permission request state ---

_perm_requests: dict[str, dict] = {}   # request_id → {tool_name, tool_input, ...}
_perm_decisions: dict[str, dict] = {}  # request_id → {behavior, tool_name, always}
_perm_events: dict[str, threading.Event] = {}  # request_id → Event for signaling


def _capture_terminal_options(tty: str) -> list[str]:
    """Capture numbered option text from the terminal's permission prompt via tmux.

    Parses from the bottom up to find the last contiguous block of numbered
    lines, then takes only the final "restart-at-1" subsequence.  This avoids
    false-matching numbered lines in user input, plan content, or tool output.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", tty, "-p", "-S", "-15"],
            capture_output=True, text=True, timeout=5,
        )
        # Collect last contiguous block of numbered lines (bottom-up)
        items: list[tuple[int, str]] = []  # (number, text)
        for line in reversed(result.stdout.split("\n")):
            m = re.match(r'\s*[❯>]?\s*(\d+)\.\s+(.+)', line)
            if m:
                items.insert(0, (int(m.group(1)), m.group(2).strip()))
            elif items:
                break
        if not items:
            return []
        # Permission options always start at 1; if plan content (1..N) is
        # contiguous with options (1..3), find the last restart at 1.
        last_restart = 0
        for i in range(len(items) - 1, -1, -1):
            if items[i][0] == 1:
                last_restart = i
                break
        return [text for _, text in items[last_restart:]]
    except Exception as e:
        logger.warning(f"Failed to capture terminal options from {tty}: {e}")
        return []


# --- Unified permission card builder ---
# Each perm_type defines: behavior values per button position, fallback labels, header, template.

_PERM_BEHAVIORS = {
    "plan":     ["plan_auto_accept", "plan_manual_approve", "deny"],
    "setMode":  ["allow", "accept_edits", "deny"],
    "addRules": ["allow", "always_allow", "deny"],
}
_PERM_FALLBACK_OPTIONS = {
    "plan":     ["Yes, auto-accept edits", "Yes, manually approve edits", "Tell Claude what to change"],
    "setMode":  ["Yes", "Yes, auto-accept edits", "No"],
    "addRules": ["Allow", "Always Allow", "Deny"],
}
_PERM_BUTTON_TYPES = ["primary", "default", "danger"]


def _build_dynamic_permission_card(
    request_id: str, perm_type: str, tool_name: str,
    tool_input: dict, terminal_options: list[str],
) -> dict:
    """Build a Feishu permission card with buttons matching the terminal's actual options.

    Works for all permission types: addRules, setMode, and plan approval.
    Button text is captured from the terminal via tmux; falls back to hardcoded defaults.
    """
    behaviors = _PERM_BEHAVIORS.get(perm_type, _PERM_BEHAVIORS["addRules"])
    fallbacks = _PERM_FALLBACK_OPTIONS.get(perm_type, _PERM_FALLBACK_OPTIONS["addRules"])
    opts = terminal_options if terminal_options and len(terminal_options) >= 3 else fallbacks

    buttons = []
    for i, (behavior, btn_type) in enumerate(zip(behaviors, _PERM_BUTTON_TYPES)):
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": opts[i]},
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


def _build_askuserquestion_card(request_id: str, questions: list, question_index: int = 0) -> dict:
    """Build a Feishu interactive card for AskUserQuestion with dynamic option buttons.

    Supports multiple questions via sequential processing (Method A):
    - Display current question with its options
    - User clicks answer → next question card appears
    - After last question, all answers are returned
    """
    if not questions or len(questions) == 0:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "Question"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "No questions found"}},
            ],
        }

    if question_index >= len(questions):
        question_index = 0

    question_obj = questions[question_index]
    question_text = question_obj.get("question", "Choose an option:")
    options = question_obj.get("options", [])

    # Add progress indicator for multiple questions
    total_questions = len(questions)
    progress_text = f"({question_index + 1}/{total_questions})" if total_questions > 1 else ""
    full_title = f"{question_text} {progress_text}".strip()

    # Build action buttons from options
    buttons = []
    for opt in options:
        buttons.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": opt.get("label", opt.get("value", ""))},
            "type": "primary",
            "value": {
                "rid": request_id,
                "answer": opt.get("label", opt.get("value", "")),
                "question_index": question_index,  # Track which question this answer is for
                "total_questions": total_questions,
            },
        })

    # Handle empty options case
    if not buttons:
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": full_title},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "⚠️ No options available for this question",
                    },
                },
            ],
        }

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": full_title},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "action",
                "actions": buttons,
            },
        ],
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
        return f"![{t('image.label', n=1)}]({path})"

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
                        line_parts.append(f"![{t('image.label', n=img_counter)}]({path})")
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


def _send(text: str) -> str | None:
    if not config.feishu_receive_id:
        logger.warning("Cannot send: FEISHU_RECEIVE_ID not configured")
        return None
    msg_type = "text"
    content = json.dumps({"text": text})
    body = CreateMessageRequestBody.builder() \
        .receive_id(config.feishu_receive_id) \
        .msg_type(msg_type) \
        .content(content) \
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
    msg_type = "text"
    content = json.dumps({"text": text})
    builder = ReplyMessageRequestBody.builder() \
        .msg_type(msg_type) \
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
        logger.error(f"Reply failed: {resp.code} {resp.msg}")
        return None
    return resp.data.message_id


def _edit_message(message_id: str, text: str):
    body = PatchMessageRequestBody.builder() \
        .content(json.dumps({"text": text})) \
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
        inject(tty, key, enter=True)
        logger.info(f"tmux fallback: sent '{key}' to {tty} for {tool_name} behavior={behavior} (rid={request_id[:8]})")
    except Exception as e:
        logger.error(f"tmux fallback failed: {e} (rid={request_id[:8]})")


def _tmux_inject_askuser(request_id: str, answers: list, req_data: dict):
    """Inject AskUserQuestion answer via tmux.

    The hook process exits with code 2 (no JSON), so Claude falls back to
    the interactive terminal.  We then:
      1. approve the permission prompt  (send "1" + Enter)
      2. send the answer option number  (send N + Enter)
    """
    tty = req_data.get("tty", "")
    if not tty:
        return
    tool_input = req_data.get("tool_input", {})
    questions = tool_input.get("questions", [])

    def _do_inject():
        # Wait for hook to exit(2) and Claude to show the question UI.
        # Claude auto-allows AskUserQuestion after hook denial, no separate
        # permission prompt is shown — we only need to inject the answer.
        time.sleep(4)

        error = validate_target(tty)
        if error:
            logger.warning(f"tmux inject AskUser: session {tty} not found (rid={request_id[:8]})")
            return

        for i, answer in enumerate(answers):
            if i < len(questions):
                options = questions[i].get("options", [])
                option_idx = None
                for j, opt in enumerate(options):
                    if opt.get("label", opt.get("value", "")) == answer:
                        option_idx = j + 1  # 1-based
                        break
                if option_idx is not None:
                    try:
                        inject(tty, str(option_idx), enter=True)
                        time.sleep(1)
                        logger.info(f"tmux inject AskUser: sent '{option_idx}' to {tty} for Q{i+1} answer={answer} (rid={request_id[:8]})")
                    except Exception as e:
                        logger.error(f"tmux inject AskUser answer failed: {e} (rid={request_id[:8]})")

    threading.Thread(target=_do_inject, daemon=True).start()


def _schedule_tmux_fallback(request_id: str, behavior: str, req_data: dict):
    """Schedule tmux fallback: wait 5s, if decision not consumed by hook, send keys."""
    def _check():
        time.sleep(5)
        if request_id in _perm_decisions:
            logger.info(f"Hook timed out, tmux fallback (rid={request_id[:8]})")
            _tmux_fallback(request_id, behavior, req_data)
            _perm_decisions.pop(request_id, None)
            _perm_events.pop(request_id, None)
            _perm_requests.pop(request_id, None)
    threading.Thread(target=_check, daemon=True).start()


def _schedule_tmux_fallback_askuser(request_id: str, answers: list, req_data: dict):
    """Legacy fallback — kept for reference, no longer called."""
    pass


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

        req_data = _perm_requests.get(request_id)
        if not req_data:
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = t("feishu.perm.expired")
            return resp

        tool_name = req_data.get("tool_name", "unknown")

        # Handle AskUserQuestion answers (supports multiple questions via sequential processing)
        if tool_name == "AskUserQuestion":
            answer = value.get("answer")
            question_index = value.get("question_index", 0)
            total_questions = value.get("total_questions", 1)

            if answer is None:
                resp.toast = CallBackToast()
                resp.toast.type = "warning"
                resp.toast.content = "No answer provided"
                return resp

            # Get tool input to access all questions
            tool_input = req_data.get("tool_input", {})
            questions = tool_input.get("questions", [])

            # Initialize answers storage if needed
            if not hasattr(_perm_decisions.get(request_id, {}), "get"):
                _perm_decisions[request_id] = {}
            current_decision = _perm_decisions.get(request_id, {})

            # Store this answer
            if "answers" not in current_decision:
                current_decision["answers"] = []
            # Ensure answers list is long enough
            while len(current_decision["answers"]) <= question_index:
                current_decision["answers"].append(None)
            current_decision["answers"][question_index] = answer

            logger.info(f"AskUserQuestion answer[{question_index}]: {answer} (rid={request_id[:8]})")

            # Check if there are more questions
            has_next_question = question_index + 1 < total_questions

            if has_next_question:
                # Build card for next question
                next_index = question_index + 1
                next_card = _build_askuserquestion_card(request_id, questions, next_index)

                # Return updated card to show next question
                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = next_card

                # Toast notification
                resp.toast = CallBackToast()
                resp.toast.type = "success"
                resp.toast.content = f"Question {question_index + 1}/{total_questions} answered. Next question..."
            else:
                # All questions answered - prepare final response
                final_decision = {
                    "behavior": "allow",
                    "answers": current_decision["answers"],
                }
                _perm_decisions[request_id] = final_decision

                # Signal that all answers are collected
                perm_event = _perm_events.get(request_id)
                if perm_event:
                    perm_event.set()

                # Schedule tmux injection: approve permission + send answer
                _tmux_inject_askuser(request_id, list(current_decision["answers"]), dict(req_data))

                # Build completion card
                result_card = {
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": "All questions answered"},
                        "template": "green",
                    },
                    "elements": [
                        {"tag": "div", "text": {"tag": "lark_md", "content": f"✓ All {total_questions} question(s) answered successfully"}},
                    ],
                }

                resp.card = CallBackCard()
                resp.card.type = "raw"
                resp.card.data = result_card

                # Toast notification
                resp.toast = CallBackToast()
                resp.toast.type = "success"
                resp.toast.content = f"All answers submitted"

            return resp

        # Handle permission decisions
        behavior = value.get("b", "")
        if not behavior:
            return resp

        decision_behavior = "allow" if behavior in ("allow", "always_allow", "accept_edits", "plan_auto_accept", "plan_manual_approve") else "deny"
        perm_suggestions = req_data.get("permission_suggestions", [])

        # Store decision and signal the waiting hook process
        decision_dict = {
            "behavior": decision_behavior,
        }
        # Include updatedPermissions using original permission_suggestions from Claude Code
        if behavior == "always_allow":
            decision_dict["updatedPermissions"] = perm_suggestions if perm_suggestions else [{"type": "addRules", "rules": [{"toolName": tool_name}], "behavior": "allow", "destination": "localSettings"}]
        elif behavior == "accept_edits":
            decision_dict["updatedPermissions"] = perm_suggestions if perm_suggestions else [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]
        elif behavior == "plan_auto_accept":
            decision_dict["updatedPermissions"] = [{"type": "setMode", "mode": "acceptEdits", "destination": "session"}]

        _perm_decisions[request_id] = decision_dict
        perm_event = _perm_events.get(request_id)
        if perm_event:
            perm_event.set()

        # Schedule tmux fallback in case hook already timed out
        _schedule_tmux_fallback(request_id, behavior, dict(req_data))

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
    reply_id = _reply(message_id, t("feishu.started", tmux=tmux_name), reply_in_thread=True)
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
    _reply(message_id, t("feishu.resumed", tmux=tmux_name))
    logger.info(f"Resumed {agent_adapter.name}: session={session_id[:8]} tmux={tmux_name} cwd={cwd}")

    if reply_text.strip():
        def _delayed_inject():
            time.sleep(3)
            try:
                inject(tmux_name, reply_text)
                _add_reaction(message_id, random.choice(_SUCCESS_EMOJIS))
            except Exception as e:
                logger.error(f"Delayed inject after resume failed: {e}")
                _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))
        threading.Thread(target=_delayed_inject, daemon=True).start()


def _on_message(data: P2ImMessageReceiveV1):
    sender_id = data.event.sender.sender_id
    logger.info("Message from open_id=%s", sender_id.open_id)

    if not config.feishu_receive_id:
        print(t("serve.received_open_id", open_id=sender_id.open_id))
        return

    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    # --- Parse message content early ---
    if msg.message_type not in ("text", "image", "post"):
        if parent_id or root_id:
            _reply(message_id, t("feishu.unsupported_type"))
        return

    text = _parse_message_content(msg, message_id)
    if not text:
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

    try:
        inject(tty, text)
        logger.info(f"Injected '{text}' -> {tty} ({project})")
        _add_reaction(message_id, random.choice(_SUCCESS_EMOJIS))
    except Exception as e:
        logger.error(f"Inject failed: {e}")
        _add_reaction(message_id, random.choice(_FAILURE_EMOJIS))


# --- FastAPI routes ---

@app.post("/hook")
async def receive_hook(request: Request):
    body = await request.json()
    hook_type = body.get("type", "unknown")
    tty = body.get("tty", "")
    cwd = body.get("cwd", "")
    matcher = body.get("matcher", "")
    session_id = body.get("session_id", "")
    message = body.get("message", "")
    title = body.get("title", "")

    # Debug: Log all hook inputs to see elicitation_dialog structure
    logger.info(f"[DEBUG HOOK] type={hook_type} matcher={matcher}")
    logger.debug(f"[HOOK BODY] {json.dumps(body, indent=2, ensure_ascii=False)}")

    if not tty:
        return {"ok": False, "error": "missing tty (not in tmux?)"}

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
                    _edit_message(reply_id, t("feishu.started_with_session", session_id=session_id[:8], tmux=tty))
            text = display_message
            if config.feishu_receive_id:
                text = f'<at user_id="{config.feishu_receive_id}"></at> {text}'
            _reply(root_id, text=text, reply_in_thread=True)
            if session_id:
                session_store.mark_subscribed(session_id)
            return {"ok": True, "msg_id": root_id}

        # User-initiated: send title as thread root, reply with content
        thread_title = _make_title(cwd, session_id, message)
        root_id = _send(text=thread_title)
        if root_id:
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            text = display_message
            if config.feishu_receive_id:
                text = f'<at user_id="{config.feishu_receive_id}"></at> {text}'
            _reply(root_id, text=text, reply_in_thread=True)
            if session_id:
                session_store.mark_subscribed(session_id)
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

    request_id = str(uuid.uuid4())
    _perm_requests[request_id] = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tty": tty,
        "hook_data_full": hook_data_full,
        "permission_suggestions": perm_suggestions,
        "created_at": time.time(),
    }
    _perm_events[request_id] = threading.Event()

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
                    _edit_message(reply_id, t("feishu.started_with_session", session_id=session_id[:8], tmux=tty))

    # Generate appropriate card based on tool type and permission_suggestions
    permission_mode = hook_data_full.get("permission_mode", "")
    if tool_name == "AskUserQuestion":
        questions = tool_input.get("questions", [])
        card = _build_askuserquestion_card(request_id, questions)
    else:
        # Determine perm_type for unified card builder
        if permission_mode == "plan" and not perm_suggestions:
            perm_type = "plan"
        elif perm_suggestions and perm_suggestions[0].get("type") == "setMode":
            perm_type = "setMode"
        else:
            perm_type = "addRules"
        terminal_options = _capture_terminal_options(tty)
        logger.info(f"[PERM_DEBUG] perm_type={perm_type} terminal_options={terminal_options}")
        card = _build_dynamic_permission_card(request_id, perm_type, tool_name, tool_input, terminal_options)

    if root_msg_id:
        logger.info(f"[CARD_DEBUG] Replying card in thread root_msg_id={root_msg_id} for {tool_name}")
        _reply_card(root_msg_id, card, reply_in_thread=True)
    else:
        logger.info(f"[CARD_DEBUG] Sending card as root message for {tool_name} (no root_msg_id, session={session_id[:8] if session_id else 'none'}, tty={tty})")
        _send_card(card)

    project = basename(cwd) if cwd else "unknown"
    logger.info(f"Permission request: {tool_name} | rid={request_id[:8]} tmux={tty} ({project})")
    return {"ok": True, "request_id": request_id}


@app.get("/hook/permission/{request_id}/decision")
async def get_permission_decision(request_id: str):
    """Long-poll for a permission decision (up to 30s per call)."""
    event = _perm_events.get(request_id)
    if not event:
        return {"status": "not_found"}

    loop = asyncio.get_event_loop()
    decided = await loop.run_in_executor(None, event.wait, 30)

    if decided:
        decision = _perm_decisions.pop(request_id, None)
        _perm_events.pop(request_id, None)
        _perm_requests.pop(request_id, None)
        if decision:
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


# --- Init ---

def init(cfg: Config):
    global config, lark_client, session_store, agent_adapter
    config = cfg
    agent_adapter = get_agent(cfg.agent)
    lark_client = lark.Client.builder() \
        .app_id(cfg.feishu_app_id) \
        .app_secret(cfg.feishu_app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()
    session_store = SessionStore(cfg.state_path)
    session_store.load()
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
    )
    threading.Thread(target=cli.start, daemon=True).start()
    logger.info("Feishu WebSocket client started")

    _start_idle_reaper()
