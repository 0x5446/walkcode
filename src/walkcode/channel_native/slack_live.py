"""Live Slack API layer for the channel-native V3 runtime (ADR 0070).

- ``SlackWebApi`` runs Web API calls through ``slack_sdk.WebClient`` off the
  event loop and maps Slack errors onto the outbox's delivery contract:
  rate limits and "may have happened" failures are transient, content and
  permission errors are permanent.
- ``SlackIngressBridge`` runs the Socket Mode client in a daemon thread. Each
  envelope is persisted to the runtime inbox BEFORE it is acknowledged; a
  failed write leaves it unacknowledged so Slack redelivers it. Ordering is
  kept by a single listener worker.

``slack_sdk`` is imported lazily inside the builders: a broken install must
surface as "channel unavailable", not as an import error of the runtime.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .models import PermanentDeliveryError, TransientDeliveryError

# Errors where retrying the same request fails the same way.
PERMANENT_SLACK_ERRORS = frozenset(
    {
        "account_inactive",
        "cant_update_message",
        "channel_not_found",
        "edit_window_closed",
        "invalid_arguments",
        "invalid_auth",
        "invalid_blocks",
        "invalid_blocks_format",
        "is_archived",
        "message_not_found",
        "missing_scope",
        "msg_blocks_too_long",
        "msg_too_long",
        "no_permission",
        "no_text",
        "not_allowed_token_type",
        "not_authed",
        "not_in_channel",
        "restricted_action",
        "team_access_not_granted",
        "token_revoked",
        "too_many_attachments",
    }
)
# Failures after which the request may or may not have taken effect.
UNCERTAIN_SLACK_ERRORS = frozenset(
    {"fatal_error", "internal_error", "request_timeout", "service_unavailable"}
)

# Methods whose arguments go as query/form params rather than a JSON body.
_FORM_METHODS = ("auth.", "conversations.", "files.info", "users.")

DOWNLOAD_CHUNK = 1024 * 1024


class SlackUncertainDeliveryError(TransientDeliveryError):
    """The request may have been applied; a blind retry could duplicate it."""


class SlackWebApi:
    def __init__(self, bot_token: str, *, client: Any | None = None, timeout: float = 30.0):
        self.bot_token = bot_token
        self._timeout = timeout
        if client is None:
            from slack_sdk import WebClient

            client = WebClient(token=bot_token, timeout=int(timeout))
        self._client = client
        self._identity: tuple[str, str] | None = None
        self._identity_lock = threading.Lock()

    async def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.call_sync, method, payload)

    def call_sync(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        from slack_sdk.errors import SlackApiError

        args = {key: value for key, value in payload.items() if value is not None}
        try:
            if method.startswith(_FORM_METHODS):
                response = self._client.api_call(method, params=args)
            else:
                response = self._client.api_call(method, json=args)
            return dict(response.data) if hasattr(response, "data") else dict(response)
        except SlackApiError as exc:
            raise _classify_api_error(method, exc) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SlackUncertainDeliveryError(f"slack {method} network error: {type(exc).__name__}: {exc}") from None

    def identity(self) -> tuple[str, str]:
        """(team_id, bot_user_id) of this token, cached after the first call."""
        with self._identity_lock:
            if self._identity is None:
                data = self.call_sync("auth.test", {})
                self._identity = (str(data.get("team_id", "") or ""), str(data.get("user_id", "") or ""))
            return self._identity

    async def download(self, url: str, destination: Path) -> int:
        return await asyncio.to_thread(self._download_sync, url, destination)

    def _download_sync(self, url: str, destination: Path) -> int:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.bot_token}"})
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                content_type = str(response.headers.get("Content-Type", "") or "")
                if content_type.startswith("text/html"):
                    # Slack answers a token it will not honour (missing
                    # files:read, file not shared to the bot) with its login
                    # page, not an HTTP error.
                    raise PermanentDeliveryError(
                        "slack file download returned an HTML page; check the files:read scope "
                        "and that the bot can see this file"
                    )
                total = 0
                with destination.open("wb") as handle:
                    while chunk := response.read(DOWNLOAD_CHUNK):
                        handle.write(chunk)
                        total += len(chunk)
                return total
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403, 404}:
                raise PermanentDeliveryError(f"slack file download HTTP {exc.code}") from None
            raise TransientDeliveryError(f"slack file download HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransientDeliveryError(f"slack file download failed: {type(exc).__name__}: {exc}") from None


def _classify_api_error(method: str, exc: Any) -> Exception:
    response = getattr(exc, "response", None)
    status = int(getattr(response, "status_code", 0) or 0)
    data = getattr(response, "data", {}) or {}
    error = str(data.get("error", "") or "") if isinstance(data, dict) else ""
    message = f"slack {method} failed: {error or status}"
    if status == 429 or error in {"ratelimited", "rate_limited"}:
        headers = getattr(response, "headers", {}) or {}
        try:
            retry_after = float(headers.get("Retry-After", headers.get("retry-after", 1)) or 1)
        except (TypeError, ValueError):
            retry_after = 1.0
        return TransientDeliveryError(message, retry_after=retry_after)
    if error in PERMANENT_SLACK_ERRORS:
        return PermanentDeliveryError(message)
    if error in UNCERTAIN_SLACK_ERRORS or status >= 500:
        return SlackUncertainDeliveryError(message)
    return TransientDeliveryError(message)


def build_slack_live_api(credentials: dict[str, str]) -> SlackWebApi:
    return SlackWebApi(str(credentials.get("bot_token", "") or ""))


def ingress_relevant(envelope: dict[str, Any]) -> bool:
    """Whether a Socket Mode envelope can matter to the runtime.

    Filtering here (before the inbox write) keeps the bot's own messages,
    edits/deletes and unrelated events out of the spool entirely. Messages
    that may or may not be addressed to the bot are kept; the runtime decides
    with the session bindings in hand.
    """
    kind = str(envelope.get("type", "") or "")
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if kind == "interactive":
        team = str((payload.get("team") or {}).get("id", "") or "")
        user_team = str((payload.get("user") or {}).get("team_id", "") or team)
        return str(payload.get("type", "") or "") == "block_actions" and user_team == team
    if kind != "events_api":
        return False
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    # People from another organization in a Slack Connect channel never drive
    # this bot: its allowlists are written in this workspace's user ids.
    sender_team = str(event.get("user_team", "") or event.get("team", "") or "")
    if sender_team and sender_team != str(payload.get("team_id", "") or sender_team):
        return False
    event_type = str(event.get("type", "") or "")
    if event_type not in {"message", "app_mention"}:
        return False
    bot_user_id = _authorized_bot_user_id(payload)
    if event.get("bot_id") or event.get("bot_profile"):
        return False
    if bot_user_id and str(event.get("user", "") or "") == bot_user_id:
        return False
    subtype = str(event.get("subtype", "") or "")
    if subtype and subtype not in {"file_share", "thread_broadcast"}:
        return False
    if event_type == "message" and str(event.get("channel_type", "") or "") != "im":
        # Channel chatter outside any thread can only matter when it mentions
        # the bot; thread replies may continue a bound session.
        mentioned = bool(bot_user_id) and f"<@{bot_user_id}>" in str(event.get("text", "") or "")
        if not event.get("thread_ts") and not mentioned:
            return False
    return True


def _authorized_bot_user_id(payload: dict[str, Any]) -> str:
    for item in payload.get("authorizations") or ():
        if isinstance(item, dict) and item.get("is_bot") and item.get("user_id"):
            return str(item["user_id"])
    return ""


class SlackIngressBridge:
    """Socket Mode client in a daemon thread feeding the serve loop's queue."""

    def __init__(
        self,
        credentials: dict[str, str],
        *,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[Any]",
        persist_event: Callable[[dict[str, Any]], Path] | None = None,
        client_factory: Callable[["SlackIngressBridge"], Any] | None = None,
        reconnect_delay: float = 5.0,
        api: "SlackWebApi | None" = None,
    ):
        self.api = api
        self.credentials = credentials
        self.reconnect_delay = reconnect_delay
        self.loop = loop
        self.queue = queue
        self._persist_event = persist_event
        self._client_factory = client_factory
        self._client: Any = None
        self._thread: threading.Thread | None = None
        self._stopped = threading.Event()

    def on_request(self, client: Any, request: Any) -> None:
        envelope = {
            "type": str(getattr(request, "type", "") or ""),
            "envelope_id": str(getattr(request, "envelope_id", "") or ""),
            "retry_attempt": getattr(request, "retry_attempt", None),
            "payload": getattr(request, "payload", None) or {},
        }
        if ingress_relevant(envelope):
            try:
                item = self._persist_event(envelope) if self._persist_event else envelope
            except Exception as exc:
                # Unacknowledged on purpose: Slack redelivers it.
                print(f"slack inbox write failed, leaving envelope unacked: {type(exc).__name__}: {exc}", file=sys.stderr)
                return
            self.loop.call_soon_threadsafe(self.queue.put_nowait, item)
        if envelope["envelope_id"]:
            from slack_sdk.socket_mode.response import SocketModeResponse

            client.send_socket_mode_response(SocketModeResponse(envelope_id=envelope["envelope_id"]))

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="walkcode-slack-socket", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        client = self._client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _run(self) -> None:
        # Once connected, the SDK reconnects on its own (auto_reconnect_enabled),
        # including on Slack's periodic refresh_requested disconnects. Only the
        # first connect needs a retry loop here: without it a network blip at
        # startup would leave ingress silently dead until a restart.
        while not self._stopped.is_set():
            try:
                if self.api is not None:
                    # Warm the (team, bot user) cache off the serve loop.
                    self.api.identity()
                if self._client is None:
                    self._client = self._build_client()
                    self._client.socket_mode_request_listeners.append(self.on_request)
                self._client.connect()
                break
            except Exception as exc:
                print(
                    f"slack socket mode connection error: {type(exc).__name__}: {exc}; retrying in "
                    f"{self.reconnect_delay:.0f}s",
                    file=sys.stderr,
                )
                self._stopped.wait(self.reconnect_delay)
        self._stopped.wait()

    def _build_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self)
        try:
            from slack_sdk import WebClient
            from slack_sdk.socket_mode import SocketModeClient
        except ImportError as exc:
            raise RuntimeError("slack-sdk is required for Slack live ingress; reinstall walkcode") from exc
        return SocketModeClient(
            app_token=str(self.credentials.get("app_token", "") or ""),
            web_client=WebClient(token=str(self.credentials.get("bot_token", "") or "")),
            # One worker keeps envelopes in arrival order through the inbox.
            concurrency=1,
        )
