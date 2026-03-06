"""Agent Hotline server: FastAPI for hooks + Feishu WebSocket for events."""

import json
import logging
import threading
from os.path import basename
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
    P2ImMessageReceiveV1,
)
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
    CallBackToast,
    CallBackCard,
)
from fastapi import FastAPI, Request

from .config import Config
from .state import Session, SessionStore
from .tty import inject, inspect_tty_owner, set_terminal_title, validate_tty

logger = logging.getLogger("agent_hotline")

app = FastAPI(title="Agent Hotline", version="0.2.0")

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
_TTL = 86400  # 24h
_STALE_SESSION_MESSAGE = "⚠️ 终端映射已失效，请等待 Claude 的下一条通知刷新会话"


def _resolve_session_id(msg) -> str | None:
    return session_store.resolve(
        root_id=getattr(msg, "root_id", ""),
        parent_id=getattr(msg, "parent_id", ""),
    )


def _thread_reply(message_id: str, text: str) -> str | None:
    return _reply(message_id, text)


def _load_reply_session(session_id: str) -> tuple[Session | None, str | None]:
    session = session_store.get(session_id)
    if not session:
        return None, None

    if session.tty_pid and session.tty_pid_started_at:
        status, live_tty = inspect_tty_owner(session.tty_pid, session.tty_pid_started_at)
        if status != "ok" or not live_tty:
            logger.warning(
                "Session %s tty owner invalid: %s pid=%s",
                session_id[:8],
                status,
                session.tty_pid,
            )
            return None, _STALE_SESSION_MESSAGE
        if live_tty != session.tty:
            logger.info(
                "Refreshing tty for session %s: %s -> %s",
                session_id[:8],
                session.tty,
                live_tty,
            )
            return session_store.upsert(
                session_id,
                tty=live_tty,
                cwd=session.cwd,
                tty_pid=session.tty_pid,
                tty_pid_started_at=session.tty_pid_started_at,
            ), None

    return session_store.touch(session_id), None


# --- Feishu helpers ---

_LABELS = {
    "stop": "✅ 任务完成",
    "permission_prompt": "🔐 需要权限确认",
    "idle_prompt": "⏳ 等待你的输入",
    "elicitation_dialog": "📋 请选择",
}

_CARD_COLORS = {
    "stop": "green",
    "permission_prompt": "red",
    "idle_prompt": "blue",
    "elicitation_dialog": "blue",
}

_PERMISSION_BUTTONS = [
    {"label": "✅ 允许 (y)", "cmd": "y", "type": "primary"},
    {"label": "❌ 拒绝 (n)", "cmd": "n", "type": "default"},
    {"label": "🔓 始终允许 (a)", "cmd": "a", "type": "default"},
]


def _build_card(
    hook_type: str,
    matcher: str,
    cwd: str,
    message: str,
    tty: str = "",
    session_id: str = "",
) -> dict:
    """Build a Feishu interactive card JSON."""
    project = basename(cwd) if cwd else "unknown"
    label = _LABELS.get(matcher) or _LABELS.get(hook_type, hook_type)
    tty_label = Path(tty).name if tty else "tty?"
    session_label = session_id[:8] if session_id else "session?"
    title = f"[{project} {tty_label} {session_label}] {label}"
    color = _CARD_COLORS.get(matcher) or _CARD_COLORS.get(hook_type, "blue")

    elements = []
    if message:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": message},
        })

    # Add buttons for permission prompts
    effective_type = matcher or hook_type
    if effective_type == "permission_prompt" and session_id:
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
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
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


def _make_message(
    hook_type: str,
    matcher: str,
    cwd: str,
    message: str,
    tty: str = "",
    session_id: str = "",
) -> str:
    project = basename(cwd) if cwd else "unknown"
    label = _LABELS.get(matcher) or _LABELS.get(hook_type, hook_type)
    tty_label = Path(tty).name if tty else "tty?"
    session_label = session_id[:8] if session_id else "session?"
    text = f"[{project} {tty_label} {session_label}] {label}"
    if message:
        text += f"\n> {message}"
    return text


