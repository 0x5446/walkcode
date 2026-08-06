"""Live Lark/Feishu API layer for the channel-native V3 runtime.

Everything network-facing for the Lark channel lives below the
``LarkBotApi.call(method, payload)`` seam:

- ``build_operation`` maps the adapter's five abstract methods to concrete
  OpenAPI operations (pure, unit-testable without the SDK);
- ``LarkLiveCaller`` executes operations through an injectable transport;
- ``SdkTransport`` is the real transport on top of ``lark-oapi`` (lazy import,
  works against both open.feishu.cn and open.larksuite.com via ``domain``);
- ``AckRegistry`` + ``LarkIngressBridge`` bridge the SDK's threaded WebSocket
  callbacks into the asyncio serve loop. Card callbacks must be answered
  inside Feishu's ~3s window, so the bridge parks a Future per event and the
  orchestrator's ``ack_callback`` resolves it with a toast; on timeout the
  bridge answers with a neutral toast and the durable outbox's card patch
  delivers the final state.

V2 discipline preserved (git show main:src/walkcode/server.py): WS callbacks
return immediately (blocking them drops heartbeats, forcing a reconnect that
redelivers events), card updates go through ``im.v1.message.patch`` (no edit
cap), and error handling separates content-caused permanent failures from
retryable transient ones — retry/backoff belongs to the OutboxDispatcher, not
this layer.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sys
import threading
import time
from typing import Any, Callable

from . import PermanentDeliveryError, TransientDeliveryError
from .lark_cards import render_lark_message

# Lark IM error codes where retrying the SAME request fails identically, so a
# retry only burns quota. Everything else (5xx, gateway, unknown) stays
# transient on purpose: when unsure, prefer retry over dropping agent output.
#
# Measured on 2026-08-04, from ~5100 failed calls in the runtime logs that ate
# most of a 10000/month Standard F3 quota:
#   230001                  request content itself is rejected
#   230002  (2064 failures) "Bot/User can NOT be out of the chat" — the bot is
#                           no longer in that chat. No amount of retrying puts
#                           it back; every attempt was pure waste.
#   230031                  "Message can only be updated within fourteen days"
#                           — an old card is frozen for good; the caller must
#                           send a new one instead of patching forever.
#   99991403 (856 failures) "This month's API call quota has been exceeded" —
#                           retrying the very error that says you are out of
#                           budget is a death spiral: the retries spend the
#                           headroom that would otherwise serve real traffic.
#                           It self-heals next month, not next second, so the
#                           only useful response is to stop.
PERMANENT_LARK_CODES = frozenset({230001, 230002, 230031, 99991403})
# Quota exhaustion is worth calling out separately from content errors: it is
# not this message's fault, and the operator needs to know the app is muted.
LARK_QUOTA_EXCEEDED_CODE = 99991403

DEFAULT_ACK_TIMEOUT = 2.5

_PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, KeyError, IndexError, ImportError)


def is_permanent_lark_code(code: int) -> bool:
    return int(code) in PERMANENT_LARK_CODES


def build_operation(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Translate one adapter-level call into a concrete OpenAPI operation.

    Returned dicts are plain data so route logic is testable without lark-oapi:
    ``{"kind": "reply"|"create", "msg_type", "content", ...}`` for sends,
    ``{"kind": "patch", ...}`` for edits, ``{"kind": "download", ...}`` for
    resources.
    """
    if method in {"sendCard", "sendMessage"}:
        view = payload.get("view")
        message = render_lark_message(
            view if isinstance(view, dict) else {},
            fallback_text=str(payload.get("text", "") or ""),
        )
        root_id = str(payload.get("root_id", "") or "")
        operation = {
            "msg_type": message["msg_type"],
            "content": json.dumps(message["content"], ensure_ascii=False),
        }
        if root_id:
            operation.update(kind="reply", message_id=root_id, reply_in_thread=True)
        else:
            operation.update(kind="create", chat_id=str(payload.get("chat_id", "") or ""))
        return operation
    if method == "editCard":
        view = payload.get("view")
        message = render_lark_message(
            view if isinstance(view, dict) else {},
            fallback_text=str(payload.get("text", "") or ""),
        )
        return {
            "kind": "patch",
            "message_id": str(payload.get("message_id", "") or ""),
            "content": json.dumps(message["content"], ensure_ascii=False),
        }
    if method == "reactMessage":
        return {
            "kind": "reaction",
            "message_id": str(payload.get("message_id", "") or ""),
            "emoji_type": str(payload.get("emoji_type", "") or "DONE"),
        }
    if method == "downloadResource":
        return {
            "kind": "download",
            "message_id": str(payload.get("message_id", "") or ""),
            "file_key": str(payload.get("file_key", "") or ""),
            "type": str(payload.get("type", "") or "file"),
        }
    if method == "getMessage":
        # Reading a merge_forward message returns the forward itself PLUS its
        # N child messages in one shot, which is the whole reason this exists:
        # a forwarded chat log otherwise reaches the agent as nothing but its
        # placeholder title.
        return {"kind": "get_message", "message_id": str(payload.get("message_id", "") or "")}
    if method == "listThreadMessages":
        # container_id_type=thread is the only way to read a topic's replies;
        # `chat` returns just the root. Lark also rejects start_time/end_time
        # for thread containers, so this operation deliberately has no time
        # window — bound the read with page_size/page_token instead.
        return {
            "kind": "list_thread",
            "container_id": str(payload.get("container_id", "") or ""),
            "page_size": max(1, min(50, int(payload.get("page_size", 50) or 50))),
            "page_token": str(payload.get("page_token", "") or ""),
        }
    raise PermanentDeliveryError(f"unknown Lark api method: {method}")


