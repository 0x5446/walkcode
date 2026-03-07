"""WalkCode server: FastAPI for hooks + Feishu WebSocket for events."""

import json
import logging
import random
import re
import subprocess
import threading
import time
from os.path import basename

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
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
from fastapi import FastAPI, Request

from .config import Config
from .state import Session, SessionStore
from .tty import inject, validate_target

logger = logging.getLogger("walkcode")

app = FastAPI(title="WalkCode", version="0.4.0")

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
_TTL = 86400  # 24h
_STALE_SESSION_MESSAGE = "⚠️ tmux 会话已失效，请等待 Claude 的下一条通知刷新会话"
_pending_roots: dict[str, str] = {}  # tmux_session_name → root_msg_id


def _resolve_session_id(msg) -> str | None:
    return session_store.resolve(
        root_id=getattr(msg, "root_id", ""),
        parent_id=getattr(msg, "parent_id", ""),
    )


def _load_reply_session(session_id: str) -> tuple[Session | None, str | None]:
    session = session_store.get(session_id)
    if not session:
        return None, None

    error = validate_target(session.tty)
    if error:
        logger.warning("Session %s target invalid: %s", session_id[:8], error)
        return None, _STALE_SESSION_MESSAGE

    return session_store.touch(session_id), None


# --- Feishu helpers ---

_LABELS = {
    "stop": "✅ 任务完成",
    "permission_prompt": "🔐 需要权限确认",
    "idle_prompt": "⏳ 等待你的输入",
    "elicitation_dialog": "📋 请选择",
}

_PERMISSION_BUTTONS = [
    {"label": "✅ 允许 (y)", "cmd": "y", "type": "primary"},
    {"label": "❌ 拒绝 (n)", "cmd": "n", "type": "default"},
    {"label": "🔓 始终允许 (a)", "cmd": "a", "type": "default"},
]

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


def _build_card(message: str, session_id: str = "") -> dict:
    """Build a Feishu interactive card with content and permission buttons."""
    elements = []
    if message:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": message},
        })

    if session_id:
        actions = []
        for btn in _PERMISSION_BUTTONS:
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": btn["label"]},
                "type": btn["type"],
                "value": {"cmd": btn["cmd"], "sid": session_id},
            })
        elements.append({"tag": "action", "actions": actions})

    return {
        "config": {"wide_screen_mode": True},
        "elements": elements or [{"tag": "div", "text": {"tag": "plain_text", "content": " "}}],
    }


def _build_result_card(title: str, color: str, status_text: str) -> dict:
    """Build a card showing action result (buttons removed)."""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": status_text}},
        ],
    }


def _make_title(cwd: str, message: str = "") -> str:
    project = basename(cwd) if cwd else "unknown"
    if not message:
        return project
    snippet = message[:5].rstrip()
    ellipsis = "..." if len(message) > 5 else ""
    return f"{project} | {snippet}{ellipsis}"


def _send(text: str = "", card: dict | None = None) -> str | None:
    if card:
        msg_type = "interactive"
        content = json.dumps(card)
    else:
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


def _reply(message_id: str, text: str = "", card: dict | None = None, reply_in_thread: bool = False) -> str | None:
    if card:
        msg_type = "interactive"
        content = json.dumps(card)
    else:
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


# --- Feishu WebSocket event handlers ---

def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """Handle Feishu card button clicks (e.g. permission y/n/a)."""
    action = data.event.action
    value = action.value or {}
    cmd = value.get("cmd", "")
    session_id = value.get("sid", "")

    if not cmd or not session_id:
        logger.warning("Card action missing cmd or sid: %s", value)
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "error"
        toast.content = "无效的按钮操作"
        resp.toast = toast
        return resp

    session, session_error = _load_reply_session(session_id)
    if not session:
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "error"
        toast.content = session_error or "会话已过期"
        resp.toast = toast
        return resp

    try:
        inject(session.tty, cmd)
        project = basename(session.cwd) if session.cwd else "?"
        logger.info(f"Card action '{cmd}' -> {session.tty} ({project})")
        status = f"✅ 已送达 {session.tty}"
        toast_type = "success"
        color = "green"
    except Exception as e:
        logger.error(f"Card action inject failed: {e}")
        status = f"❌ 注入失败: {e}"
        toast_type = "error"
        color = "orange"

    resp = P2CardActionTriggerResponse()
    toast = CallBackToast()
    toast.type = toast_type
    toast.content = status
    resp.toast = toast

    card = CallBackCard()
    card.type = "raw"
    card.data = _build_result_card(
        title=f"🔐 权限确认 → {cmd}",
        color=color,
        status_text=status,
    )
    resp.card = card

    return resp


