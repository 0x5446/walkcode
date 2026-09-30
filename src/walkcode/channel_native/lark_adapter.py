"""Lark/Feishu channel adapter."""

from __future__ import annotations

import json
import math
import tempfile
import time

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import (
    attachment_download_dir,
    AttachmentRef,
    ChannelBinding,
    ChannelCapabilities,
    InboundEvent,
    _log_degrade,
    _maybe_await,
)
from .views import render_view_text


class LarkBotApi:
    def __init__(self, caller: Callable[[str, dict[str, Any]], Any] | None = None):
        self._caller = caller

    async def call(self, method: str, payload: dict[str, Any]) -> Any:
        if self._caller is None:
            raise RuntimeError("LarkBotApi requires a caller in channel-native core tests")
        result = self._caller(method, payload)
        return await _maybe_await(result)


class LarkChannelAdapter:
    def __init__(self, api: LarkBotApi):
        self.kind = "lark"
        self.api = api
        self._capabilities = ChannelCapabilities(
            editable_message=True,
            private_callback_ack=True,
            attachment_download=True,
        )

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def react_to_message(self, binding: ChannelBinding, message_id: str, emoji: str = "DONE") -> bool:
        # emoji is a Lark emoji_type key (DONE/OK/THUMBSUP/...), not a glyph.
        if not message_id:
            return False
        result = await self.api.call(
            "reactMessage",
            {"message_id": message_id, "emoji_type": emoji},
        )
        return bool(result.get("ok", True)) if isinstance(result, dict) else True

    def binding_for(self, chat_id: str, root_id: str = "") -> ChannelBinding:
        return ChannelBinding(
            channel_kind="lark",
            account_id="bot",
            chat_id=chat_id,
            thread_id=root_id,
            root_message_id=root_id,
        )

    def parse_event(self, payload: dict[str, Any]) -> InboundEvent:
        event_id = str(payload.get("event_id", ""))
        event = payload.get("event", {})
        if "message" in event:
            message = event.get("message", {})
            sender = event.get("sender", {})
            sender_id = sender.get("sender_id", {})
            root_id = str(message.get("root_id", "") or "")
            message_id = str(message.get("message_id", "") or "")
            root = root_id or message_id
            content = self._decode_content(message.get("content", ""))
            text = self._parse_message_text(content)
            attachments = self._attachments_from_message(message, content)
            overflow = self._post_attachment_overflow(content)
            if overflow:
                # Truncation must be visible to the agent and the user, not
                # just the operator log — otherwise the turn silently runs on
                # partial input.
                note = (
                    f"[消息共 {overflow + self._MAX_POST_ATTACHMENTS} 个附件，"
                    f"超出上限，仅保留前 {self._MAX_POST_ATTACHMENTS} 个]"
                )
                text = f"{text}\n{note}" if text.strip() else note
            try:
                created_at = float(message.get("create_time", 0) or 0) / 1000.0
            except (TypeError, ValueError):
                created_at = 0.0
            if not (created_at > 0 and math.isfinite(created_at) and created_at < time.time() + 60.0):
                # NaN/Infinity/负数/明显未来的时间戳一律视为未知：进水位会
                # 把所有后续正常消息判旧（ADR 0057 审查 R1）。
                created_at = 0.0
            return InboundEvent(
                event_id=f"lark:{event_id}",
                channel_kind="lark",
                account_id="bot",
                chat_id=str(message.get("chat_id", "")),
                thread_id=root,
                message_id=message_id,
                root_message_id=root,
                sender_id=str(sender_id.get("open_id", "") or event.get("open_id", "")),
                sender_display=str(sender.get("sender_type", "") or ""),
                text=text,
                attachments=attachments,
                raw=payload,
                created_at=created_at,
            )
        action = event.get("action", {})
        value = action.get("value", {}) if isinstance(action, dict) else {}
        token = str(value.get("token", "") or value.get("callback_token", ""))
        action_name = str(value.get("action", ""))
        form_value = action.get("form_value") if isinstance(action, dict) else None
        root_id = str(event.get("root_id", "") or "")
        message_id = str(event.get("message_id", "") or "")
        root = root_id or message_id
        return InboundEvent(
            event_id=f"lark:{event_id}",
            channel_kind="lark",
            account_id="bot",
            chat_id=str(event.get("chat_id", "")),
            thread_id=root,
            message_id=message_id,
            root_message_id=root,
            sender_id=str(event.get("open_id", "") or event.get("operator", {}).get("open_id", "")),
            sender_display="",
            text=token,
            # "data" carries the action name: tokenless buttons (e.g.
            # the status card's request_takeover) are routed by action name.
            # "form" carries a form-container submit's field values (locally
            # staged selections arrive in one callback).
            callback={
                "token": token,
                "action": action_name,
                "data": token or action_name,
                "value": value,
                "form": form_value if isinstance(form_value, dict) else {},
            },
            raw=payload,
        )

    async def send_view(self, binding: ChannelBinding, view_model: dict[str, Any]) -> str:
        text = render_view_text(view_model)
        method = "sendCard" if self._is_interactive(view_model) else "sendMessage"
        payload = {
            "chat_id": binding.chat_id,
            "root_id": binding.root_message_id,
            "text": text,
            "view": dict(view_model),
        }
        result = await self.api.call(method, payload)
        return str(result.get("data", {}).get("message_id", ""))

    async def edit_view(self, binding: ChannelBinding, message_id: str, view_model: dict[str, Any]) -> bool:
        payload = {
            "chat_id": binding.chat_id,
            "message_id": message_id,
            "root_id": binding.root_message_id,
            "text": render_view_text(view_model),
            "view": dict(view_model),
        }
        result = await self.api.call("editCard", payload)
        return bool(result.get("ok", True))

    async def ack_callback(self, inbound: InboundEvent) -> None:
        await self.api.call(
            "ackCallback",
            {
                "event_id": inbound.event_id,
                "message_id": inbound.message_id,
                "token": str((inbound.callback or {}).get("token", "")),
            },
        )

    async def download_attachment(self, attachment: AttachmentRef) -> AttachmentRef:
        resource_type = "image" if attachment.mime.startswith("image/") else "file"
        result = await self.api.call(
            "downloadResource",
            {
                "message_id": attachment.source_message_id,
                "file_key": attachment.source_id,
                "type": resource_type,
            },
        )
        content = self._download_content_bytes(result)
        suffix, mime = self._resolve_download_type(result, attachment.mime, content)
        if suffix == ".img":
            # An unidentifiable image falls back to the opaque ".img" the
            # sniffer exists to eliminate; leave a trace so the recurrence
            # (new format, truncated bytes, odd API response) is debuggable.
            _log_degrade(
                "lark_image_sniff_failed",
                message_id=attachment.source_message_id,
                source_id=attachment.source_id,
                content_bytes=len(content),
            )
        with tempfile.NamedTemporaryFile(
            "wb",
            prefix="walkcode-lark-",
            suffix=suffix,
            dir=attachment_download_dir(),
            delete=False,
        ) as tmp:
            tmp.write(content)
            local_path = tmp.name
        return AttachmentRef(
            source_id=attachment.source_id,
            mime=mime,
            local_path=local_path,
            source_message_id=attachment.source_message_id,
        )

    @staticmethod
    def _is_interactive(view_model: dict[str, Any]) -> bool:
        return str(view_model.get("type", "")) in {
            "permission_prompt",
            "ask_user_question",
            "health",
            "status",
            "takeover_prompt",
            "takeover_confirmation",
            "takeover_progress",
            "manual_only",
            "model_choice",
        }

    # A forwarded chat log can carry hundreds of messages; render a bounded
    # prefix so one forward cannot blow up the turn's input.
    _MAX_RENDERED_MESSAGES = 60
    _MAX_RENDERED_CHARS_PER_MESSAGE = 500

    async def fetch_message(self, message_id: str) -> list[dict[str, Any]]:
        """Read one message. For merge_forward this also returns its children.

        Lark's GET /im/v1/messages/:id documents that a merge_forward query
        yields "1 forward message plus N child messages" in the same items
        array — which is why unpacking a forward needs no per-child fetch.
        """
        if not str(message_id or "").strip():
            return []
        result = await self.api.call("getMessage", {"message_id": str(message_id)})
        items = (result.get("data", {}) or {}).get("items", [])
        return [item for item in items if isinstance(item, dict)]

    async def fetch_thread_messages(
        self, root_message_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Read a topic's replies oldest-first, capped at `limit` messages."""
        if not str(root_message_id or "").strip():
            return []
        collected: list[dict[str, Any]] = []
        page_token = ""
        while len(collected) < limit:
            result = await self.api.call(
                "listThreadMessages",
                {
                    "container_id": str(root_message_id),
                    "page_size": min(50, limit - len(collected)),
                    "page_token": page_token,
                },
            )
            data = result.get("data", {}) or {}
            collected.extend(item for item in data.get("items", []) if isinstance(item, dict))
            page_token = str(data.get("page_token", "") or "")
            if not data.get("has_more") or not page_token:
                break
        return collected[:limit]

    @classmethod
    def render_message_log(cls, items: list[dict[str, Any]], *, skip_message_id: str = "") -> str:
        """Render fetched messages as a "who said what" transcript.

        Deleted messages and the container message itself are dropped; text is
        clipped per message and the whole list is capped, with the truncation
        stated inline rather than silently.
        """
        lines: list[str] = []
        dropped = 0
        for item in items:
            if item.get("deleted"):
                continue
            message_id = str(item.get("message_id", "") or "")
            if skip_message_id and message_id == skip_message_id:
                continue
            if str(item.get("msg_type", "") or "") == "merge_forward":
                # The container of a forward carries no prose of its own.
                continue
            text = cls._parse_message_text(cls._decode_content(item.get("content", "")))
            text = " ".join(str(text or "").split())
            if not text:
                continue
            if len(lines) >= cls._MAX_RENDERED_MESSAGES:
                dropped += 1
                continue
            if len(text) > cls._MAX_RENDERED_CHARS_PER_MESSAGE:
                text = text[: cls._MAX_RENDERED_CHARS_PER_MESSAGE] + "…"
            speaker = str(item.get("sender_id", "") or "") or "unknown"
            lines.append(f"{speaker}: {text}")
        if dropped:
            lines.append(f"[另有 {dropped} 条消息超出上限未展开]")
        return "\n".join(lines)

    @staticmethod
    def _decode_content(content: Any) -> Any:
        if not content:
            return {}
        if isinstance(content, dict):
            return content
        try:
            return json.loads(str(content))
        except json.JSONDecodeError:
            return content

    @classmethod
    def _parse_message_text(cls, content: Any) -> str:
        if isinstance(content, dict):
            if "text" in content:
                return str(content["text"])
            if cls._post_bodies(content):
                # post (rich text): the mobile "image + caption" send. Prose
                # and image keys live nested inside paragraph segments, not in
                # top-level fields — a flat read would yield "" and get the
                # message dropped as empty.
                return cls._parse_post_text(content)
            if "title" in content:
                return str(content["title"])
            return ""
        return str(content or "")

    # Post attachments are unbounded user input; cap what a single rich-text
    # message may fan out into downloads so one blast cannot wedge the inbound
    # loop on Lark API calls and disk writes.
    _MAX_POST_ATTACHMENTS = 10

    @staticmethod
    def _post_bodies(content: dict[str, Any]) -> list[dict[str, Any]]:
        """Post (rich text) bodies of a decoded message content dict.

        Receive events carry the flat shape ``{"title": ..., "content":
        [[segment, ...], ...]}``; the send-side API (and legacy events) wraps
        the same body under locale keys like ``{"zh_cn": {...}}``. Accept
        both so a valid post can never parse to empty and get dropped.
        """
        if isinstance(content.get("content"), list):
            return [content]
        return [
            value
            for value in content.values()
            if isinstance(value, dict) and isinstance(value.get("content"), list)
        ]

    @classmethod
    def _post_paragraphs(cls, content: dict[str, Any]) -> list[list[dict[str, Any]]]:
        """Paragraph rows across all post bodies, defensively typed.

        Each segment is a ``{"tag": ...}`` dict (text / a / at / img / media /
        emotion / code_block / hr / md).
        """
        return [
            [segment for segment in paragraph if isinstance(segment, dict)]
            for body in cls._post_bodies(content)
            for paragraph in body["content"]
            if isinstance(paragraph, list)
        ]

    @staticmethod
    def _post_segment_text(segment: dict[str, Any]) -> str:
        tag = str(segment.get("tag", "") or "")
        if tag == "at":
            name = str(segment.get("user_name", "") or segment.get("user_id", "") or "")
            return f"@{name}" if name else ""
        if tag == "a":
            label = str(segment.get("text", "") or "")
            href = str(segment.get("href", "") or "")
            if label and href and label != href:
                return f"{label} ({href})"
            return label or href
        if tag in {"img", "media"}:
            # Pictures and videos surface as attachments, not prose.
            return ""
        # text / md / code_block carry prose in "text"; unknown future tags
        # degrade to their "text" field instead of vanishing.
        return str(segment.get("text", "") or "")

    @classmethod
    def _parse_post_text(cls, content: dict[str, Any]) -> str:
        lines = [str(body.get("title", "") or "") for body in cls._post_bodies(content)]
        for paragraph in cls._post_paragraphs(content):
            lines.append("".join(cls._post_segment_text(segment) for segment in paragraph))
        return "\n".join(line for line in lines if line.strip())

    @classmethod
    def _post_attachment_segments(cls, content: dict[str, Any]) -> list[dict[str, Any]]:
        """img / media segments of a post body that reference a downloadable key."""
        segments: list[dict[str, Any]] = []
        for paragraph in cls._post_paragraphs(content):
            for segment in paragraph:
                tag = str(segment.get("tag", "") or "")
                if tag == "img" and segment.get("image_key"):
                    segments.append(segment)
                elif tag == "media" and segment.get("file_key"):
                    segments.append(segment)
        return segments

    @classmethod
    def _post_attachment_overflow(cls, content: Any) -> int:
        """How many post attachments the cap dropped (0 when under the cap)."""
        if not isinstance(content, dict):
            return 0
        return max(0, len(cls._post_attachment_segments(content)) - cls._MAX_POST_ATTACHMENTS)

    @classmethod
    def _attachments_from_message(cls, message: dict[str, Any], content: Any) -> list[AttachmentRef]:
        if not isinstance(content, dict):
            return []
        message_id = str(message.get("message_id", ""))
        message_type = str(message.get("message_type", "") or message.get("msg_type", ""))
        attachments: list[AttachmentRef] = []
        image_key = str(content.get("image_key", "") or "")
        if image_key:
            attachments.append(
                AttachmentRef(
                    source_id=image_key,
                    mime="image/*",
                    source_message_id=message_id,
                )
            )
        file_key = str(content.get("file_key", "") or "")
        if file_key or message_type == "file":
            source_id = file_key or str(content.get("key", "") or "")
            if source_id:
                attachments.append(
                    AttachmentRef(
                        source_id=source_id,
                        mime=str(content.get("mime_type", "") or ""),
                        source_message_id=message_id,
                    )
                )
        post_segments = cls._post_attachment_segments(content)
        if len(post_segments) > cls._MAX_POST_ATTACHMENTS:
            _log_degrade(
                "post_attachments_truncated",
                message_id=message_id,
                kept=cls._MAX_POST_ATTACHMENTS,
                total=len(post_segments),
            )
            post_segments = post_segments[: cls._MAX_POST_ATTACHMENTS]
        for segment in post_segments:
            if str(segment.get("tag", "") or "") == "img":
                attachments.append(
                    AttachmentRef(
                        source_id=str(segment["image_key"]),
                        mime="image/*",
                        source_message_id=message_id,
                    )
                )
            else:
                attachments.append(
                    AttachmentRef(
                        source_id=str(segment["file_key"]),
                        mime=str(segment.get("mime_type", "") or ""),
                        source_message_id=message_id,
                    )
                )
        return attachments

    @staticmethod
    def _download_content_bytes(result: Any) -> bytes:
        if isinstance(result, bytes):
            return result
        if isinstance(result, str):
            return result.encode("utf-8")
        if isinstance(result, dict):
            content = result.get("content", b"")
            if isinstance(content, bytes):
                return content
            return str(content).encode("utf-8")
        return bytes(result)

    @classmethod
    def _download_suffix(cls, result: Any, mime: str, content: bytes = b"") -> str:
        return cls._resolve_download_type(result, mime, content)[0]

    @classmethod
    def _resolve_download_type(cls, result: Any, mime: str, content: bytes) -> tuple[str, str]:
        """Single source of truth for a download's ``(suffix, mime)``.

        Priority: a sender-provided file name wins wholesale — its suffix is
        kept and the inbound mime is left untouched, so path and metadata can
        never contradict each other. Without one (Lark image resources never
        carry a file name), a single content sniff decides both: agents get an
        honest, readable extension instead of the opaque ".img" they had to
        rename before Read worked, and the "image/*" placeholder mime is
        replaced by the sniffed concrete type. Fallbacks never fabricate a
        type.
        """
        if isinstance(result, dict):
            named = Path(str(result.get("file_name", "") or "")).suffix
            if named:
                return named, mime
        sniffed = cls._sniff_image_type(content)
        if sniffed is not None:
            suffix, sniffed_mime = sniffed
            # A concrete inbound mime (file/media messages) is sender
            # metadata — keep it; only the placeholder gets upgraded.
            return suffix, (sniffed_mime if mime == "image/*" else mime)
        if mime == "application/pdf":
            return ".pdf", mime
        if mime.startswith("image/"):
            return ".img", mime
        return "", mime

    # Magic-byte table for image formats Lark realistically delivers. Python
    # >= 3.13 removed stdlib ``imghdr``, so this is hand-rolled on purpose.
    _IMAGE_MAGIC: tuple[tuple[bytes, str, str], ...] = (
        (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
        (b"GIF87a", ".gif", "image/gif"),
        (b"GIF89a", ".gif", "image/gif"),
        (b"II*\x00", ".tiff", "image/tiff"),
        (b"MM\x00*", ".tiff", "image/tiff"),
        (b"\x00\x00\x01\x00", ".ico", "image/x-icon"),
    )

    # ISO-BMFF brands that pin down a concrete image codec. The generic HEIF
    # container brands (mif1/msf1) deliberately have no entry: they only say
    # "HEIF container", so the compatible-brand list must name the codec.
    _FTYP_CODEC_BRANDS: dict[bytes, tuple[str, str]] = {
        b"heic": (".heic", "image/heic"),
        b"heix": (".heic", "image/heic"),
        b"avif": (".avif", "image/avif"),
        b"avis": (".avif", "image/avif"),
    }

    _FTYP_GENERIC_BRANDS = frozenset({b"mif1", b"msf1"})

    @classmethod
    def _sniff_image_type(cls, content: bytes) -> tuple[str, str] | None:
        """Return ``(suffix, mime)`` for recognized image bytes; None otherwise.

        Deliberately returns None for unknown content instead of guessing —
        the caller keeps its existing fallback and never fabricates a type.
        """
        if not content:
            return None
        for magic, suffix, mime in cls._IMAGE_MAGIC:
            if content.startswith(magic):
                return suffix, mime
        if len(content) >= 12 and content[4:8] == b"ftyp":
            return cls._sniff_ftyp_brands(content)
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp", "image/webp"
        # BMP's bare "BM" prefix is too weak on its own; require the reserved
        # header bytes (offset 6-9) to be zero as the spec mandates.
        if len(content) >= 14 and content[:2] == b"BM" and content[6:10] == b"\x00\x00\x00\x00":
            return ".bmp", "image/bmp"
        return None

    @classmethod
    def _sniff_ftyp_brands(cls, content: bytes) -> tuple[str, str] | None:
        """Resolve an ISO-BMFF ``ftyp`` box to ``(suffix, mime)``, or None.

        The major brand alone is not enough: generic HEIF containers use
        ``mif1``/``msf1`` as the major brand and name the actual codec (heic,
        avif, ...) in the compatible-brand list. So: major brand first, then —
        for generic majors only — walk the compatible brands bounded by the
        box size. Unknown brands return None rather than a guess.
        """
        major = content[8:12]
        hit = cls._FTYP_CODEC_BRANDS.get(major)
        if hit is not None:
            return hit
        if major not in cls._FTYP_GENERIC_BRANDS:
            return None
        box_size = int.from_bytes(content[0:4], "big")
        # Clamp the scan: a corrupt/hostile size field must not run past the
        # buffer, and 256 bytes is far beyond any real ftyp box.
        end = min(box_size if box_size >= 16 else 16, len(content), 256)
        for offset in range(16, end - 3, 4):
            hit = cls._FTYP_CODEC_BRANDS.get(content[offset:offset + 4])
            if hit is not None:
                return hit
        return None