def message_item_to_dict(item: Any) -> dict[str, Any]:
    """Flatten one lark-oapi Message object into plain data.

    Keeps the adapter and everything above it testable without the SDK
    installed, matching how the send path already returns plain dicts.
    """
    sender = getattr(item, "sender", None)
    body = getattr(item, "body", None)
    mentions = getattr(item, "mentions", None) or []
    return {
        "message_id": str(getattr(item, "message_id", "") or ""),
        "msg_type": str(getattr(item, "msg_type", "") or ""),
        "chat_id": str(getattr(item, "chat_id", "") or ""),
        "root_id": str(getattr(item, "root_id", "") or ""),
        "parent_id": str(getattr(item, "parent_id", "") or ""),
        "create_time": str(getattr(item, "create_time", "") or ""),
        "deleted": bool(getattr(item, "deleted", False)),
        "sender_id": str(getattr(sender, "id", "") or ""),
        "sender_name": str(getattr(sender, "sender_name", "") or ""),
        "sender_type": str(getattr(sender, "sender_type", "") or ""),
        "content": str(getattr(body, "content", "") or ""),
        "mentions": [
            {
                "key": str(getattr(mention, "key", "") or ""),
                "id": str(getattr(mention, "id", "") or ""),
                "name": str(getattr(mention, "name", "") or ""),
            }
            for mention in mentions
        ],
    }


class AckRegistry:
    """Thread-safe event_id -> Future map linking WS card callbacks to acks."""

    def __init__(self):
        self._lock = threading.Lock()
        self._futures: dict[str, concurrent.futures.Future] = {}

    def register(self, event_id: str) -> concurrent.futures.Future:
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            stale = [key for key, item in self._futures.items() if item.done()]
            for key in stale:
                del self._futures[key]
            self._futures[str(event_id)] = future
        return future

    def resolve(self, event_id: str, value: dict[str, Any]) -> bool:
        key = str(event_id)
        if key.startswith("lark:"):
            key = key[len("lark:"):]
        with self._lock:
            future = self._futures.pop(key, None)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True

    def discard(self, event_id: str) -> None:
        with self._lock:
            self._futures.pop(str(event_id), None)


