"""Slack channel adapter (ADR 0070).

One Slack thread is one session, rooted on a bot-sent status card — the same
placement as Lark topics. Binding identity: ``account_id`` = workspace
``team_id``, ``chat_id`` = channel id, ``thread_id`` = ``root_message_id`` =
thread root ``ts``, ``message_id`` = message ``ts``.

Inbound payloads are the Socket Mode envelopes ``SlackIngressBridge`` writes
to the inbox: ``{"type": "events_api" | "interactive", "payload": {...}}``.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from .models import (
    AttachmentRef,
    ChannelBinding,
    ChannelCapabilities,
    InboundEvent,
    _log_degrade,
    attachment_download_dir,
)
from .slack_blocks import is_card, render_slack_messages
from .slack_live import SlackUncertainDeliveryError
from .views import render_view_text

_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
_USER_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]*)?>")
_CHANNEL_RE = re.compile(r"<#([A-Z0-9]+)(?:\|([^>]*))?>")
_SPECIAL_RE = re.compile(r"<!([a-z]+)(?:\^[^>|]*)?(?:\|([^>]*))?>")

# Session status on Slack's native agent UI, from the WalkCode health status.
_AGENT_SESSION_STATUS = {
    "running": "processing",
    "waiting_permission": "suspended",
    "waiting_user": "suspended",
    "stale": "processing",
    "idle": "active",
    "error": "active",
    "stopped": "closed",
}


class SlackChannelAdapter:
    kind = "slack"
    display_name = "Slack"
    locale = "zh"
    ack_reactions = ("white_check_mark", "ok_hand", "+1", "muscle", "clap")

    _MAX_ATTACHMENTS = 10
    _MAX_RENDERED_MESSAGES = 60
    _MAX_RENDERED_CHARS_PER_MESSAGE = 500
    # How long a send whose outcome was uncertain is remembered for lookup.
    _UNCERTAIN_TTL = 600.0

    def __init__(self, api: Any):
        self.api = api
        self._capabilities = ChannelCapabilities(
            editable_message=True,
            private_callback_ack=False,
            attachment_download=True,
            topic_sessions=True,
        )
        self._uncertain_sends: dict[str, float] = {}
        self._agent_status: dict[tuple[str, str], tuple[str, str]] = {}

    @property
    def account_id(self) -> str:
        return self.api.identity()[0]

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    # -- inbound -----------------------------------------------------------

    def parse_event(self, envelope: dict[str, Any]) -> InboundEvent:
        kind = str(envelope.get("type", "") or "")
        payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
        if kind == "interactive":
            return self._parse_block_actions(envelope, payload)
        return self._parse_message_event(envelope, payload)

    def _parse_message_event(self, envelope: dict[str, Any], payload: dict[str, Any]) -> InboundEvent:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        team_id = str(payload.get("team_id", "") or event.get("team", "") or "")
        bot_user_id = _bot_user_id(payload)
        channel = str(event.get("channel", "") or "")
        ts = str(event.get("ts", "") or "")
        root = str(event.get("thread_ts", "") or "") or ts
        raw_text = str(event.get("text", "") or "")
        mentioned = bool(bot_user_id) and f"<@{bot_user_id}>" in raw_text
        is_dm = str(event.get("channel_type", "") or "") == "im" or channel.startswith("D")
        text = _plain_text(raw_text, strip_user=bot_user_id)
        shared = _shared_messages(event)
        if shared:
            text = f"{text}\n{shared}" if text else shared
        attachments, overflow = self._attachments(event, channel, ts)
        if overflow:
            note = f"[消息共 {overflow + self._MAX_ATTACHMENTS} 个附件，超出上限，仅保留前 {self._MAX_ATTACHMENTS} 个]"
            text = f"{text}\n{note}" if text else note
        sender_team = str(event.get("user_team", "") or event.get("team", "") or team_id)
        external = bool(team_id) and sender_team != team_id
        return InboundEvent(
            event_id=f"slack:{team_id}:{channel}:{ts}",
            channel_kind="slack",
            account_id=team_id,
            chat_id=channel,
            thread_id=root,
            message_id=ts,
            root_message_id=root,
            # Users from another organization (Slack Connect) never match the
            # sender allowlist by accident: their id is namespaced.
            sender_id=f"{sender_team}:{event.get('user', '')}" if external else str(event.get("user", "") or ""),
            sender_display=str(event.get("user", "") or ""),
            text=_command_text(text),
            attachments=attachments,
            raw=envelope,
            created_at=_ts_seconds(ts),
            addressed=mentioned or is_dm or str(event.get("type", "")) == "app_mention",
        )

    def _parse_block_actions(self, envelope: dict[str, Any], payload: dict[str, Any]) -> InboundEvent:
        team_id = str((payload.get("team") or {}).get("id", "") or "")
        user = payload.get("user") or {}
        channel = str((payload.get("channel") or {}).get("id", "") or (payload.get("container") or {}).get("channel_id", "") or "")
        message = payload.get("message") or {}
        container = payload.get("container") or {}
        message_ts = str(message.get("ts", "") or container.get("message_ts", "") or "")
        root = str(message.get("thread_ts", "") or container.get("thread_ts", "") or "") or message_ts
        actions = payload.get("actions") or [{}]
        action = actions[0] if isinstance(actions[0], dict) else {}
        try:
            value = json.loads(str(action.get("value", "") or "{}"))
        except ValueError:
            value = {}
        if not isinstance(value, dict):
            value = {}
        token = str(value.get("token", "") or "")
        action_name = str(value.get("action", "") or "")
        user_team = str(user.get("team_id", "") or team_id)
        user_id = str(user.get("id", "") or "")
        return InboundEvent(
            event_id=f"slack:action:{team_id}:{message_ts}:{action.get('action_ts', '')}",
            channel_kind="slack",
            account_id=team_id,
            chat_id=channel,
            thread_id=root,
            message_id=message_ts,
            root_message_id=root,
            sender_id=f"{user_team}:{user_id}" if team_id and user_team != team_id else user_id,
            sender_display=str(user.get("username", "") or user.get("name", "") or ""),
            text=token,
            callback={
                "token": token,
                "action": action_name,
                "data": token or action_name,
                "value": value,
                "form": _form_values(payload.get("state")),
            },
            raw=envelope,
        )

    def _attachments(self, event: dict[str, Any], channel: str, ts: str) -> tuple[list[AttachmentRef], int]:
        files = [item for item in event.get("files") or () if isinstance(item, dict) and item.get("id")]
        refs = [
            AttachmentRef(
                source_id=str(item["id"]),
                mime=str(item.get("mimetype", "") or "application/octet-stream"),
                source_message_id=f"{channel}:{ts}",
            )
            for item in files[: self._MAX_ATTACHMENTS]
        ]
        return refs, max(0, len(files) - self._MAX_ATTACHMENTS)

    async def expand_inbound(self, inbound: InboundEvent) -> InboundEvent:
        # Shared messages arrive inline in the event; parse_event already
        # rendered them, so there is nothing to fetch.
        return inbound

    async def fetch_thread_messages(
        self, root_message_id: str, *, limit: int = 50, chat_id: str = ""
    ) -> list[dict[str, Any]]:
        if not str(root_message_id or "").strip() or not chat_id:
            return []
        collected: list[dict[str, Any]] = []
        cursor = ""
        while len(collected) < limit:
            data = await self.api.call(
                "conversations.replies",
                {
                    "channel": chat_id,
                    "ts": root_message_id,
                    "limit": min(200, limit - len(collected)),
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            collected.extend(item for item in data.get("messages") or () if isinstance(item, dict))
            cursor = str(((data.get("response_metadata") or {}).get("next_cursor", "")) or "")
            if not data.get("has_more") or not cursor:
                break
        return collected[:limit]

    def render_message_log(self, items: list[dict[str, Any]], *, skip_message_id: str = "") -> str:
        lines: list[str] = []
        dropped = 0
        for item in items:
            if skip_message_id and str(item.get("ts", "") or "") == skip_message_id:
                continue
            if item.get("subtype") in {"message_deleted", "tombstone"}:
                continue
            text = " ".join(_plain_text(str(item.get("text", "") or "")).split())
            if not text:
                continue
            if len(lines) >= self._MAX_RENDERED_MESSAGES:
                dropped += 1
                continue
            if len(text) > self._MAX_RENDERED_CHARS_PER_MESSAGE:
                text = text[: self._MAX_RENDERED_CHARS_PER_MESSAGE] + "…"
            who = "bot" if item.get("bot_id") else f"@{item.get('user', '?')}"
            lines.append(f"{who}: {text}")
        if dropped:
            lines.append(f"[另有 {dropped} 条消息未展开]")
        return "\n".join(lines)

    # -- outbound ----------------------------------------------------------

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str:
        messages = render_slack_messages(view_model, fallback_text=render_view_text(view_model))
        last_ts = ""
        for message in messages:
            last_ts = await self._post(binding, message)
        if str(view_model.get("type", "")) == "health":
            await self._sync_agent_session(binding, view_model, root_ts=binding.root_message_id or last_ts)
        return last_ts

    async def _post(self, binding: ChannelBinding, message: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "channel": binding.chat_id,
            "text": message["text"],
            "blocks": message["blocks"],
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if binding.root_message_id:
            payload["thread_ts"] = binding.root_message_id
        key = _send_key(payload)
        if key in self._uncertain_sends:
            found = await self._find_sent(payload)
            if found:
                self._uncertain_sends.pop(key, None)
                return found
        try:
            data = await self.api.call("chat.postMessage", payload)
        except SlackUncertainDeliveryError:
            now = time.monotonic()
            self._uncertain_sends = {k: t for k, t in self._uncertain_sends.items() if now - t < self._UNCERTAIN_TTL}
            self._uncertain_sends[key] = now
            raise
        self._uncertain_sends.pop(key, None)
        return str(data.get("ts", "") or "")

    async def _find_sent(self, payload: dict[str, Any]) -> str:
        """Find this bot's copy of a message whose send outcome was uncertain.

        ``chat.postMessage`` has no idempotency key; a timed-out send may have
        landed. Looking for an identical recent bot message turns the retry
        into a no-op instead of a duplicate.
        """
        try:
            if payload.get("thread_ts"):
                data = await self.api.call(
                    "conversations.replies",
                    {"channel": payload["channel"], "ts": payload["thread_ts"], "limit": 50},
                )
                candidates = list(data.get("messages") or ())[-20:]
            else:
                data = await self.api.call("conversations.history", {"channel": payload["channel"], "limit": 20})
                candidates = list(data.get("messages") or ())
        except Exception as exc:
            _log_degrade("slack_uncertain_send_lookup_failed", channel=payload.get("channel"), error=exc)
            return ""
        for item in reversed(candidates):
            if isinstance(item, dict) and item.get("bot_id") and str(item.get("text", "")) == payload["text"]:
                return str(item.get("ts", "") or "")
        return ""

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool:
        message = render_slack_messages(view_model, fallback_text=render_view_text(view_model))[0]
        await self.api.call(
            "chat.update",
            {
                "channel": binding.chat_id,
                "ts": message_id,
                "text": message["text"],
                "blocks": message["blocks"],
            },
        )
        if str(view_model.get("type", "")) == "health":
            await self._sync_agent_session(binding, view_model, root_ts=binding.root_message_id or message_id)
        return True

    async def _sync_agent_session(self, binding: ChannelBinding, view: dict[str, Any], *, root_ts: str) -> None:
        """Mirror the status card onto Slack's native agent session UI.

        Best effort: the card is the source of truth; a failure here (app not
        declared as an agent, older workspace) is logged once per thread and
        never fails the delivery.
        """
        status = _AGENT_SESSION_STATUS.get(str(view.get("status", "") or ""), "")
        title = " ".join(str(view.get("title", "") or "").split())[:200]
        if not status or not root_ts or not binding.chat_id:
            return
        key = (binding.chat_id, root_ts)
        previous = self._agent_status.get(key)
        if previous == (status, title):
            return
        self._agent_status[key] = (status, title)
        try:
            await self.api.call(
                "agents.sessions.setStatus",
                {"channel_id": binding.chat_id, "thread_ts": root_ts, "status": status, "title": title or None},
            )
            if previous is not None and title and previous[1] != title:
                await self.api.call(
                    "agents.sessions.rename",
                    {"channel_id": binding.chat_id, "thread_ts": root_ts, "title": title},
                )
        except Exception as exc:
            if previous is None:
                _log_degrade("slack_agent_session_sync_failed", channel=binding.chat_id, thread_ts=root_ts, error=exc)

    async def notify_private(self, inbound: InboundEvent, text: str) -> None:
        user = inbound.sender_id.rsplit(":", 1)[-1]
        if not (inbound.chat_id and user):
            return
        payload: dict[str, Any] = {"channel": inbound.chat_id, "user": user, "text": text}
        if inbound.root_message_id:
            payload["thread_ts"] = inbound.root_message_id
        await self.api.call("chat.postEphemeral", payload)

    async def ack_callback(self, inbound: InboundEvent) -> None:
        # Socket Mode envelopes are acknowledged by the ingress bridge right
        # after the inbox write; Slack has no toast to answer with.
        return None

    async def react_to_message(self, binding: ChannelBinding, message_id: str, emoji: str = "white_check_mark") -> bool:
        if not message_id:
            return False
        await self.api.call("reactions.add", {"channel": binding.chat_id, "timestamp": message_id, "name": emoji})
        return True

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        data = await self.api.call("files.info", {"file": attachment.source_id})
        info = data.get("file") or {}
        url = str(info.get("url_private_download", "") or info.get("url_private", "") or "")
        if not url:
            from .models import PermanentDeliveryError

            raise PermanentDeliveryError(f"slack file {attachment.source_id} has no download url")
        suffix = Path(str(info.get("name", "") or "")).suffix[:16]
        with tempfile.NamedTemporaryFile(
            "wb", prefix="walkcode-slack-", suffix=suffix, dir=attachment_download_dir(), delete=False
        ) as tmp:
            local_path = Path(tmp.name)
        try:
            await self.api.download(url, local_path)
        except BaseException:
            local_path.unlink(missing_ok=True)
            raise
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=str(info.get("mimetype", "") or attachment.mime),
            local_path=str(local_path),
            source_message_id=attachment.source_message_id,
        )

    def is_interactive(self, view_model: dict[str, Any]) -> bool:
        return is_card(view_model)


def _bot_user_id(payload: dict[str, Any]) -> str:
    for item in payload.get("authorizations") or ():
        if isinstance(item, dict) and item.get("is_bot") and item.get("user_id"):
            return str(item["user_id"])
    return ""


def _plain_text(text: str, *, strip_user: str = "") -> str:
    """Slack mrkdwn → plain text the agent can read."""
    if strip_user:
        text = re.sub(rf"<@{re.escape(strip_user)}(?:\|[^>]*)?>", "", text)
    text = _LINK_RE.sub(lambda m: f"{m.group(2)} ({m.group(1)})" if m.group(2) and m.group(2) != m.group(1) else m.group(1), text)
    text = _USER_RE.sub(lambda m: f"@{m.group(1)}", text)
    text = _CHANNEL_RE.sub(lambda m: f"#{m.group(2) or m.group(1)}", text)
    text = _SPECIAL_RE.sub(lambda m: f"@{m.group(2) or m.group(1)}", text)
    return html.unescape(text).strip()


def _command_text(text: str) -> str:
    """``!cmd`` → ``/cmd`` and ``!!cmd`` → ``//cmd``.

    Slack intercepts a leading ``/`` as one of its own slash commands and
    custom ones cannot run in threads, so WalkCode commands use ``!``; the
    rewrite lets the shared command path handle them unchanged.
    """
    stripped = text.lstrip()
    if stripped.startswith("!!") and len(stripped) > 2 and not stripped[2].isspace():
        return "//" + stripped[2:]
    if stripped.startswith("!") and len(stripped) > 1 and not stripped[1].isspace() and stripped[1] != "!":
        return "/" + stripped[1:]
    return text


def _shared_messages(event: dict[str, Any]) -> str:
    """Render messages shared/forwarded into this one ("[转发消息] …")."""
    parts: list[str] = []
    for item in event.get("attachments") or ():
        if not isinstance(item, dict) or not (item.get("is_share") or item.get("is_msg_unfurl")):
            continue
        body = _plain_text(str(item.get("text", "") or item.get("fallback", "") or ""))
        if not body:
            continue
        author = str(item.get("author_name", "") or item.get("author_subname", "") or "")
        parts.append(f"[转发消息] {author}: {body}" if author else f"[转发消息] {body}")
    return "\n".join(parts)


def _form_values(state: Any) -> dict[str, Any]:
    """Fold block_actions ``state.values`` into the Lark form shape.

    ``q{i}`` → picked option index (str) or index list; ``q{i}_other`` → text.
    """
    values = state.get("values") if isinstance(state, dict) else None
    form: dict[str, Any] = {}
    if not isinstance(values, dict):
        return form
    for block_id, elements in values.items():
        if not isinstance(elements, dict):
            continue
        for element in elements.values():
            if not isinstance(element, dict):
                continue
            if "selected_options" in element:
                picked = [str(o.get("value", "")) for o in element.get("selected_options") or () if isinstance(o, dict)]
                if picked:
                    form[str(block_id)] = picked
            elif isinstance(element.get("selected_option"), dict):
                form[str(block_id)] = str(element["selected_option"].get("value", ""))
            elif element.get("value"):
                form[str(block_id)] = str(element["value"])
    return form


def _ts_seconds(ts: str) -> float:
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return 0.0
    return value if 0 < value < time.time() + 60.0 else 0.0


def _send_key(payload: dict[str, Any]) -> str:
    material = json.dumps(
        [payload.get("channel"), payload.get("thread_ts"), payload.get("text")],
        ensure_ascii=False,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