def _start_claude(prompt: str, message_id: str):
    """Start a Claude Code instance in a tmux session, triggered from Feishu."""
    cwd = config.default_cwd
    tmux_name = f"walkcode-{int(time.time())}"
    escaped = prompt.replace("'", "'\\''")
    cmd = f"cd '{cwd}' && claude '{escaped}'"

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, cmd],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.error(f"tmux new-session failed: {result.stderr.strip()}")
            _reply(message_id, f"⚠️ 启动失败: {result.stderr.strip()}", reply_in_thread=True)
            return
    except Exception as e:
        logger.error(f"Start Claude failed: {e}")
        _reply(message_id, f"⚠️ 启动失败: {e}", reply_in_thread=True)
        return

    _pending_roots[tmux_name] = message_id
    _reply(message_id, f"🚀 已启动 Claude Code\ntmux attach -t {tmux_name}", reply_in_thread=True)
    logger.info(f"Started Claude Code: tmux={tmux_name} cwd={cwd} prompt={prompt[:50]}")


def _on_message(data: P2ImMessageReceiveV1):
    sender_id = data.event.sender.sender_id
    logger.info("Message from open_id=%s", sender_id.open_id)
    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    if not parent_id and not root_id:
        # Non-reply message: start a new Claude Code instance
        if msg.message_type != "text":
            return
        try:
            text = json.loads(msg.content).get("text", "").strip()
        except (json.JSONDecodeError, TypeError):
            return
        text = _MENTION_RE.sub("", text).strip()
        if not text:
            return
        _start_claude(text, message_id)
        return

    session_id = _resolve_session_id(msg)
    if not session_id:
        logger.warning(
            "Reply to unknown thread message root=%s parent=%s (mapping lost or reply target never registered)",
            root_id or "-",
            parent_id or "-",
        )
        _reply(message_id, "⚠️ 找不到对应会话，请等待下一条通知后再回复")
        return

    session, session_error = _load_reply_session(session_id)
    if not session:
        if session_error:
            _reply(message_id, session_error)
        return

    if msg.message_type != "text":
        _reply(message_id, "⚠️ 只支持文本回复")
        return

    try:
        reply_text = json.loads(msg.content).get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return

    # Strip @mention placeholders (e.g. @_user_1)
    reply_text = _MENTION_RE.sub("", reply_text).strip()

    if not reply_text:
        return

    project = basename(session.cwd) if session.cwd else "?"

    try:
        inject(session.tty, reply_text)
        logger.info(f"Injected '{reply_text}' -> {session.tty} ({project})")
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

    if not tty:
        return {"ok": False, "error": "missing tty (not in tmux?)"}

    effective_type = matcher or hook_type
    needs_card = effective_type == "permission_prompt"
    project = basename(cwd) if cwd else "unknown"
    logger.info(f"Hook: [{project}] {effective_type} | tmux={tty} session={session_id[:8] if session_id else '-'}")

    session = session_store.get(session_id) if session_id else None

    if session and session.root_msg_id:
        # Existing session: reply to thread root
        session_store.upsert(session_id, tty=tty, cwd=cwd)
        if needs_card:
            card = _build_card(message, session_id)
            msg_id = _reply(session.root_msg_id, card=card, reply_in_thread=True)
        else:
            msg_id = _reply(session.root_msg_id, text=message or effective_type, reply_in_thread=True)
        if msg_id:
            return {"ok": True, "msg_id": msg_id, "thread": session.root_msg_id}
    else:
        # New session: check if Feishu-initiated (pending root exists)
        pending_root = _pending_roots.pop(tty, None)
        if pending_root:
            # Feishu-initiated: reuse existing thread
            root_id = pending_root
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            if needs_card:
                card = _build_card(message, session_id)
                _reply(root_id, card=card, reply_in_thread=True)
            elif message:
                _reply(root_id, text=message, reply_in_thread=True)
            else:
                _reply(root_id, text=effective_type, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

        # User-initiated: send title as thread root, reply with content
        title = _make_title(cwd, message)
        root_id = _send(text=title)
        if root_id:
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            if needs_card:
                card = _build_card(message, session_id)
                _reply(root_id, card=card, reply_in_thread=True)
            elif message:
                _reply(root_id, text=message, reply_in_thread=True)
            else:
                _reply(root_id, text=effective_type, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

    return {"ok": False, "error": "send failed"}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": session_store.count()}


# --- Init ---

def init(cfg: Config):
    global config, lark_client, session_store
    config = cfg
    lark_client = lark.Client.builder() \
        .app_id(cfg.feishu_app_id) \
        .app_secret(cfg.feishu_app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()
    session_store = SessionStore(cfg.state_path, ttl=_TTL)
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
