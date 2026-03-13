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
from .i18n import t
from .state import Session, SessionStore
from .tty import inject, validate_target, get_session_activity, kill_session

logger = logging.getLogger("walkcode")

app = FastAPI(title="WalkCode", version="0.7.1")

# --- State ---

config: Config = None  # type: ignore
lark_client: lark.Client = None  # type: ignore
session_store: SessionStore = None  # type: ignore
_IDLE_TIMEOUT = 7200  # 2h — kill tmux sessions idle longer than this
_REAPER_INTERVAL = 600  # 10min — how often the idle reaper runs


# --- Permission request state ---

_perm_requests: dict[str, dict] = {}   # request_id → {tool_name, tool_input, ...}
_perm_decisions: dict[str, dict] = {}  # request_id → {behavior, tool_name, always}
_perm_events: dict[str, threading.Event] = {}  # request_id → Event for signaling


def _build_permission_card(request_id: str, tool_name: str, tool_input: dict) -> dict:
    """Build a Feishu interactive card for a permission request."""
    input_str = json.dumps(tool_input, indent=2, ensure_ascii=False)
    if len(input_str) > 500:
        input_str = input_str[:500] + "\n..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": t("feishu.perm.header")},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**Tool:** `{tool_name}`\n**Input:**\n```json\n{input_str}\n```",
                },
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": t("feishu.perm.allow")},
                        "type": "primary",
                        "value": {"rid": request_id, "b": "allow"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": t("feishu.perm.deny")},
                        "type": "danger",
                        "value": {"rid": request_id, "b": "deny"},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": t("feishu.perm.always_allow")},
                        "type": "default",
                        "value": {"rid": request_id, "b": "always_allow"},
                    },
                ],
            },
        ],
    }


def _build_permission_result_card(tool_name: str, behavior: str) -> dict:
    """Build a result card showing the permission decision."""
    if behavior == "always_allow":
        label = t("feishu.perm.always_allowed")
        template = "green"
    elif behavior == "allow":
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


# --- Card action handler ---

def _on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """Handle Feishu card button clicks for permission decisions."""
    resp = P2CardActionTriggerResponse()
    try:
        event = data.event
        if not event or not event.action:
            return resp

        value = event.action.value or {}
        request_id = value.get("rid", "")
        behavior = value.get("b", "")

        if not request_id or not behavior:
            return resp

        req_data = _perm_requests.get(request_id)
        if not req_data:
            resp.toast = CallBackToast()
            resp.toast.type = "info"
            resp.toast.content = t("feishu.perm.expired")
            return resp

        tool_name = req_data.get("tool_name", "unknown")
        decision_behavior = "allow" if behavior in ("allow", "always_allow") else "deny"

        # Store decision and signal the waiting hook process
        _perm_decisions[request_id] = {
            "behavior": decision_behavior,
            "tool_name": tool_name,
            "always": behavior == "always_allow",
        }
        perm_event = _perm_events.get(request_id)
        if perm_event:
            perm_event.set()

        logger.info(f"Permission decision: {behavior} for {tool_name} (rid={request_id[:8]})")

        result_card = _build_permission_result_card(tool_name, behavior)

        # Return updated card inline (replaces buttons within 3s)
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = result_card

        # Toast notification
        if behavior == "always_allow":
            toast_text = t("feishu.perm.always_allowed")
        elif behavior == "allow":
            toast_text = t("feishu.perm.allowed")
        else:
            toast_text = t("feishu.perm.denied")
        resp.toast = CallBackToast()
        resp.toast.type = "success" if decision_behavior == "allow" else "warning"
        resp.toast.content = toast_text

        # If "always allow", add rule to settings.json
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

def _start_claude(prompt: str, message_id: str):
    """Start a Claude Code instance in a tmux session, triggered from Feishu."""
    cwd = config.default_cwd
    os.makedirs(cwd, exist_ok=True)
    tmux_name = f"walkcode-{int(time.time())}"
    escaped = prompt.replace("'", "'\\''")
    cmd = f"cd '{cwd}' && claude --permission-mode default '{escaped}'"

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
        logger.error(f"Start Claude failed: {e}")
        _reply(message_id, t("feishu.start_failed", error=e), reply_in_thread=True)
        return

    session_store.add_pending(tmux_name, message_id)
    reply_id = _reply(message_id, t("feishu.started", tmux=tmux_name), reply_in_thread=True)
    if reply_id:
        session_store.update_pending_reply(tmux_name, reply_id)
    logger.info(f"Started Claude Code: tmux={tmux_name} cwd={cwd} prompt={prompt[:50]}")


def _resume_claude(session_id: str, old_session: Session, reply_text: str, message_id: str):
    """Resume a dead Claude session in a new tmux, reusing the Feishu thread."""
    cwd = old_session.cwd or config.default_cwd
    tmux_name = f"walkcode-{int(time.time())}"
    escaped_sid = session_id.replace("'", "'\\''")
    cmd = f"cd '{cwd}' && claude --resume '{escaped_sid}' --permission-mode default"

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
        logger.error(f"Resume Claude failed: {e}")
        _reply(message_id, t("feishu.resume_failed", error=e))
        return

    session_store.upsert(session_id, tty=tmux_name, cwd=cwd, root_msg_id=old_session.root_msg_id)
    _reply(message_id, t("feishu.resumed", tmux=tmux_name))
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

    if not config.feishu_receive_id:
        print(t("serve.received_open_id", open_id=sender_id.open_id))
        return

    msg = data.event.message
    parent_id = msg.parent_id
    root_id = msg.root_id
    message_id = msg.message_id

    # --- Parse message content early ---
    if msg.message_type != "text":
        if parent_id or root_id:
            _reply(message_id, t("feishu.text_only"))
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
        msg_id = _reply(session.root_msg_id, text=display_message, reply_in_thread=True)
        if msg_id:
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
            _reply(root_id, text=display_message, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

        # User-initiated: send title as thread root, reply with content
        thread_title = _make_title(cwd, session_id, message)
        root_id = _send(text=thread_title)
        if root_id:
            if session_id:
                session_store.upsert(session_id, tty=tty, cwd=cwd, root_msg_id=root_id)
            _reply(root_id, text=display_message, reply_in_thread=True)
            return {"ok": True, "msg_id": root_id}

    return {"ok": False, "error": "send failed"}


@app.post("/hook/permission")
async def receive_permission_hook(request: Request):
    """Receive a PermissionRequest hook, send Feishu card, return request_id."""
    body = await request.json()
    tty = body.get("tty", "")
    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")
    tool_name = body.get("tool_name", "")
    tool_input = body.get("tool_input", {})

    if not tty:
        return {"ok": False, "error": "missing tty"}

    request_id = str(uuid.uuid4())
    _perm_requests[request_id] = {"tool_name": tool_name, "tool_input": tool_input, "tty": tty}
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

    card = _build_permission_card(request_id, tool_name, tool_input)
    if root_msg_id:
        _reply_card(root_msg_id, card, reply_in_thread=True)
    else:
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