class LarkLiveCaller:
    """Async callable behind ``LarkBotApi``: routes methods to the transport."""

    def __init__(
        self,
        transport: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        ack_registry: AckRegistry | None = None,
    ):
        self._transport = transport
        self._ack_registry = ack_registry

    async def __call__(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "ackCallback":
            resolved = False
            if self._ack_registry is not None:
                resolved = self._ack_registry.resolve(
                    str(payload.get("event_id", "")),
                    {"toast": {"type": "success", "content": "✅ 已收到，正在处理"}},
                )
            return {"ok": True, "resolved": resolved}
        operation = build_operation(method, payload)
        return await asyncio.to_thread(self._transport, operation)


class SdkTransport:
    """Executes operations through lark-oapi. Imports the SDK lazily so every
    unit test (and any deployment without the ``lark`` extra) works untouched."""

    def __init__(self, app_id: str, app_secret: str, domain: str = ""):
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self._client = None

    def _sdk(self):
        try:
            import lark_oapi as lark
            from lark_oapi.api.im import v1 as im_v1
        except ImportError as exc:
            raise TransientDeliveryError(
                "lark-oapi is not installed; reinstall walkcode with the lark extra "
                "(uv tool install walkcode --with lark-oapi)"
            ) from exc
        return lark, im_v1

    def _ensure_client(self):
        if self._client is None:
            lark, _ = self._sdk()
            builder = lark.Client.builder().app_id(self.app_id).app_secret(self.app_secret)
            if self.domain:
                builder = builder.domain(self.domain)
            self._client = builder.build()
        return self._client

    def __call__(self, operation: dict[str, Any]) -> dict[str, Any]:
        kind = operation.get("kind")
        try:
            if kind == "create":
                return self._create(operation)
            if kind == "reply":
                return self._reply(operation)
            if kind == "patch":
                return self._patch(operation)
            if kind == "reaction":
                return self._reaction(operation)
            if kind == "download":
                return self._download(operation)
            if kind == "get_message":
                return self._get_message(operation)
            if kind == "list_thread":
                return self._list_thread(operation)
        except (TransientDeliveryError, PermanentDeliveryError):
            raise
        except _PROGRAMMING_ERRORS:
            raise
        except Exception as exc:
            # Network/DNS/TLS/SDK-transport blips: the outbox retries with backoff.
            raise TransientDeliveryError(f"Lark API call failed: {exc}") from exc
        raise PermanentDeliveryError(f"unknown Lark operation kind: {kind}")

    def _check(self, resp: Any, what: str) -> Any:
        if resp.success():
            return resp
        code = int(getattr(resp, "code", 0) or 0)
        detail = f"{what} failed: {code} {getattr(resp, 'msg', '')}"
        if is_permanent_lark_code(code):
            raise PermanentDeliveryError(detail)
        raise TransientDeliveryError(detail)

    def _create(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        body = (
            im_v1.CreateMessageRequestBody.builder()
            .receive_id(operation["chat_id"])
            .msg_type(operation["msg_type"])
            .content(operation["content"])
            .build()
        )
        request = (
            im_v1.CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )
        resp = self._check(self._ensure_client().im.v1.message.create(request), "Lark create")
        return {"data": {"message_id": resp.data.message_id}}

    def _reply(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        builder = (
            im_v1.ReplyMessageRequestBody.builder()
            .msg_type(operation["msg_type"])
            .content(operation["content"])
        )
        if operation.get("reply_in_thread"):
            builder = builder.reply_in_thread(True)
        request = (
            im_v1.ReplyMessageRequest.builder()
            .message_id(operation["message_id"])
            .request_body(builder.build())
            .build()
        )
        resp = self._check(self._ensure_client().im.v1.message.reply(request), "Lark reply")
        return {"data": {"message_id": resp.data.message_id}}

    def _patch(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        body = im_v1.PatchMessageRequestBody.builder().content(operation["content"]).build()
        request = (
            im_v1.PatchMessageRequest.builder()
            .message_id(operation["message_id"])
            .request_body(body)
            .build()
        )
        self._check(self._ensure_client().im.v1.message.patch(request), "Lark patch")
        return {"ok": True}

    def _reaction(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        emoji = im_v1.Emoji.builder().emoji_type(operation["emoji_type"]).build()
        body = im_v1.CreateMessageReactionRequestBody.builder().reaction_type(emoji).build()
        request = (
            im_v1.CreateMessageReactionRequest.builder()
            .message_id(operation["message_id"])
            .request_body(body)
            .build()
        )
        self._check(self._ensure_client().im.v1.message_reaction.create(request), "Lark reaction")
        return {"ok": True}

    def _download(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        request = (
            im_v1.GetMessageResourceRequest.builder()
            .message_id(operation["message_id"])
            .file_key(operation["file_key"])
            .type(operation["type"])
            .build()
        )
        resp = self._check(
            self._ensure_client().im.v1.message_resource.get(request), "Lark download"
        )
        return {
            "content": resp.file.read(),
            "file_name": str(getattr(resp, "file_name", "") or ""),
        }

    def _get_message(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        request = (
            im_v1.GetMessageRequest.builder()
            .message_id(operation["message_id"])
            .build()
        )
        resp = self._check(self._ensure_client().im.v1.message.get(request), "Lark get message")
        items = getattr(getattr(resp, "data", None), "items", None) or []
        return {"data": {"items": [message_item_to_dict(item) for item in items]}}

    def _list_thread(self, operation: dict[str, Any]) -> dict[str, Any]:
        _, im_v1 = self._sdk()
        builder = (
            im_v1.ListMessageRequest.builder()
            .container_id_type("thread")
            .container_id(operation["container_id"])
            .sort_type("ByCreateTimeAsc")
            .page_size(operation["page_size"])
        )
        if operation.get("page_token"):
            builder = builder.page_token(operation["page_token"])
        resp = self._check(
            self._ensure_client().im.v1.message.list(builder.build()), "Lark list thread"
        )
        data = getattr(resp, "data", None)
        items = getattr(data, "items", None) or []
        return {
            "data": {
                "items": [message_item_to_dict(item) for item in items],
                "has_more": bool(getattr(data, "has_more", False)),
                "page_token": str(getattr(data, "page_token", "") or ""),
            }
        }


def build_lark_live_api(
    credentials: dict[str, str],
    options: dict[str, Any],
    *,
    ack_registry: AckRegistry | None = None,
    transport: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
):
    from . import LarkBotApi

    if transport is None:
        transport = SdkTransport(
            app_id=str(credentials.get("app_id", "") or ""),
            app_secret=str(credentials.get("app_secret", "") or ""),
            domain=str(options.get("openapi_domain", "") or ""),
        )
    return LarkBotApi(caller=LarkLiveCaller(transport, ack_registry=ack_registry))


def _attr_or_key(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_plain(obj: Any) -> Any:
    """Best-effort conversion of an SDK event object into plain JSON data."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {key: _to_plain(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(item) for item in obj]
    fields = getattr(obj, "__dict__", None)
    if isinstance(fields, dict):
        return {
            key.lstrip("_"): _to_plain(value)
            for key, value in fields.items()
            if not callable(value)
        }
    return str(obj)


def normalize_message_event(data: Any) -> dict[str, Any]:
    """Shape a p2.im.message.receive_v1 event into the adapter's payload form:
    ``{"event_id": ..., "event": {"message": ..., "sender": ...}}``."""
    header = _attr_or_key(data, "header", {}) or {}
    event = _to_plain(_attr_or_key(data, "event", {})) or {}
    return {
        "event_id": str(_attr_or_key(header, "event_id", "") or ""),
        "event": event if isinstance(event, dict) else {},
    }


def normalize_card_action_event(data: Any) -> dict[str, Any]:
    """Shape a card.action.trigger event into the adapter's callback form.

    The raw event nests message/chat ids under ``context``; the adapter reads
    them from the event top level, so they are lifted here.
    """
    header = _attr_or_key(data, "header", {}) or {}
    event = _to_plain(_attr_or_key(data, "event", {})) or {}
    if not isinstance(event, dict):
        event = {}
    context = event.get("context", {}) if isinstance(event.get("context"), dict) else {}
    operator = event.get("operator", {}) if isinstance(event.get("operator"), dict) else {}
    normalized = dict(event)
    normalized.setdefault("message_id", str(context.get("open_message_id", "") or ""))
    normalized.setdefault("chat_id", str(context.get("open_chat_id", "") or ""))
    normalized.setdefault("operator", operator)
    return {
        "event_id": str(_attr_or_key(header, "event_id", "") or ""),
        "event": normalized,
    }


class LarkIngressBridge:
    """Runs the lark-oapi WebSocket client in a daemon thread and forwards
    events into an asyncio queue owned by the serve loop.

    Both SDK callbacks return promptly: message events are fire-and-forget;
    card actions wait on an AckRegistry future for at most ``ack_timeout``
    seconds so the inline response (toast) still fits Feishu's callback
    window, then fall back to a neutral toast.
    """

    def __init__(
        self,
        credentials: dict[str, str],
        options: dict[str, Any],
        *,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict[str, Any]]",
        ack_registry: AckRegistry,
        ack_timeout: float = DEFAULT_ACK_TIMEOUT,
        ws_client_factory: Callable[..., Any] | None = None,
        reconnect_delay: float = 5.0,
    ):
        self.credentials = credentials
        self.options = options
        self.loop = loop
        self.queue = queue
        self.ack_registry = ack_registry
        self.ack_timeout = ack_timeout
        self.reconnect_delay = reconnect_delay
        self._ws_client_factory = ws_client_factory
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def _enqueue(self, payload: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, payload)

    def on_message(self, data: Any) -> None:
        # Must not block: heavy work in this callback stalls the SDK event
        # loop, drops WS heartbeats, and the reconnect redelivers the event.
        self._enqueue(normalize_message_event(data))

    def on_card_action(self, data: Any) -> dict[str, Any]:
        payload = normalize_card_action_event(data)
        event_id = payload.get("event_id", "")
        future = self.ack_registry.register(event_id)
        self._enqueue(payload)
        try:
            return future.result(timeout=self.ack_timeout)
        except concurrent.futures.TimeoutError:
            self.ack_registry.discard(event_id)
            return {"toast": {"type": "info", "content": "正在处理…"}}

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_ws, name="walkcode-lark-ws", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _run_ws(self) -> None:
        # The lark-oapi sync WS client captures the current event loop when it
        # is constructed. Building it on the serve loop's thread would make the
        # SDK call run_until_complete on the already-running loop ("This event
        # loop is already running"), so both construction and start() happen on
        # this thread with a fresh private loop.
        #
        # start() returns (or raises) when the connection dies — e.g. after the
        # machine sleeps, the SDK logs "receive message loop exit" and gives up.
        # Without this loop the ingress silently stays dead until a restart.
        while not self._stopped.is_set():
            asyncio.set_event_loop(asyncio.new_event_loop())
            try:
                client = self._build_ws_client()
                client.start()
            except Exception as exc:
                print(
                    f"lark WS connection error: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
            if self._stopped.is_set():
                break
            print(
                f"lark WS disconnected; reconnecting in {self.reconnect_delay:.0f}s",
                file=sys.stderr,
            )
            self._stopped.wait(self.reconnect_delay)

    def _build_ws_client(self) -> Any:
        if self._ws_client_factory is not None:
            return self._ws_client_factory(self)
        try:
            import lark_oapi as lark
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
        except ImportError as exc:
            raise RuntimeError(
                "lark-oapi is required for Lark live ingress; reinstall walkcode "
                "with the lark extra (uv tool install walkcode --with lark-oapi)"
            ) from exc

        def _on_card_action(data: Any) -> Any:
            return P2CardActionTriggerResponse(self.on_card_action(data))

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self.on_message)
            .register_p2_card_action_trigger(_on_card_action)
            .build()
        )
        kwargs: dict[str, Any] = {"event_handler": handler}
        domain = str(self.options.get("openapi_domain", "") or "")
        if domain:
            kwargs["domain"] = domain
        return lark.ws.Client(
            str(self.credentials.get("app_id", "") or ""),
            str(self.credentials.get("app_secret", "") or ""),
            **kwargs,
        )