def _terminal_label(cwd: str, tty: str, session_id: str) -> str:
    project = basename(cwd) if cwd else "unknown"
    tty_label = Path(tty).name if tty else "tty?"
    session_label = session_id[:8] if session_id else "session?"
    return f"{project} {tty_label} {session_label}"


def _tag_terminal(tty: str, cwd: str, session_id: str):
    if not tty:
        return

    try:
        set_terminal_title(tty, _terminal_label(cwd, tty, session_id))
    except Exception as e:
        logger.warning("Set terminal title failed for %s: %s", tty, e)


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


def _reply(message_id: str, text: str = "", card: dict | None = None) -> str | None:
    if card:
        msg_type = "interactive"
        content = json.dumps(card)
    else:
        msg_type = "text"
        content = json.dumps({"text": text})
    body = ReplyMessageRequestBody.builder() \
        .msg_type(msg_type) \
        .content(content) \
        .build()
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

    tty_error = validate_tty(session.tty)
    if tty_error:
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "error"
        toast.content = f"终端不可用: {tty_error}"
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


def _on_message(data: P2ImMessageReceiveV1):
    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    if not parent_id and not root_id:
        logger.info(f"Non-reply message from {msg.sender.sender_id.open_id} (use this open_id for FEISHU_RECEIVE_ID)")
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
        _thread_reply(message_id, "⚠️ 只支持文本回复")
        return

    try:
        reply_text = json.loads(msg.content).get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return

    if not reply_text:
        return

    tty_error = validate_tty(session.tty)
    if tty_error:
        _thread_reply(message_id, f"❌ {tty_error}")
        return

    project = basename(session.cwd) if session.cwd else "?"

    try:
        inject(session.tty, reply_text)
        logger.info(f"Injected '{reply_text}' -> {session.tty} ({project})")
        final = f"✅ 已送达 {session.tty}"
    except Exception as e:
        logger.error(f"Inject failed: {e}")
        final = f"❌ 注入失败: {e}"

    _thread_reply(message_id, final)


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
    tty_pid = body.get("tty_pid")
    tty_pid_started_at = body.get("tty_pid_started_at")

    if not tty:
        return {"ok": False, "error": "missing tty"}

    _tag_terminal(tty, cwd, session_id)

    effective_type = matcher or hook_type
    needs_card = effective_type == "permission_prompt"
    text = _make_message(hook_type, matcher, cwd, message, tty=tty, session_id=session_id)
    logger.info(f"Hook: {text} | tty={tty} session={session_id[:8] if session_id else '-'}")

    session = session_store.get(session_id) if session_id else None

    if session and session.root_msg_id:
        # Existing session: reply to thread root
        session_store.upsert(
            session_id,
            tty=tty,
            cwd=cwd,
            tty_pid=tty_pid,
            tty_pid_started_at=tty_pid_started_at,
        )
        if needs_card:
            card = _build_card(hook_type, matcher, cwd, message, tty=tty, session_id=session_id)
            msg_id = _reply(session.root_msg_id, card=card)
        else:
            msg_id = _reply(session.root_msg_id, text=text)
        if msg_id:
            return {"ok": True, "msg_id": msg_id, "thread": session.root_msg_id}
    else:
        # New session: text root as thread anchor
        msg_id = _send(text=text)
        if msg_id and session_id:
            session_store.upsert(
                session_id,
                tty=tty,
                cwd=cwd,
                root_msg_id=msg_id,
                tty_pid=tty_pid,
                tty_pid_started_at=tty_pid_started_at,
            )
            # Only add card reply for interactive types (buttons)
            if needs_card:
                card = _build_card(hook_type, matcher, cwd, message, tty=tty, session_id=session_id)
                _reply(msg_id, card=card)
            return {"ok": True, "msg_id": msg_id}
        elif msg_id:
            if needs_card:
                card = _build_card(hook_type, matcher, cwd, message, tty=tty, session_id=session_id)
                _reply(msg_id, card=card)
            return {"ok": True, "msg_id": msg_id}

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
        cfg.feishu_verification_token, ""
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
