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
from .tty import inject, validate_target, get_session_activity, kill_session

logger = logging.getLogger("walkcode")

app = FastAPI(title="WalkCode", version="0.5.0")

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
_IDLE_TIMEOUT = 7200  # 2h — kill tmux sessions idle longer than this
_REAPER_INTERVAL = 600  # 10min — how often the idle reaper runs
_STALE_SESSION_MESSAGE = "⚠️ tmux 会话已失效，请等待 Claude 的下一条通知刷新会话"
_pending_roots: dict[str, str] = {}  # tmux_session_name → root_msg_id
_pending_msg_to_tty: dict[str, str] = {}  # root_msg_id → tmux_session_name
_pending_reply_ids: dict[str, str] = {}  # tmux_session_name → reply_message_id


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


def _build_card(message: str, session_id: str = "", title: str = "") -> dict:
    """Build a Feishu interactive card with content and permission buttons."""
    card: dict = {"config": {"wide_screen_mode": True}}

    if title:
        card["header"] = {
            "title": {"tag": "plain_text", "content": title},
            "template": "orange",
        }

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

    card["elements"] = elements or [{"tag": "div", "text": {"tag": "plain_text", "content": " "}}]
    return card


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
    if session_error or not session:
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
    cmd = f"cd '{cwd}' && claude --permission-mode dontAsk '{escaped}'"

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
    _pending_msg_to_tty[message_id] = tmux_name
    reply_id = _reply(message_id, f"🚀 已启动 Claude Code\ntmux attach -t {tmux_name}", reply_in_thread=True)
    if reply_id:
        _pending_reply_ids[tmux_name] = reply_id
    logger.info(f"Started Claude Code: tmux={tmux_name} cwd={cwd} prompt={prompt[:50]}")


def _resume_claude(session_id: str, old_session: Session, reply_text: str, message_id: str):
    """Resume a dead Claude session in a new tmux, reusing the Feishu thread."""
    cwd = old_session.cwd or config.default_cwd
    tmux_name = f"walkcode-{int(time.time())}"
    escaped_sid = session_id.replace("'", "'\\''")
    cmd = f"cd '{cwd}' && claude --resume '{escaped_sid}' --permission-mode dontAsk"

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_name, cmd],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.error(f"tmux new-session for resume failed: {result.stderr.strip()}")
            _reply(message_id, f"⚠️ 恢复失败: {result.stderr.strip()}")
            return
    except Exception as e:
        logger.error(f"Resume Claude failed: {e}")
        _reply(message_id, f"⚠️ 恢复失败: {e}")
        return

    session_store.upsert(session_id, tty=tmux_name, cwd=cwd, root_msg_id=old_session.root_msg_id)
    _reply(message_id, f"🔄 已恢复 Claude Code 会话\ntmux attach -t {tmux_name}")
    logger.info(f"Resumed Claude: session={session_id[:8]} tmux={tmux_name} cwd={cwd}")

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
    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    # --- Parse message content early ---
    if msg.message_type != "text":
        if parent_id or root_id:
            _reply(message_id, "⚠️ 只支持文本回复")
        return

    try:
        text = json.loads(msg.content).get("text", "").strip()
    except (json.JSONDecodeError, TypeError):
        return
    text = _MENTION_RE.sub("", text).strip()
    if not text:
        return

    # --- New message: start a new Claude Code instance ---
    if not parent_id and not root_id:
        _start_claude(text, message_id)
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
                _resume_claude(session_id, session, text, message_id)
            else:
                _reply(message_id, "⚠️ 会话已过期，请发送新消息开始新会话")
            return
        if not session:
            return
        tty = session.tty
        project = basename(session.cwd) if session.cwd else "?"
    else:
        # Check pending Feishu-initiated sessions (hook not yet received)
        _root = root_id or parent_id
        _tmux = _pending_msg_to_tty.get(_root) if _root else None
        if _tmux:
            error = validate_target(_tmux)
            if error:
                _reply(message_id, _STALE_SESSION_MESSAGE)
                return
            tty = _tmux
            project = basename(config.default_cwd) if config.default_cwd else "?"
        else:
            logger.warning(
                "Reply to unknown thread message root=%s parent=%s (mapping lost or reply target never registered)",
                root_id or "-",
                parent_id or "-",
            )
            _reply(message_id, "⚠️ 找不到对应会话，请等待下一条通知后再回复")
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

    if not tty:
        return {"ok": False, "error": "missing tty (not in tmux?)"}

    effective_type = matcher or hook_type
    needs_card = effective_type == "permission_prompt"
    # Card title: use notification title, fall back to _LABELS
    card_title = title or _LABELS.get(effective_type, "")
    # Text display: combine title + message for non-card text replies
    if title and message:
        display_message = f"**{title}**\n{message}"
    else:
        display_message = message
    project = basename(cwd) if cwd else "unknown"
    logger.info(f"Hook: [{project}] {effective_type} | tmux={tty} session={session_id[:8] if session_id else '-'}")

    session = session_store.get(session_id) if session_id else None

    if session and session.root_msg_id:
        # Existing session: reply to thread root
        session_store.upsert(session_id, tty=tty, cwd=cwd)
        if needs_card:
            card = _build_card(message, session_id, title=card_title)
            msg_id = _reply(session.root_msg_id, card=card, reply_in_thread=True)
        else:
            msg_id = _reply(session.root_msg_id, text=display_message or effective_type, reply_in_thread=True)
        if msg_id:
            return {"ok": True, "msg_id": msg_id, "thread": session.root_msg_id}
    else:
        # New session: check if Feishu-initiated (pending root exists)
        pending_root = _pending_roots.pop(tty, None)
        if pending_root:
            _pending_msg_to_tty.pop(pending_root, None)
            reply_id = _pending_reply_ids.pop(tty, None)
            # Feishu-initiated: reuse existing thread
            root_id = pending_root
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
                # Update the launch reply with session info
                if reply_id:
                    _edit_message(reply_id, f"🚀 Claude Code | {session_id[:8]}\ntmux attach -t {tty}")
            if needs_card:
                card = _build_card(message, session_id, title=card_title)
                _reply(root_id, card=card, reply_in_thread=True)
            elif display_message:
                _reply(root_id, text=display_message, reply_in_thread=True)
            else:
                _reply(root_id, text=effective_type, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

        # User-initiated: send title as thread root, reply with content
        thread_title = _make_title(cwd, session_id, message)
        root_id = _send(text=thread_title)
        if root_id:
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            if needs_card:
                card = _build_card(message, session_id, title=card_title)
                _reply(root_id, card=card, reply_in_thread=True)
            elif display_message:
                _reply(root_id, text=display_message, reply_in_thread=True)
            else:
                _reply(root_id, text=effective_type, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

    return {"ok": False, "error": "send failed"}


@app.get("/health")
async def health():
    return {"status": "ok", "sessions": session_store.count()}


# --- Idle reaper ---

def _reap_idle_sessions():
    """Check all tracked sessions and kill idle tmux sessions."""
    now = time.time()
    for session_id, session in session_store.items():
        if not session.tty:
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
                        "⏰ 会话因长时间无活动已关闭，回复任意消息可恢复",
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

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    logger.info("Idle reaper started (timeout=%ds, interval=%ds)", _IDLE_TIMEOUT, _REAPER_INTERVAL)


# --- Init ---

def init(cfg: Config):
    global config, lark_client, session_store
    config = cfg
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
