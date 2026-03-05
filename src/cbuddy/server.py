"""CBuddy server: FastAPI for hooks + Feishu WebSocket for events."""

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from os.path import basename

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    P2ImMessageReceiveV1,
)
from fastapi import FastAPI, Request

from .config import Config
from .tty import inject, validate_tty

logger = logging.getLogger("cbuddy")

app = FastAPI(title="CBuddy", version="0.2.0")

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore


@dataclass
class Session:
    tty: str
    cwd: str
    root_msg_id: str | None = None
    created_at: float = field(default_factory=time.time)


sessions: dict[str, Session] = {}        # session_id -> Session
root_to_session: dict[str, str] = {}     # root_msg_id -> session_id
_TTL = 86400  # 24h


def _cleanup():
    now = time.time()
    expired = [sid for sid, s in sessions.items() if now - s.created_at > _TTL]
    for sid in expired:
        s = sessions.pop(sid)
        if s.root_msg_id:
            root_to_session.pop(s.root_msg_id, None)


# --- Feishu helpers ---

_LABELS = {
    "stop": "✅ 任务完成",
    "permission_prompt": "🔐 需要权限确认 (回复 y/n/a)",
    "idle_prompt": "⏳ 等待你的输入",
}


def _make_message(hook_type: str, matcher: str, cwd: str, message: str) -> str:
    project = basename(cwd) if cwd else "unknown"
    label = _LABELS.get(matcher) or _LABELS.get(hook_type, hook_type)
    text = f"[{project}] {label}"
    if message:
        content = message[:300]
        if len(message) > 300:
            content += "..."
        text += f"\n> {content}"
    return text


def _send(text: str) -> str | None:
    body = CreateMessageRequestBody.builder() \
        .receive_id(config.feishu_receive_id) \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
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


def _reply(message_id: str, text: str) -> str | None:
    body = ReplyMessageRequestBody.builder() \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
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


# --- Feishu WebSocket event handler ---

def _on_message(data: P2ImMessageReceiveV1):
    msg = data.event.message
    parent_id = msg.parent_id
    message_id = msg.message_id

    if not parent_id:
        return

    session_id = root_to_session.get(parent_id)
    if not session_id:
        return

    session = sessions.get(session_id)
    if not session:
        return

    if msg.message_type != "text":
        _reply(message_id, "⚠️ 只支持文本回复")
        return

    try:
        reply_text = json.loads(msg.content).get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return

    if not reply_text:
        return

    tty_error = validate_tty(session.tty)
    if tty_error:
        _reply(message_id, f"❌ {tty_error}")
        return

    try:
        inject(session.tty, reply_text)
        project = basename(session.cwd) if session.cwd else "?"
        logger.info(f"Injected '{reply_text}' -> {session.tty} ({project})")
        _reply(message_id, f"✅ 已发送到 {session.tty}")
    except Exception as e:
        logger.error(f"Inject failed: {e}")
        _reply(message_id, f"❌ 注入失败: {e}")


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
        return {"ok": False, "error": "missing tty"}

    text = _make_message(hook_type, matcher, cwd, message)
    logger.info(f"Hook: {text} | tty={tty} session={session_id[:8] if session_id else '-'}")

    session = sessions.get(session_id) if session_id else None

    if session and session.root_msg_id:
        # Existing session: reply to thread root
        session.tty = tty  # update in case TTY changed
        session.cwd = cwd
        msg_id = _reply(session.root_msg_id, text)
        if msg_id:
            return {"ok": True, "msg_id": msg_id, "thread": session.root_msg_id}
    else:
        # New session: send standalone message (becomes thread root)
        msg_id = _send(text)
        if msg_id and session_id:
            sessions[session_id] = Session(tty=tty, cwd=cwd, root_msg_id=msg_id)
            root_to_session[msg_id] = session_id
            _cleanup()
            return {"ok": True, "msg_id": msg_id}
        elif msg_id:
            return {"ok": True, "msg_id": msg_id}

    return {"ok": False, "error": "send failed"}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": len(sessions)}


# --- Init ---

def init(cfg: Config):
    global config, lark_client
    config = cfg
    lark_client = lark.Client.builder() \
        .app_id(cfg.feishu_app_id) \
        .app_secret(cfg.feishu_app_secret) \
        .log_level(lark.LogLevel.INFO) \
        .build()


def start_ws_client(cfg: Config):
    handler = lark.EventDispatcherHandler.builder(
        cfg.feishu_verification_token, ""
    ).register_p2_im_message_receive_v1(
        _on_message
    ).build()

    cli = lark.ws.Client(
        cfg.feishu_app_id, cfg.feishu_app_secret,
        event_handler=handler, log_level=lark.LogLevel.INFO,
    )
    threading.Thread(target=cli.start, daemon=True).start()
    logger.info("Feishu WebSocket client started")
