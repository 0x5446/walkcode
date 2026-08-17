import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import walkcode.channel_native as walkcode_channel_native
from walkcode.channel_native import (
    EMPTY_TURN_PLACEHOLDER,
    ActorRef,
    AgentEventType,
    AttachmentRef,
    AuthorizationStore,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelCapabilities,
    CodexAppServerTransport,
    DurableOutbox,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    LaunchSpec,
    Orchestrator,
    SessionRegistry,
    TransportUnavailable,
    TurnInput,
)
from walkcode.channel_native_runtime import (
    CodexManagedAppServerClient,
    CodexStdioAppServerClient,
    _notification_matches_thread,
    _notification_thread_id,
    _read_websocket_frame,
    _websocket_frame,
)


def _drain_events(transport, handle, stop_after=None):
    """Consume the transport's event generator to exhaustion.

    ``events()`` is a persistent listener: it re-enters the bounded collector
    while the turn stays open. Tests hand it a fake client whose batches run
    dry, so the silence ceiling is pinned to 0 at construction — the listen
    then ends on the first empty batch instead of waiting an hour.
    """

    async def run():
        collected = []
        async for event in transport.events(handle):
            collected.append(event)
            if stop_after is not None and len(collected) >= stop_after:
                break
        return collected

    return asyncio.run(run())


class _FakeCodexClient:
    def __init__(self):
        self.requests = []
        self.responses = []
        self.event_batches = {}

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        return {}

    async def events(self, thread_id):
        return self.event_batches.pop(thread_id, [])

    async def answer_request(self, request_id, result):
        self.responses.append((request_id, result))


class _IdleGapCodexStdioClient(CodexStdioAppServerClient):
    def __init__(self):
        super().__init__(request_timeout=1, event_timeout=1, event_idle_timeout=0.01)
        self.messages = [
            {"method": "turn/started", "params": {"threadId": "thread-1"}},
            TimeoutError(),
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "delta": "OK"},
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]

    async def _ensure_started(self):
        if not self._reader_alive():
            self._start_reader()

    async def _read_message(self, *, timeout):
        if not self.messages:
            # Idle wire: the resident reader blocks here in production.
            await asyncio.sleep(0.01)
            raise TimeoutError()
        message = self.messages.pop(0)
        if isinstance(message, Exception):
            # A scripted idle gap between two real messages.
            await asyncio.sleep(0.01)
            raise message
        return message


class _EventMsgCodexStdioClient(CodexStdioAppServerClient):
    def __init__(self):
        super().__init__(request_timeout=1, event_timeout=0.05, event_idle_timeout=0.01)
        self.messages = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "walkcode-codex-ok",
                    "phase": "final_answer",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "walkcode-codex-ok",
                },
            },
        ]

    async def _ensure_started(self):
        if not self._reader_alive():
            self._start_reader()

    async def _read_message(self, *, timeout):
        if not self.messages:
            await asyncio.sleep(0.01)
            raise TimeoutError()
        return self.messages.pop(0)


class _ServerRequestCodexStdioClient(CodexStdioAppServerClient):
    def __init__(self):
        super().__init__(request_timeout=1, event_timeout=1, event_idle_timeout=0.01)
        self.messages = [
            {
                "id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "startedAtMs": 1,
                    "environmentId": None,
                    "command": "rm -rf build",
                    "cwd": "/tmp/project",
                    "availableDecisions": ["accept", "decline", "cancel"],
                },
            }
        ]

    async def _ensure_started(self):
        if not self._reader_alive():
            self._start_reader()

    async def _read_message(self, *, timeout):
        if not self.messages:
            await asyncio.sleep(0.01)
            raise TimeoutError()
        return self.messages.pop(0)


class _ManagedClientNoProcess(CodexManagedAppServerClient):
    def __init__(self):
        super().__init__(socket_path="/tmp/codex.sock")
        self.started = 0
        self.sent = []

    async def _start_daemon(self):
        self.started += 1

    async def _ensure_started(self):
        if not self._daemon_checked:
            await self._start_daemon()
            self._daemon_checked = True

    async def _send(self, message):
        self.sent.append(message)
        # No wire and no reader here: answer the request the way the resident
        # reader would, so request() resolves its future.
        if "id" in message:
            self._dispatch({"id": message["id"], "result": {"thread": {"id": "thread-managed"}}})

    async def _read_response(self, request_id, *, timeout):
        return {"id": request_id, "result": {"thread": {"id": "thread-managed"}}}


class _ManagedClientFakeDaemon(CodexManagedAppServerClient):
    def __init__(self, *, socket_path: str):
        super().__init__(socket_path=socket_path, request_timeout=1, event_timeout=1, event_idle_timeout=0.01)
        self.started = 0

    async def _start_daemon(self):
        self.started += 1


async def _managed_websocket_smoke():
    socket_path = f"/tmp/walkcode-codex-{uuid.uuid4().hex}.sock"
    messages = []
    handshakes = []

    async def handle_client(reader, writer):
        try:
            request = (await reader.readuntil(b"\r\n\r\n")).decode("iso-8859-1", errors="replace")
            handshakes.append(request)
            key = ""
            for line in request.splitlines():
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
                    break
            accept = base64.b64encode(
                hashlib.sha1((key + CodexManagedAppServerClient._WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            writer.write(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            while True:
                opcode, payload = await _read_websocket_frame(reader)
                if opcode == 0x8:
                    break
                if opcode != 0x1:
                    continue
                message = json.loads(payload.decode("utf-8"))
                messages.append(message)
                if message.get("method") == "initialize":
                    response = {"id": message["id"], "result": {"userAgent": "codex-test", "codexHome": "/tmp"}}
                    writer.write(_websocket_frame(0x1, json.dumps(response).encode("utf-8"), masked=False))
                    await writer.drain()
                elif message.get("method") == "thread/start":
                    response = {"id": message["id"], "result": {"thread": {"id": "thread-ws"}}}
                    writer.write(_websocket_frame(0x1, json.dumps(response).encode("utf-8"), masked=False))
                    await writer.drain()
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    try:
        server = await asyncio.start_unix_server(handle_client, path=socket_path)
    except Exception:
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        raise
    try:
        client = _ManagedClientFakeDaemon(socket_path=socket_path)
        result = await client.request("thread/start", {"cwd": "/tmp/project"})
        client._close_websocket()
        return result, client.started, handshakes, messages
    finally:
        server.close()
        await server.wait_closed()
        if os.path.exists(socket_path):
            os.unlink(socket_path)


def _actor(actor_id="owner"):
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


def _binding():
    return ChannelBinding("telegram", "bot", "chat", "topic", "root")


def _channel_caps():
    return ChannelCapabilities(
        thread_context=True,
        editable_message=True,
        interactive_message=True,
        interactive_update=True,
        private_callback_ack=True,
        toast_or_ephemeral_notice=True,
        force_reply=True,
        attachment_download=True,
        forum_or_topic=True,
        max_text_chars=4096,
        max_callback_payload_bytes=64,
    )


def _callback(token: str, *, actor_id: str = "owner") -> InboundEvent:
    return InboundEvent(
        event_id=f"cb-{actor_id}-{token[:4]}",
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id=f"m-{actor_id}",
        root_message_id="root",
        sender_id=actor_id,
        sender_display=actor_id.title(),
        text=f"cb:{token}",
        callback={"token": token},
    )


def _token_for(view: dict, action: str) -> str:
    return next(item["token"] for item in view["actions"] if item["action"] == action)


class CodexAppServerTransportTests(unittest.TestCase):
    def test_managed_client_starts_daemon_once(self):
        client = _ManagedClientNoProcess()

        first = asyncio.run(client.request("thread/start", {"cwd": "/tmp/project"}))
        second = asyncio.run(client.request("thread/resume", {"threadId": "thread-managed"}))

        self.assertEqual(client.socket_path, "/tmp/codex.sock")
        self.assertEqual(client.started, 1)
        self.assertEqual(first["thread"]["id"], "thread-managed")
        self.assertEqual(second["thread"]["id"], "thread-managed")

    def test_managed_client_talks_to_unix_websocket_control_socket(self):
        result, started, handshakes, messages = asyncio.run(_managed_websocket_smoke())

        self.assertEqual(result["thread"]["id"], "thread-ws")
        self.assertEqual(started, 1)
        self.assertEqual(len(handshakes), 1)
        self.assertIn("Upgrade: websocket", handshakes[0])
        self.assertEqual(
            [message.get("method") for message in messages],
            ["initialize", "initialized", "thread/start"],
        )

    def test_stdio_events_wait_through_idle_gap_until_turn_completed(self):
        client = _IdleGapCodexStdioClient()

        events = asyncio.run(client.events("thread-1"))

        self.assertEqual([event["method"] for event in events], [
            "turn/started",
            "item/agentMessage/delta",
            "turn/completed",
        ])

    def test_stdio_events_accept_event_msg_task_complete(self):
        client = _EventMsgCodexStdioClient()

        events = asyncio.run(client.events("thread-1"))

        self.assertEqual([event["payload"]["type"] for event in events], [
            "agent_message",
            "task_complete",
        ])

    def test_stdio_events_return_hitl_server_request_without_waiting_for_turn_completed(self):
        client = _ServerRequestCodexStdioClient()

        events = asyncio.run(client.events("thread-1"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["id"], "approval-1")
        self.assertEqual(events[0]["method"], "item/commandExecution/requestApproval")

    def test_launch_and_submit_use_app_server_shapes(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "idem-1"))

        self.assertEqual(client.requests[0][0], "thread/start")
        self.assertEqual(client.requests[0][1]["cwd"], "/tmp/project")
        self.assertFalse(client.requests[0][1]["ephemeral"])
        self.assertEqual(client.requests[1][0], "turn/start")
        self.assertEqual(client.requests[1][1]["threadId"], "thread-1")
        self.assertEqual(
            client.requests[1][1]["input"],
            [{"type": "text", "text": "hello", "text_elements": []}],
        )
        self.assertEqual(client.requests[1][1]["idempotencyKey"], "idem-1")

    def test_shutdown_interrupts_the_live_turn_then_unsubscribes(self):
        # Closing has to stop the WORK, not just the watching: one app-server
        # serves every thread under this CODEX_HOME, so there is no process to
        # kill — unsubscribing alone would leave the agent running with
        # whatever sandbox it holds.
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "idem-1"))
        result = asyncio.run(transport.shutdown(handle, "graceful"))

        self.assertTrue(result.accepted)
        methods = [method for method, _params in client.requests]
        self.assertEqual(methods[-2:], ["turn/interrupt", "thread/unsubscribe"])
        self.assertEqual(
            client.requests[-2][1],
            {"threadId": "thread-1", "turnId": "turn-1"},
        )
        self.assertEqual(client.requests[-1][1], {"threadId": "thread-1"})

    def test_shutdown_between_turns_skips_the_interrupt(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        result = asyncio.run(transport.shutdown(handle, "graceful"))

        self.assertTrue(result.accepted)
        self.assertEqual(
            [method for method, _params in client.requests],
            ["thread/start", "thread/unsubscribe"],
        )

    def test_shutdown_still_closes_when_the_server_refuses(self):
        # A wedged daemon must not pin the session at "running" with no way
        # for the user to end it.
        class _AngryClient(_FakeCodexClient):
            async def request(self, method, params):
                await super().request(method, params)
                if method in {"turn/interrupt", "thread/unsubscribe"}:
                    raise TransportUnavailable("app-server is gone")
                if method == "thread/start":
                    return {"thread": {"id": "thread-1"}}
                return {}

        client = _AngryClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        result = asyncio.run(transport.shutdown(handle, "graceful"))

        self.assertTrue(result.accepted)
        self.assertEqual(result.state, "stopped")

    def test_released_thread_ends_a_parked_listener_with_a_closed_turn(self):
        # A drain sitting in client.events() when the close lands must not wait
        # out the silence ceiling, and must not end bare — a bare stream end
        # reads as a mid-turn failure.
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=3600)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "idem-1"))
        asyncio.run(transport.shutdown(handle, "graceful"))
        events = _drain_events(transport, handle)

        self.assertEqual([event.type for event in events], [AgentEventType.TURN_COMPLETED])

    def test_restart_backend_replaces_the_process_and_drops_stale_caches(self):
        # /reload's reason for existing: the app-server snapshots mcp_servers
        # at process start, so a config edit only reaches existing threads
        # after the process is replaced. Per-thread caches describe the OLD
        # process and must not survive it.
        class _RestartableClient(_FakeCodexClient):
            def __init__(self):
                super().__init__()
                self.restarts = 0

            async def restart(self):
                self.restarts += 1

        client = _RestartableClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "idem-1"))
        transport._thread_models["thread-1"] = "gpt-5.6-sol"
        transport.effective_sandbox["thread-1"] = "readOnly"

        asyncio.run(transport.restart_backend())

        self.assertEqual(client.restarts, 1)
        self.assertEqual(transport._thread_models, {})
        self.assertEqual(transport._active_turns, {})
        self.assertEqual(transport.effective_sandbox, {})

    def test_restart_backend_keeps_the_release_mark_for_parked_drains(self):
        # _released_threads is a signal to drains, not a cache of the old
        # process. Clearing it lets a drain that returns from a batch just
        # after the restart fall through to client.events(), which respawns an
        # app-server and then listens on a thread nobody resumed there —
        # empty batches until the hour-long silence ceiling.
        class _RestartableClient(_FakeCodexClient):
            async def restart(self):
                return None

        transport = CodexAppServerTransport(
            client=_RestartableClient(), event_silence_ceiling=0
        )
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.shutdown(handle, "graceful"))
        self.assertIn("thread-1", transport._released_threads)

        asyncio.run(transport.restart_backend())

        self.assertIn("thread-1", transport._released_threads)
        # And a genuine comeback still clears it.
        asyncio.run(transport.resume_thread("thread-1", cwd="/tmp/project"))
        self.assertNotIn("thread-1", transport._released_threads)

    def test_restart_backend_keeps_the_env_context_mark(self):
        # The channel preamble lives in the thread's HISTORY, not in the
        # server. Re-marking it would re-inject the same block on every resume
        # after a reload.
        class _RestartableClient(_FakeCodexClient):
            async def restart(self):
                return None

        transport = CodexAppServerTransport(
            client=_RestartableClient(), event_silence_ceiling=0, environment_context="CTX"
        )
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="hello"), "idem-1"))
        self.assertIn("thread-1", transport._env_context_delivered)

        asyncio.run(transport.restart_backend())

        self.assertIn("thread-1", transport._env_context_delivered)

    def test_restart_backend_refuses_a_client_that_cannot_restart(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        with self.assertRaises(CapabilityUnsupported):
            asyncio.run(transport.restart_backend())

    def test_resume_clears_a_stale_release_mark(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.shutdown(handle, "graceful"))
        asyncio.run(transport.resume_thread("thread-1", cwd="/tmp/project"))

        self.assertNotIn("thread-1", transport._released_threads)

    def test_submit_turn_carries_attachment_paths(self):
        # An attachment-only message used to reach codex as text "" — the
        # image was dropped AND the blank user message poisoned the thread.
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(
            transport.submit_turn(
                handle,
                TurnInput(
                    text="",
                    attachments=[
                        AttachmentRef(
                            source_id="img-1",
                            mime="image/png",
                            local_path="/tmp/walkcode-attachments/a.png",
                        )
                    ],
                ),
                "idem-attach",
            )
        )

        text = client.requests[1][1]["input"][0]["text"]
        self.assertIn("/tmp/walkcode-attachments/a.png", text)
        self.assertTrue(text.strip())

    def test_submit_turn_never_sends_empty_text(self):
        # `400 user message must have content` from the relay's Chat
        # Completions upstream bricks the thread for every later turn.
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text="   "), "idem-blank"))

        self.assertEqual(
            client.requests[1][1]["input"],
            [{"type": "text", "text": EMPTY_TURN_PLACEHOLDER, "text_elements": []}],
        )

    def test_submit_turn_with_env_context_still_never_blank(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(
            client=client, event_silence_ceiling=0, environment_context="CTX"
        )

        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))
        asyncio.run(transport.submit_turn(handle, TurnInput(text=""), "idem-ctx"))

        text = client.requests[1][1]["input"][0]["text"]
        self.assertIn("CTX", text)
        self.assertIn(EMPTY_TURN_PLACEHOLDER, text)

    def test_error_notification_while_retrying_becomes_a_diagnostic_note(self):
        # app-server v2 `error` notification. willRetry=True means codex is
        # still backing off — the note belongs on the progress card, and it
        # must NOT count as the agent having answered.
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": True,
                    "error": {
                        "message": "user message must have content",
                        "codexErrorInfo": {"responseStreamConnectionFailed": {"httpStatusCode": 400}},
                    },
                },
            },
            thread_id="thread-1",
        )

        self.assertEqual(event.type, AgentEventType.TURN_NARRATION)
        self.assertTrue(event.payload["diagnostic"])
        self.assertIn("响应流连接失败", event.payload["text"])
        self.assertIn("HTTP 400", event.payload["text"])
        self.assertIn("user message must have content", event.payload["text"])

    def test_error_notification_after_retries_becomes_a_session_error(self):
        # willRetry=False: the turn is lost. Without this the turn just ends
        # in silence — the 2026-08-07 outage verbatim.
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(
            {
                "method": "error",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "willRetry": False,
                    "error": {
                        "message": "retry limit reached",
                        "codexErrorInfo": "responseTooManyFailedAttempts",
                    },
                },
            },
            thread_id="thread-1",
        )

        self.assertEqual(event.type, AgentEventType.SESSION_ERROR)
        self.assertEqual(event.payload["reason"], "codex_turn_error")
        self.assertIn("重试次数耗尽", event.payload["message"])
        self.assertEqual(event.payload["turn_id"], "turn-1")

    def test_error_notification_without_codex_error_info_still_reports(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(
            {"method": "error", "params": {"willRetry": False, "error": {"message": "boom"}}},
            thread_id="thread-1",
        )

        self.assertEqual(event.type, AgentEventType.SESSION_ERROR)
        self.assertIn("boom", event.payload["message"])

    # Captured verbatim off a live `codex app-server --stdio` (codex 0.144.5)
    # whose upstream answered 403 MODEL_NOT_IN_PLAN — six `error`
    # notifications, five retrying and one terminal. Recorded 2026-08-07 so
    # the parser is pinned to the real wire shape, not to the schema alone
    # (docs/review/.review-learnings.md: a fake client proves nothing about
    # the protocol).
    _LIVE_RETRY_ERROR = {
        "method": "error",
        "params": {
            "error": {
                "message": "Reconnecting... 5/5",
                "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": None}},
                "additionalDetails": (
                    'stream disconnected before completion: {"error":{"message":'
                    '"MODEL_NOT_IN_PLAN: GPT-5.6 Sol available in Pro and above plans '
                    'or extra on demand usage","type":"permission_error","code":"FORBIDDEN"}}'
                ),
            },
            "willRetry": True,
            "threadId": "019fdbff-710c-7e72-98dd-3fea57646a9f",
            "turnId": "019fdbff-7afb-7431-85c9-6a82a3a00521",
        },
    }
    _LIVE_TERMINAL_ERROR = {
        "method": "error",
        "params": {
            "error": {
                "message": (
                    'stream disconnected before completion: {"error":{"message":'
                    '"MODEL_NOT_IN_PLAN: GPT-5.6 Sol available in Pro and above plans '
                    'or extra on demand usage","type":"permission_error","code":"FORBIDDEN"}}'
                ),
                "codexErrorInfo": "other",
                "additionalDetails": None,
            },
            "willRetry": False,
            "threadId": "019fdbff-710c-7e72-98dd-3fea57646a9f",
            "turnId": "019fdbff-7afb-7431-85c9-6a82a3a00521",
        },
    }

    def test_live_captured_retry_error_is_parsed(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(dict(self._LIVE_RETRY_ERROR), thread_id="t")

        self.assertEqual(event.type, AgentEventType.TURN_NARRATION)
        self.assertTrue(event.payload["diagnostic"])
        self.assertIn("响应流中断", event.payload["text"])
        self.assertIn("Reconnecting... 5/5", event.payload["text"])
        # httpStatusCode is null here — no phantom "HTTP None".
        self.assertNotIn("HTTP", event.payload["text"])

    def test_live_captured_terminal_error_is_parsed(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(dict(self._LIVE_TERMINAL_ERROR), thread_id="t")

        self.assertEqual(event.type, AgentEventType.SESSION_ERROR)
        # codexErrorInfo "other" carries no label; the message must survive.
        self.assertIn("MODEL_NOT_IN_PLAN", event.payload["message"])
        self.assertIn("本轮失败", event.payload["message"])

    def test_error_variants_render_predictably(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())
        cases = [
            ("unauthorized", None, "鉴权失败", ""),
            ("usageLimitExceeded", None, "用量超限", ""),
            ("contextWindowExceeded", None, "上下文超限", ""),
            ({"httpConnectionFailed": {"httpStatusCode": 502}}, None, "连接失败", "HTTP 502"),
            (
                {"responseTooManyFailedAttempts": {"httpStatusCode": 429}},
                None,
                "重试次数耗尽",
                "HTTP 429",
            ),
            ({"activeTurnNotSteerable": {"turnKind": "review"}}, None, "当前回合不可插话", ""),
            ("someFutureVariant", None, "someFutureVariant", ""),
            (None, "plain failure", "plain failure", ""),
        ]
        for info, message, expected, status in cases:
            with self.subTest(info=info):
                event = transport._convert_event(
                    {
                        "method": "error",
                        "params": {
                            "willRetry": False,
                            "error": {"message": message or "", "codexErrorInfo": info},
                        },
                    },
                    thread_id="t",
                )
                text = event.payload["message"]
                self.assertIn(expected, text)
                if status:
                    self.assertIn(status, text)

    def test_error_with_nothing_usable_still_says_something(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(
            {"method": "error", "params": {"willRetry": False}}, thread_id="t"
        )

        self.assertEqual(event.type, AgentEventType.SESSION_ERROR)
        self.assertIn("未描述的错误", event.payload["message"])

    def test_long_upstream_message_keeps_the_http_status_visible(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient())

        event = transport._convert_event(
            {
                "method": "error",
                "params": {
                    "willRetry": True,
                    "error": {
                        "message": "x" * 1500,
                        "codexErrorInfo": {"httpConnectionFailed": {"httpStatusCode": 429}},
                    },
                },
            },
            thread_id="t",
        )

        text = event.payload["text"]
        self.assertIn("HTTP 429", text[:60])
        self.assertLess(len(text), 400)

    def test_unhandled_event_types_are_logged_once_each(self):
        # Everything unrecognised used to vanish without a trace, which is how
        # a whole class of "codex said it, nobody listened" stayed invisible.
        transport = CodexAppServerTransport(client=_FakeCodexClient())
        logged = []
        with patch.object(walkcode_channel_native, "_log_degrade",
                          lambda event, **kw: logged.append((event, kw))):
            for _ in range(3):
                transport._convert_event({"method": "thread/tokenUsage/updated"}, thread_id="t")
            transport._convert_event(
                {"type": "event_msg", "payload": {"type": "sub_agent_activity"}}, thread_id="t"
            )

        self.assertEqual(
            [kw["event_type"] for _, kw in logged],
            ["thread/tokenUsage/updated", "event_msg/sub_agent_activity"],
        )
        self.assertTrue(all(name == "codex_event_type_unhandled" for name, _ in logged))

    def test_events_convert_delta_and_completed(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {"method": "mcpServer/startupStatus/updated", "params": {"threadId": "thread-1"}},
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "item-1", "delta": "hi"},
            },
            {"method": "thread/tokenUsage/updated", "params": {"threadId": "thread-1"}},
            {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "hi")
        self.assertEqual(events[1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[1].payload["status"], "completed")

    def test_events_convert_codex_event_msg_agent_message_and_task_complete(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "message": "walkcode-codex-ok",
                    "phase": "final_answer",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "turn-1",
                    "last_agent_message": "walkcode-codex-ok",
                    "duration_ms": 1234,
                },
            },
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "walkcode-codex-ok")
        self.assertEqual(events[1].type, AgentEventType.TURN_COMPLETED)
        self.assertEqual(events[1].payload["message"], "walkcode-codex-ok")
        self.assertEqual(events[1].payload["status"], "completed")

    def test_events_coalesce_codex_delta_fragments(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "delta": "walkcode"},
            },
            {
                "method": "item/agentMessage/delta",
                "params": {"threadId": "thread-1", "delta": "-ok"},
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "walkcode-ok")
        self.assertEqual(events[1].type, AgentEventType.TURN_COMPLETED)

    def test_events_convert_tool_lifecycle_without_full_output(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "method": "item/toolCall/started",
                "params": {
                    "threadId": "thread-1",
                    "itemId": "tool-1",
                    "toolName": "shell",
                    "arguments": {"cmd": "ls -la"},
                },
            },
            {
                "method": "item/toolCall/completed",
                "params": {
                    "threadId": "thread-1",
                    "itemId": "tool-1",
                    "toolName": "shell",
                    "output": "large output that should not be surfaced",
                },
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "shell")
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("large output", events[1].payload["summary"])
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_events_convert_command_execution_items_without_full_output(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "commandExecution",
                        "id": "cmd-1",
                        "command": "ls -la",
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "commandExecution",
                        "id": "cmd-1",
                        "command": "ls -la",
                        "output": "large output that should not be surfaced",
                    },
                },
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "command")
        self.assertIn("ls -la", events[0].payload["summary"])
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertNotIn("large output", events[1].payload["summary"])
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_events_convert_web_search_items(self):
        """`webSearch` is tool activity even though its name shares no root.

        The tool-like probe looks for tool/function/command/exec/shell/bash and
        `webSearch` hits none of them, so a server-executed search produced no
        card at all — just a one-shot `codex_event_type_unhandled` line. Item
        shape per `codex app-server generate-json-schema`: {id, query, action?}.
        """
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "webSearch",
                        "id": "ws-1",
                        "query": "InfLoRA continual learning",
                        "action": None,
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "webSearch",
                        "id": "ws-1",
                        "query": "InfLoRA continual learning",
                        "action": {"type": "search", "query": "InfLoRA continual learning"},
                    },
                },
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[0].payload["tool_name"], "web_search")
        self.assertIn("InfLoRA continual learning", events[0].payload["summary"])
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertEqual(events[1].payload["tool_id"], "ws-1")
        self.assertEqual(events[1].payload["tool_name"], "web_search")
        # The completion must keep the query. The progress card upserts by
        # tool_id, so a generic "Tool completed" here overwrites what the user
        # was reading and the finished card says nothing about the search.
        self.assertIn("InfLoRA continual learning", events[1].payload["summary"])
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_events_convert_file_change_items_with_paths_not_diffs(self):
        """`fileChange` gets a card too, and the card must not carry the patch."""
        client = _FakeCodexClient()
        changes = [
            {"path": "src/a.py", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-old\n+new"},
            {"path": "src/b.py", "kind": {"type": "add"}, "diff": "@@ -0,0 +1 @@\n+brand new"},
        ]
        client.event_batches["thread-1"] = [
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "inProgress",
                        "changes": changes,
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "completed",
                        "changes": changes,
                    },
                },
            },
            {"method": "turn/completed", "params": {"threadId": "thread-1"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        for event in events[:2]:
            self.assertEqual(event.payload["tool_name"], "apply_patch")
            self.assertIn("src/a.py", event.payload["summary"])
            self.assertIn("src/b.py", event.payload["summary"])
            self.assertNotIn("brand new", event.payload["summary"])
            self.assertNotIn("@@", event.payload["summary"])
        self.assertEqual(events[0].type, AgentEventType.TOOL_STARTED)
        self.assertEqual(events[1].type, AgentEventType.TOOL_COMPLETED)
        self.assertEqual(events[2].type, AgentEventType.TURN_COMPLETED)

    def test_declined_file_change_is_a_failed_card_not_a_completed_one(self):
        """codex reports a rejected patch as `item/completed` + status declined.

        Going by the method name alone turned "the user said no" into a green
        card claiming the edit landed.
        """
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        event = transport._convert_event(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "declined",
                        "changes": [
                            {"path": "src/a.py", "kind": {"type": "update"}, "diff": "@@"}
                        ],
                    },
                },
            },
            thread_id="thread-1",
        )

        self.assertEqual(event.type, AgentEventType.TOOL_FAILED)
        self.assertEqual(event.payload["tool_name"], "apply_patch")
        # Which patch was rejected is the first thing anyone asks.
        self.assertIn("src/a.py", event.payload["summary"])

    def test_bulk_file_change_summary_counts_the_files_it_cannot_show(self):
        """Truncating a joined path list hides how big the edit was."""
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        event = transport._convert_event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "inProgress",
                        "changes": [
                            {"path": f"src/module_{n:02d}.py", "kind": {"type": "update"}, "diff": "@@"}
                            for n in range(50)
                        ],
                    },
                },
            },
            thread_id="thread-1",
        )

        summary = event.payload["summary"]
        self.assertIn("50 files", summary)
        self.assertIn("+45 more", summary)
        self.assertLessEqual(len(summary), 160)
        self.assertNotIn("...", summary)

    def test_long_paths_keep_the_file_count_instead_of_being_chopped(self):
        """The count must be budgeted for, not appended and hoped for.

        With deep paths, a "+N more" tail computed outside the summary limit
        gets truncated away — leaving a card that both cuts a path mid-word and
        hides how big the edit was.
        """
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        long_paths = [
            f"services/backend/internal/domain/scheduling/handlers/very_long_handler_{n:02d}.py"
            for n in range(12)
        ]
        event = transport._convert_event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "inProgress",
                        "changes": [
                            {"path": path, "kind": {"type": "update"}, "diff": "@@"}
                            for path in long_paths
                        ],
                    },
                },
            },
            thread_id="thread-1",
        )

        summary = event.payload["summary"]
        self.assertLessEqual(len(summary), 160)
        self.assertIn("12 files", summary)
        self.assertNotIn("...", summary)
        # Whatever paths made the cut must be whole.
        listed = summary.split(" (+")[0]
        for path in listed.split(", "):
            self.assertIn(path, long_paths)

    def test_file_change_card_prefers_paths_over_a_generic_summary_field(self):
        """A generic `summary` must not be able to push a patch onto the card."""
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        event = transport._convert_event(
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "fileChange",
                        "id": "patch-1",
                        "status": "completed",
                        "summary": "@@ -1 +1 @@\n-old secret\n+new secret",
                        "changes": [
                            {"path": "src/a.py", "kind": {"type": "update"}, "diff": "@@"}
                        ],
                    },
                },
            },
            thread_id="thread-1",
        )

        self.assertEqual(event.payload["summary"], "src/a.py")

    def test_mcp_tool_call_item_is_named_after_the_tool(self):
        """`mcpToolCall` spells its name `tool`, which the generic chain missed."""
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        event = transport._convert_event(
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread-1",
                    "item": {
                        "type": "mcpToolCall",
                        "id": "mcp-1",
                        "server": "exa",
                        "tool": "web_search_exa",
                        "status": "inProgress",
                        "arguments": {"query": "InfLoRA"},
                    },
                },
            },
            thread_id="thread-1",
        )

        self.assertEqual(event.type, AgentEventType.TOOL_STARTED)
        self.assertEqual(event.payload["tool_name"], "web_search_exa")

    # Generated from the installed codex, not hand-written:
    #   codex app-server generate-json-schema --out <dir>
    #   → codex_app_server_protocol.v2.schemas.json → ThreadItem variants
    # `test_codex_thread_item_snapshot_matches_installed_codex` re-derives it
    # from the binary and fails when the two drift apart. A hand-maintained
    # list here would be worthless: upgrading codex would not change it, so
    # the "new variant fails the build" guarantee would be a lie.
    _THREAD_ITEM_SNAPSHOT = Path(__file__).parent / "data" / "codex_thread_item_variants.json"

    @classmethod
    def _schema_thread_item_variants(cls) -> set[str]:
        payload = json.loads(cls._THREAD_ITEM_SNAPSHOT.read_text())
        return set(payload["thread_item_variants"])

    @staticmethod
    def _thread_item_variants_from_schema_dir(schema_dir) -> set[str]:
        schema = json.loads(
            (Path(schema_dir) / "codex_app_server_protocol.v2.schemas.json").read_text()
        )
        defs = schema.get("definitions") or schema["$defs"]
        thread_item = defs["ThreadItem"]
        found: set[str] = set()
        for variant in thread_item.get("oneOf") or thread_item.get("anyOf") or []:
            type_schema = variant.get("properties", {}).get("type", {})
            names = type_schema.get("enum") or (
                [type_schema["const"]] if "const" in type_schema else []
            )
            found.update(names)
        return found

    def test_codex_thread_item_snapshot_matches_installed_codex(self):
        """The snapshot must track the codex actually installed here.

        Skipped where codex is absent (CI), which is exactly why the snapshot
        is committed — but on a developer box, an upgrade that adds a
        ThreadItem variant fails here and forces the classification decision.
        """
        codex = shutil.which("codex")
        if codex is None:
            self.skipTest("codex CLI not installed")
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [codex, "app-server", "generate-json-schema", "--out", tmp],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                self.skipTest(f"codex could not emit its schema: {result.stderr.strip()[:200]}")
            live = self._thread_item_variants_from_schema_dir(tmp)

        self.assertEqual(
            live,
            self._schema_thread_item_variants(),
            "codex changed its ThreadItem variants — regenerate "
            "tests/data/codex_thread_item_variants.json and classify the new ones "
            "in _CODEX_TOOL_ITEM_SPECS or in this test's not_tool_activity set",
        )

    def test_codex_tool_item_specs_cover_every_schema_variant(self):
        """Every `ThreadItem` variant is classified on purpose, not by spelling.

        The 2026-08-07 outage was a tool item nobody had classified; this is
        the guard that turns the next one into a red test.
        """
        schema_variants = self._schema_thread_item_variants()
        # Variants that are deliberately not tool cards: they are either
        # rendered by another path (messages, reasoning, plan) or have no
        # user-facing form yet.
        not_tool_activity = {
            "userMessage",
            "hookPrompt",
            "agentMessage",
            "plan",
            "reasoning",
            "subAgentActivity",
            "imageView",
            "sleep",
            "imageGeneration",
            "enteredReviewMode",
            "exitedReviewMode",
            "contextCompaction",
        }
        compact = {
            variant: re.sub(r"[^a-z0-9]+", "", variant.lower()) for variant in schema_variants
        }
        mapped = {
            variant
            for variant, key in compact.items()
            if key in walkcode_channel_native._CODEX_TOOL_ITEM_SPECS
        }

        self.assertEqual(mapped, schema_variants - not_tool_activity)
        self.assertEqual(
            set(walkcode_channel_native._CODEX_TOOL_ITEM_SPECS),
            {compact[variant] for variant in mapped},
            "the spec table must not carry keys that no schema variant produces",
        )

    def test_fuzzy_file_search_notification_is_not_a_tool_card(self):
        """The file picker's own notification is not agent tool activity.

        `fuzzyFileSearch/sessionCompleted` is codex asking the *client* to
        render an autocomplete list. Matching item types on a bare "search" or
        "file" substring would turn every keystroke into a TOOL_COMPLETED card,
        so the extra types are matched exactly instead.
        """
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)

        for method in ("fuzzyFileSearch/sessionCompleted", "fuzzyFileSearch/sessionUpdated"):
            with self.subTest(method=method):
                event = transport._convert_event(
                    {"method": method, "params": {"threadId": "thread-1", "query": "chan"}},
                    thread_id="thread-1",
                )
                self.assertIsNone(event)

    def test_events_convert_command_approval_request_and_answer_original_request_id(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "startedAtMs": 1,
                    "environmentId": None,
                    "reason": "network access",
                    "command": "curl https://example.com",
                    "cwd": "/tmp/project",
                    "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.PERMISSION_REQUESTED)
        self.assertEqual(events[0].payload["rid"], "approval-1")
        self.assertEqual(events[0].payload["tool_name"], "Command")
        self.assertEqual(events[0].payload["tool_input"]["command"], "curl https://example.com")
        self.assertEqual(events[0].payload["actions"], ["accept", "acceptForSession", "decline", "cancel"])

        asyncio.run(transport.approve_permission(handle, "approval-1", {"action": "acceptForSession"}))

        self.assertEqual(
            client.responses,
            [("approval-1", {"decision": "acceptForSession"})],
        )

    def test_events_convert_file_change_approval_and_answer_original_request_id(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "file-approval-1",
                "method": "item/fileChange/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "file-1",
                    "startedAtMs": 1,
                    "grantRoot": "/tmp/project",
                    "reason": "write generated file",
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.PERMISSION_REQUESTED)
        self.assertEqual(events[0].payload["rid"], "file-approval-1")
        self.assertEqual(events[0].payload["tool_name"], "File change")
        self.assertEqual(events[0].payload["tool_input"]["grant_root"], "/tmp/project")
        self.assertEqual(events[0].payload["actions"], ["accept", "acceptForSession", "decline", "cancel"])

        asyncio.run(transport.approve_permission(handle, "file-approval-1", {"action": "decline"}))

        self.assertEqual(client.responses, [("file-approval-1", {"decision": "decline"})])

    def test_events_convert_permission_profile_approval_and_answer_native_shape(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "permissions-1",
                "method": "item/permissions/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "perm-1",
                    "startedAtMs": 1,
                    "cwd": "/tmp/project",
                    "reason": "needs network and write",
                    "permissions": {
                        "network": {"enabled": True},
                        "fileSystem": {"write": ["/tmp/project"]},
                    },
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.PERMISSION_REQUESTED)
        self.assertEqual(events[0].payload["rid"], "permissions-1")
        self.assertEqual(events[0].payload["tool_name"], "Permission profile")
        self.assertEqual(events[0].payload["tool_input"]["permissions"]["network"], {"enabled": True})

        asyncio.run(transport.approve_permission(handle, "permissions-1", {"action": "acceptForSession"}))

        self.assertEqual(
            client.responses,
            [
                (
                    "permissions-1",
                    {
                        "permissions": {
                            "network": {"enabled": True},
                            "fileSystem": {"write": ["/tmp/project"]},
                        },
                        "scope": "session",
                    },
                )
            ],
        )

    def test_orchestrator_roundtrips_codex_command_approval_through_callback(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "approval-1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "cmd-1",
                    "startedAtMs": 1,
                    "environmentId": None,
                    "command": "ls -la",
                    "cwd": "/tmp/project",
                    "availableDecisions": ["accept", "decline"],
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        channel = FakeChannelAdapter("telegram", _channel_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(),
            interactions=InteractionStore(),
            outbox=DurableOutbox(),
            channels={"telegram": channel},
            transports={"codex_app_server": transport},
            authz=AuthorizationStore(),
        )
        session = asyncio.run(
            orchestrator.start_session(_binding(), "codex_app_server", "/tmp/project", _actor())
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="run command"),
                actor=_actor(),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        # Find the card, don't assume it is last: with the scripted batches
        # exhausted the listener also emits its give-up notice behind it.
        prompt = next(
            sent["view"]
            for sent in channel.sent_views
            if sent["view"].get("type") == "permission_prompt"
        )
        pending = orchestrator.hitls.pending_for_session(session.session_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].transport_request_id, "approval-1")
        self.assertEqual(pending[0].native_method, "item/commandExecution/requestApproval")
        callback = asyncio.run(
            orchestrator.handle_inbound_event(
                _callback(_token_for(prompt, "accept")),
                agent_transport_kind="codex_app_server",
                cwd="/tmp/project",
            )
        )

        self.assertTrue(callback.accepted)
        self.assertEqual(client.responses, [("approval-1", {"decision": "accept"})])
        decided = orchestrator.hitls.get(pending[0].hitl_request_id)
        self.assertEqual(decided.status, "decided")
        self.assertEqual(
            orchestrator.hitls.decision_for(pending[0].hitl_request_id).native_response["action"],
            "accept",
        )

    def test_events_convert_tool_request_user_input_and_answer_by_question_id(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "ask-1",
                "method": "item/tool/requestUserInput",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "itemId": "tool-1",
                    "questions": [
                        {
                            "id": "choice",
                            "header": "Mode",
                            "question": "Pick a mode",
                            "isOther": True,
                            "isSecret": False,
                            "options": [
                                {"label": "Fast", "description": ""},
                                {"label": "Careful", "description": ""},
                            ],
                        }
                    ],
                    "autoResolutionMs": None,
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.ASK_USER_REQUESTED)
        self.assertEqual(events[0].payload["rid"], "ask-1")
        self.assertEqual(events[0].payload["questions"][0]["prompt"], "Pick a mode")
        self.assertEqual(events[0].payload["questions"][0]["options"], ["Fast", "Careful"])
        self.assertTrue(events[0].payload["questions"][0]["allow_other"])

        asyncio.run(transport.answer_user_question(handle, "ask-1", {0: "Careful"}))

        self.assertEqual(
            client.responses,
            [("ask-1", {"answers": {"choice": {"answers": ["Careful"]}}})],
        )

    def test_events_convert_mcp_elicitation_form_and_answer_content(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "id": "mcp-1",
                "method": "mcpServer/elicitation/request",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "serverName": "demo",
                    "mode": "form",
                    "message": "Need deployment options",
                    "requestedSchema": {
                        "required": ["environment", "dry_run"],
                        "properties": {
                            "environment": {
                                "type": "string",
                                "title": "Environment",
                                "enum": ["staging", "prod"],
                            },
                            "dry_run": {
                                "type": "boolean",
                                "title": "Dry run",
                            },
                        },
                    },
                },
            }
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].type, AgentEventType.ASK_USER_REQUESTED)
        self.assertEqual(events[0].payload["rid"], "mcp-1")
        self.assertEqual([q["id"] for q in events[0].payload["questions"]], ["environment", "dry_run"])
        self.assertEqual(events[0].payload["questions"][0]["options"], ["staging", "prod"])
        self.assertEqual(events[0].payload["questions"][1]["options"], ["true", "false"])

        asyncio.run(transport.answer_user_question(handle, "mcp-1", {0: "prod", 1: "false"}))

        self.assertEqual(
            client.responses,
            [
                (
                    "mcp-1",
                    {
                        "action": "accept",
                        "content": {"environment": "prod", "dry_run": False},
                        "_meta": None,
                    },
                )
            ],
        )

    def test_codex_question_answer_can_rebuild_shape_from_persisted_question_metadata(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        asyncio.run(
            transport.answer_user_question(
                None,
                "ask-after-restart",
                {
                    0: "Careful",
                    "_questions": [{"id": "choice", "prompt": "Pick", "options": ["Fast", "Careful"]}],
                },
            )
        )

        self.assertEqual(
            client.responses,
            [("ask-after-restart", {"answers": {"choice": {"answers": ["Careful"]}}})],
        )

    def test_codex_permission_answer_can_rebuild_permissions_response_from_persisted_metadata(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        asyncio.run(
            transport.approve_permission(
                None,
                "perm-after-restart",
                {
                    "action": "acceptForSession",
                    "_tool_input": {
                        "native_method": "item/permissions/requestApproval",
                        "permissions": {
                            "network": {"hosts": ["example.com"]},
                            "fileSystem": {"writableRoots": ["/tmp/project"]},
                        },
                    },
                },
            )
        )

        self.assertEqual(
            client.responses,
            [
                (
                    "perm-after-restart",
                    {
                        "permissions": {
                            "network": {"hosts": ["example.com"]},
                            "fileSystem": {"writableRoots": ["/tmp/project"]},
                        },
                        "scope": "session",
                    },
                )
            ],
        )

    def test_resume_requires_thread_id(self):
        client = _FakeCodexClient()
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)

        with self.assertRaises(ValueError):
            asyncio.run(transport.resume_thread("", cwd="/tmp/project"))

        handle = asyncio.run(transport.resume_thread("thread-2", cwd="/tmp/project"))
        self.assertEqual(handle.ref["thread_id"], "thread-2")
        self.assertEqual(client.requests[-1][0], "thread/resume")

    def test_unverified_capabilities_are_disabled(self):
        transport = CodexAppServerTransport(client=_FakeCodexClient(), event_silence_ceiling=0)
        caps = transport.capabilities()

        self.assertTrue(caps.permission_callback)
        self.assertTrue(caps.ask_user_question)
        self.assertFalse(caps.multi_client_observe)
        self.assertFalse(caps.resume_active_turn)


class _BatchScriptCodexClient(_FakeCodexClient):
    """Fake whose ``events()`` hands out one scripted batch per call.

    The real collector is bounded: it returns after its own timeout whether or
    not the turn finished, so a single turn spans several batches. This fake
    reproduces that shape; ``[]`` stands for "the collector waited and nothing
    arrived".
    """

    def __init__(self, batches):
        super().__init__()
        self.batches = list(batches)
        self.event_calls = 0

    async def events(self, thread_id):
        self.event_calls += 1
        return self.batches.pop(0) if self.batches else []


class CodexPersistentListenTests(unittest.TestCase):
    def _handle(self, transport):
        return asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

    def test_listen_spans_collector_batches_until_turn_completed(self):
        # The 2026-07-30 loss: a 68-minute turn outlived the collector's
        # window, the drain read the batch end as a broken stream, and the
        # final answer (produced 39s later) never reached the channel.
        client = _BatchScriptCodexClient(
            [
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "t1", "delta": "wor"},
                    }
                ],
                [],
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "t1", "delta": "king"},
                    }
                ],
                [
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-1", "turn": {"id": "t1"}},
                    }
                ],
            ]
        )
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=3600)
        transport._EMPTY_BATCH_MIN_INTERVAL = 0
        handle = self._handle(transport)

        events = _drain_events(transport, handle)

        self.assertEqual(client.event_calls, 4)
        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.TURN_DELTA,
                AgentEventType.TURN_DELTA,
                AgentEventType.TURN_COMPLETED,
            ],
        )
        self.assertEqual(events[0].payload["text"], "wor")
        self.assertEqual(events[1].payload["text"], "king")

    def test_silence_ceiling_warns_and_closes_the_turn(self):
        # Giving up must be loud AND must close the turn: a stream that ends
        # mid-turn is what flips the session to ERROR_RECOVERABLE, and that
        # state has no self-healing path.
        client = _BatchScriptCodexClient(
            [
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "t1", "delta": "hi"},
                    }
                ]
            ]
        )
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = self._handle(transport)

        events = _drain_events(transport, handle)

        self.assertEqual(events[0].type, AgentEventType.TURN_DELTA)
        self.assertEqual(events[0].payload["text"], "hi")
        self.assertIn("已静默", events[-2].payload["text"])
        self.assertIn("直接回复可重新拉起会话", events[-2].payload["text"])
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)

    def test_hitl_request_parks_the_turn_without_synthetic_completion(self):
        # A permission card hands the turn to a human. Giving up on the listen
        # here would strand everything the agent produces after the answer.
        client = _BatchScriptCodexClient(
            [
                [
                    {
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "rm -rf /tmp/x"},
                    }
                ]
            ]
        )
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = self._handle(transport)

        events = _drain_events(transport, handle)

        types = [event.type for event in events]
        self.assertIn(AgentEventType.PERMISSION_REQUESTED, types)
        # No fake completion: the turn really is unfinished.
        self.assertNotIn(AgentEventType.TURN_COMPLETED, types)
        # And the give-up notice says it is waiting on a person, not stalled.
        self.assertIn("等你回应", events[-1].payload["text"])

    def test_listen_continues_past_an_answered_hitl_card(self):
        # Regression: returning at the HITL event ended the only consumer, so
        # the agent's continuation after the human answered was never
        # delivered — the same silent loss the whole change removes.
        client = _BatchScriptCodexClient(
            [
                [
                    {
                        "id": "req-1",
                        "method": "item/commandExecution/requestApproval",
                        "params": {"threadId": "thread-1", "command": "ls"},
                    }
                ],
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {"threadId": "thread-1", "turnId": "t1", "delta": "after approval"},
                    }
                ],
                [
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "thread-1", "turn": {"id": "t1"}},
                    }
                ],
            ]
        )
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=3600)
        transport._EMPTY_BATCH_MIN_INTERVAL = 0
        handle = self._handle(transport)

        events = _drain_events(transport, handle)

        self.assertEqual(client.event_calls, 3)
        self.assertEqual(
            [event.type for event in events],
            [
                AgentEventType.PERMISSION_REQUESTED,
                AgentEventType.TURN_DELTA,
                AgentEventType.TURN_COMPLETED,
            ],
        )
        self.assertEqual(events[1].payload["text"], "after approval")

    def test_empty_batches_are_throttled_against_a_hot_loop(self):
        # A client that returns empty instantly (closed transport, stub) must
        # not spin the ceiling window at full CPU.
        client = _BatchScriptCodexClient([])
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0.05)
        transport._EMPTY_BATCH_MIN_INTERVAL = 0.01
        handle = self._handle(transport)
        slept: list[float] = []
        real_sleep = asyncio.sleep

        async def recording_sleep(delay, *args, **kwargs):
            slept.append(delay)
            return await real_sleep(0, *args, **kwargs)

        asyncio.sleep = recording_sleep
        try:
            events = _drain_events(transport, handle)
        finally:
            asyncio.sleep = real_sleep

        self.assertTrue(slept, "empty batches must be throttled")
        self.assertTrue(all(delay <= 0.01 for delay in slept), slept)
        self.assertEqual(events[-1].type, AgentEventType.TURN_COMPLETED)


class _ScriptedWireCodexStdioClient(CodexStdioAppServerClient):
    """Stdio client whose wire is a scripted list instead of a subprocess."""

    def __init__(self, wire):
        super().__init__(request_timeout=1, event_timeout=0.2, event_idle_timeout=0.01)
        self.wire = list(wire)
        self.sent: list[dict] = []

    async def _ensure_started(self):
        if not self._reader_alive():
            self._start_reader()

    async def _send(self, message):
        self.sent.append(message)

    async def _read_message(self, *, timeout):
        if not self.wire:
            await asyncio.sleep(0.01)
            raise TimeoutError()
        return self.wire.pop(0)


class CodexEventRoutingTests(unittest.TestCase):
    def test_thread_less_events_are_not_handed_to_a_foreign_thread(self):
        # Several TUI sessions share one app-server process. An event with no
        # threadId used to go to whichever drain asked first, surfacing one
        # session's output in another session's channel.
        client = _ScriptedWireCodexStdioClient([])
        unaddressed = {"type": "event_msg", "payload": {"type": "agent_message", "message": "whose?"}}

        async def scenario():
            # Both threads are genuinely listening when the message lands.
            a = asyncio.ensure_future(client.events("thread-a"))
            b = asyncio.ensure_future(client.events("thread-b"))
            await asyncio.sleep(0.02)
            self.assertEqual(sorted(client._live_listener_threads()), ["thread-a", "thread-b"])
            client._dispatch(unaddressed)
            return await a, await b

        events_a, events_b = asyncio.run(scenario())

        self.assertEqual(events_a, [])
        self.assertEqual(events_b, [])
        self.assertEqual(client._buffered_notifications, [unaddressed])

    def test_buffered_thread_less_event_is_claimed_once_the_others_go_quiet(self):
        # Regression: liveness was keyed on _thread_queues, which is never
        # torn down. After two threads had ever run, every unaddressed message
        # stayed in the buffer with no claimant — a silent loss of the exact
        # final replies this design protects.
        client = _ScriptedWireCodexStdioClient([])
        final = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "done"}}

        async def scenario():
            a = asyncio.ensure_future(client.events("thread-a"))
            b = asyncio.ensure_future(client.events("thread-b"))
            await asyncio.sleep(0.02)
            client._dispatch(final)          # ambiguous: buffered
            await a
            await b                          # both listens end
            self.assertEqual(client._live_listener_threads(), [])
            return await client.events("thread-b")   # sole listener now

        events = asyncio.run(scenario())

        self.assertEqual(events, [final])
        self.assertEqual(client._buffered_notifications, [])

    def test_thread_less_events_still_reach_a_solitary_thread(self):
        client = _ScriptedWireCodexStdioClient(
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "done"},
                },
            ]
        )

        events = asyncio.run(client.events("thread-only"))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["type"], "task_complete")

    def test_event_msg_thread_id_is_read_from_the_payload(self):
        # `params` is absent on the event_msg shape, and the old extractor
        # returned "" from the params branch before ever reaching payload.
        message = {
            "type": "event_msg",
            "payload": {"type": "agent_message", "threadId": "thread-x", "message": "hi"},
        }

        self.assertEqual(_notification_thread_id(message), "thread-x")
        self.assertFalse(_notification_matches_thread(message, "thread-y"))
        self.assertTrue(_notification_matches_thread(message, "thread-x"))

    def test_events_do_not_block_a_concurrent_request(self):
        # The old events() held the client lock for its whole window, so a
        # message sent from the channel could wait minutes to be submitted.
        client = _ScriptedWireCodexStdioClient([])

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.01)

            async def answer_later():
                await asyncio.sleep(0.01)
                client._dispatch({"id": 1, "result": {"turn": {"id": "turn-1"}}})

            asyncio.ensure_future(answer_later())
            result = await asyncio.wait_for(
                client.request("turn/start", {"threadId": "thread-1"}), timeout=1
            )
            listen.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen
            return result

        result = asyncio.run(scenario())

        self.assertEqual(result["turn"]["id"], "turn-1")


class CodexStreamFailureTests(unittest.TestCase):
    """The reader dying must never silently swallow already-received output."""

    def test_events_received_before_the_failure_are_delivered_first(self):
        # Regression: the error was raised out of band and jumped ahead of
        # events already sitting in the queue, so a turn's final text was
        # dropped in favour of the exception.
        client = _ScriptedWireCodexStdioClient([])
        delta = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "t1", "delta": "half a sentence"},
        }

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._dispatch(delta)
            client._fail_stream(TransportUnavailable("wire died"))
            return await listen

        events = asyncio.run(scenario())

        self.assertEqual(events, [delta])

    def test_the_failure_is_raised_on_the_next_call_not_swallowed(self):
        # Regression: a reconnect ran _start_reader, which cleared
        # _stream_error, so a mid-batch death never reached the drain and the
        # broken turn looked merely quiet. The carried failure must survive to
        # the next call on the SAME connection.
        client = _ScriptedWireCodexStdioClient([])
        delta = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "t1", "delta": "half"},
        }

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            generation = client._connection_generation
            client._dispatch(delta)
            client._fail_stream(TransportUnavailable("wire died"))
            first = await listen
            self.assertEqual(
                client._connection_generation, generation, "no reconnect expected yet"
            )
            with self.assertRaises(TransportUnavailable) as caught:
                await client.events("thread-1")
            return first, str(caught.exception)

        first, message = asyncio.run(scenario())

        self.assertEqual(first, [delta])
        self.assertIn("wire died", message)

    def test_a_stale_failure_marker_cannot_fail_a_turn_on_a_new_wire(self):
        # Regression: the terminal event of a turn returns ahead of the
        # failure marker queued behind it, leaving the marker in the queue.
        # After a reconnect it would surface on the NEXT turn — failing a turn
        # running over a perfectly healthy wire.
        client = _ScriptedWireCodexStdioClient([])
        completed = {
            "method": "turn/completed",
            "params": {"threadId": "thread-1", "turn": {"id": "t1"}},
        }
        next_turn_delta = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "t2", "delta": "new turn"},
        }

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._dispatch(completed)
            client._fail_stream(TransportUnavailable("wire died"))
            first = await listen                      # returns at turn/completed
            self.assertEqual(first, [completed])
            # Reconnect: a fresh reader owns the wire from here.
            client._start_reader()
            await asyncio.sleep(0)
            second = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._dispatch(next_turn_delta)
            return await second

        events = asyncio.run(scenario())

        self.assertEqual(events, [next_turn_delta])

    def test_failure_with_nothing_in_hand_raises_immediately(self):
        client = _ScriptedWireCodexStdioClient([])

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._fail_stream(TransportUnavailable("wire died"))
            await listen

        with self.assertRaises(TransportUnavailable):
            asyncio.run(scenario())

    def test_pending_requests_are_failed_when_the_reader_dies(self):
        client = _ScriptedWireCodexStdioClient([])

        async def scenario():
            pending = asyncio.ensure_future(client.request("turn/start", {"threadId": "thread-1"}))
            await asyncio.sleep(0.02)
            client._fail_stream(TransportUnavailable("wire died"))
            with self.assertRaises(TransportUnavailable):
                await pending
            return client._pending_responses

        leftover = asyncio.run(scenario())

        self.assertEqual(leftover, {}, "a dead reader must not leak pending futures")


class CodexAbortedListenTests(unittest.TestCase):
    def test_carried_failure_survives_the_reconnect_it_triggers(self):
        # Regression: the carried error was checked AFTER _ensure_started(),
        # which reconnects a dead reader and bumps the generation — so the
        # error looked stale, was dropped, and the caller got an empty batch.
        # The open turn then looked merely quiet.
        client = _ScriptedWireCodexStdioClient([])
        delta = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "t1", "delta": "half"},
        }

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._dispatch(delta)
            client._fail_stream(TransportUnavailable("wire died"))
            first = await listen
            self.assertEqual(first, [delta])
            # Kill the reader for real, so the next call must reconnect.
            await client._stop_reader()
            with self.assertRaises(TransportUnavailable) as caught:
                await client.events("thread-1")
            return str(caught.exception)

        message = asyncio.run(scenario())

        self.assertIn("wire died", message)

    def test_cancelling_a_listen_does_not_eat_events_it_already_took(self):
        # queue.get() is destructive: a drain cancelled at a handoff would
        # otherwise take its collected events to the grave.
        client = _ScriptedWireCodexStdioClient([])
        delta = {
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thread-1", "turnId": "t1", "delta": "keep me"},
        }

        async def scenario():
            listen = asyncio.ensure_future(client.events("thread-1"))
            await asyncio.sleep(0.02)
            client._dispatch(delta)
            await asyncio.sleep(0.02)          # let the listen pull it off
            listen.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen
            # A fresh listen must still see it.
            return await client.events("thread-1")

        events = asyncio.run(scenario())

        self.assertEqual(events, [delta])


class CodexAppServerModelBackfillTests(unittest.TestCase):
    def test_event_msg_stream_backfills_model_from_thread_settings_applied(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "thread_settings_applied",
                    "thread_settings": {"model": "gpt-5.6-sol", "model_provider_id": "azure"},
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "hello"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
            },
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].payload["model"], "gpt-5.6-sol")
        self.assertEqual(events[1].payload["model"], "gpt-5.6-sol")

    def test_event_msg_stream_backfills_model_from_turn_context(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {"type": "turn_context", "payload": {"turn_id": "turn-1", "model": "gpt-5.6-sol"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"},
            },
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["model"], "gpt-5.6-sol")

    def test_event_msg_without_model_source_leaves_model_absent(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "no model yet"}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        # events() is a persistent listener: without a task_complete the drain
        # only ends on the silence ceiling (synthetic TURN_COMPLETED). We only
        # care about the first converted agent_message here.
        events = _drain_events(transport, handle, stop_after=1)

        self.assertEqual(len(events), 1)
        self.assertNotIn("model", events[0].payload)

    def test_jsonrpc_stream_backfills_model_from_thread_settings_updated(self):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = [
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "thread-1",
                    "threadSettings": {"model": "gpt-5.6-sol"},
                },
            },
            {"method": "item/agentMessage/delta", "params": {"threadId": "thread-1", "delta": "hi"}},
            {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}}},
        ]
        transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
        handle = asyncio.run(transport.launch(LaunchSpec(cwd="/tmp/project", session_id="s1")))

        events = _drain_events(transport, handle)

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].payload["model"], "gpt-5.6-sol")
        self.assertEqual(events[1].payload["model"], "gpt-5.6-sol")


_CODEX_LIVE_FLAG = "WALKCODE_E2E_CODEX_APP_SERVER"


def _codex_live_skip_reason() -> str:
    if os.environ.get(_CODEX_LIVE_FLAG, "").strip() not in {"1", "true", "yes", "on"}:
        return f"set {_CODEX_LIVE_FLAG}=1 to run the real codex app-server checks"
    if not shutil.which("codex"):
        return "codex CLI is not installed"
    return ""


@unittest.skipIf(_codex_live_skip_reason(), _codex_live_skip_reason() or "live gate")
class CodexAppServerSandboxLiveTests(unittest.TestCase):
    """Real `codex app-server` checks for the sandbox contract.

    Everything else in this file talks to a fake client, which can only prove
    what walkcode *sends*. The whole point of omitting the `sandbox` key is what
    the server then *does* — that is unfalsifiable without a real server, and
    AGENTS.md requires the real-environment check for behaviour changes.

    Gated because it spawns a process and takes a few seconds; the assertions
    are deterministic, not flaky, so enable it in any pre-release run.
    """

    # Not covered here: thread/resume against a real server. A thread that has
    # never run a turn has no rollout on disk, so the app-server answers
    # `no rollout found for thread id ...`; producing one would need a real model
    # call. The resume path's parameter construction is covered by
    # test_resume_carries_the_same_sandbox_override_as_launch, and both paths go
    # through the same _with_sandbox_override helper exercised below.

    def _effective_sandbox(self, config_lines, *, override=None, resume=False):
        codex_home = tempfile.mkdtemp(prefix="walkcode-codex-live-")
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        Path(codex_home, "config.toml").write_text("\n".join(config_lines) + "\n")
        cwd = tempfile.mkdtemp(prefix="walkcode-codex-live-cwd-")
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)

        proc = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": codex_home},
            text=True,
            bufsize=1,
        )
        self.addCleanup(proc.kill)

        def call(request_id, method, params):
            proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                + "\n"
            )
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                self.assertTrue(line, f"app-server closed stdout before answering {method}")
                message = json.loads(line)
                if message.get("id") == request_id:
                    self.assertNotIn("error", message, f"{method} failed: {message.get('error')}")
                    return message["result"]

        call(1, "initialize", {"clientInfo": {"name": "walkcode-test", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()

        # Build params exactly the way the transport does, so this test breaks if
        # the omission logic regresses.
        transport = CodexAppServerTransport(client=None, sandbox_override=override)
        start = call(2, "thread/start", transport._with_sandbox_override({"cwd": cwd}))
        if not resume:
            return start["sandbox"]["type"]
        thread_id = start["thread"]["id"]
        resumed = call(
            3, "thread/resume", transport._with_sandbox_override({"threadId": thread_id, "cwd": cwd})
        )
        return resumed["sandbox"]["type"]

    def test_omitting_sandbox_uses_the_profile_setting(self):
        self.assertEqual(
            self._effective_sandbox(['sandbox_mode = "danger-full-access"']), "dangerFullAccess"
        )
        self.assertEqual(self._effective_sandbox(['sandbox_mode = "read-only"']), "readOnly")

    def test_profile_without_sandbox_mode_fails_closed(self):
        # If this ever starts returning workspaceWrite, omitting the key stops
        # being safe for profiles that never declared a sandbox.
        self.assertEqual(self._effective_sandbox(['approval_policy = "never"']), "readOnly")

    def test_explicit_override_still_beats_the_profile(self):
        self.assertEqual(
            self._effective_sandbox(['sandbox_mode = "danger-full-access"'], override="read-only"),
            "readOnly",
        )

    def test_resume_params_carry_the_override(self):
        # thread/resume used to drop the override, so a cold resume silently
        # inherited the profile's full access. Assert on the params the transport
        # builds for resume — see the class note on why the live server cannot
        # resume a turn-less thread.
        transport = CodexAppServerTransport(client=None, sandbox_override="read-only")
        params = transport._with_sandbox_override({"threadId": "t1", "cwd": "/tmp"})
        self.assertEqual(params["sandbox"], "read-only")


@unittest.skipIf(_codex_live_skip_reason(), _codex_live_skip_reason() or "live gate")
class CodexAppServerShutdownLiveTests(unittest.TestCase):
    """Real `codex app-server` checks for the shutdown protocol calls.

    ``shutdown()`` sends two methods no other code path uses. A fake client
    can only prove we *send* them; whether the server knows them at all is
    exactly the thing that would silently turn the close into a no-op, so it gets
    the real-server check AGENTS.md requires.
    """

    def _server(self):
        codex_home = tempfile.mkdtemp(prefix="walkcode-codex-exit-")
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        Path(codex_home, "config.toml").write_text('sandbox_mode = "read-only"\n')
        cwd = tempfile.mkdtemp(prefix="walkcode-codex-exit-cwd-")
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)

        proc = subprocess.Popen(
            ["codex", "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "CODEX_HOME": codex_home},
            text=True,
            bufsize=1,
        )
        self.addCleanup(proc.kill)

        state = {"id": 0}

        def call(method, params, *, expect_error=False):
            state["id"] += 1
            request_id = state["id"]
            proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                + "\n"
            )
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                self.assertTrue(line, f"app-server closed stdout before answering {method}")
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if expect_error:
                    return message
                self.assertNotIn("error", message, f"{method} failed: {message.get('error')}")
                return message["result"]

        call("initialize", {"clientInfo": {"name": "walkcode-test", "version": "1"}})
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}}) + "\n")
        proc.stdin.flush()
        return call, cwd

    def test_unsubscribe_drops_this_connections_subscription(self):
        call, cwd = self._server()
        thread_id = call("thread/start", {"cwd": cwd})["thread"]["id"]

        first = call("thread/unsubscribe", {"threadId": thread_id})
        second = call("thread/unsubscribe", {"threadId": thread_id})

        self.assertEqual(first["status"], "unsubscribed")
        # notSubscribed, not notLoaded: the call takes away the SUBSCRIPTION,
        # it does not unload the thread. Also proves the repeat is harmless,
        # which is what makes shutdown's best-effort retry safe.
        self.assertEqual(second["status"], "notSubscribed")

    def test_turn_interrupt_is_a_known_method(self):
        # No live turn to stop here (that needs a model call), so this only
        # rules out the failure that would matter: the server not knowing the
        # method at all, which shutdown() would swallow as a degrade log.
        call, cwd = self._server()
        thread_id = call("thread/start", {"cwd": cwd})["thread"]["id"]

        message = call(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": "no-such-turn"},
            expect_error=True,
        )

        code = (message.get("error") or {}).get("code")
        self.assertNotEqual(code, -32601, f"turn/interrupt unknown to the server: {message}")

    def test_unsubscribing_one_thread_leaves_the_others_alone(self):
        # The whole worry about closing a codex session: one app-server serves every
        # thread under a CODEX_HOME, so if the close reached the SERVER rather
        # than the THREAD, ending one Feishu session would silently kill every
        # other session on the same profile. Prove it is thread-scoped.
        call, cwd = self._server()
        keep = call("thread/start", {"cwd": cwd})["thread"]["id"]
        drop = call("thread/start", {"cwd": cwd})["thread"]["id"]

        self.assertEqual(call("thread/unsubscribe", {"threadId": drop})["status"], "unsubscribed")

        # Same connection, same server process, still answering — and the
        # sibling is untouched: still loaded, still readable, still holding its
        # own subscription (only a subscribed thread answers "unsubscribed").
        loaded = set(call("thread/loaded/list", {}).get("data") or [])
        self.assertIn(keep, loaded, loaded)
        call("thread/read", {"threadId": keep})  # `call` fails the test on error
        self.assertEqual(call("thread/unsubscribe", {"threadId": keep})["status"], "unsubscribed")

    def test_restart_backend_replaces_the_server_and_picks_up_a_new_mcp(self):
        """The end-to-end claim behind /reload, against a real app-server.

        A config edit is invisible to `thread/resume` because the app-server
        snapshots `mcp_servers` at process start. Nothing short of replacing
        the process fixes that, and a fake client can prove neither that the
        replacement happened nor that it read the edited file.
        """
        codex_home = tempfile.mkdtemp(prefix="walkcode-codex-reload-")
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        cwd = tempfile.mkdtemp(prefix="walkcode-codex-reload-cwd-")
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        probe = shutil.which("cat") or "/bin/cat"
        base = 'sandbox_mode = "read-only"\n'
        Path(codex_home, "config.toml").write_text(base)

        def mcp_children(pid):
            found = []
            for kid in subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, text=True
            ).stdout.split():
                cmd = subprocess.run(
                    ["ps", "-o", "command=", "-p", kid],
                    capture_output=True,
                    text=True,
                    env={**os.environ, "LC_ALL": "C"},
                ).stdout.strip()
                if cmd.startswith(probe):
                    found.append(kid)
                found.extend(mcp_children(kid))
            return found

        async def scenario():
            client = CodexStdioAppServerClient(request_timeout=30, codex_home=codex_home)
            transport = CodexAppServerTransport(client=client, event_silence_ceiling=0)
            try:
                await transport.launch(LaunchSpec(cwd=cwd, session_id="s1"))
                first_pid = client._process.pid
                await asyncio.sleep(1.5)
                before = len(mcp_children(first_pid))

                # Exactly what "I added an MCP to config.toml" looks like.
                Path(codex_home, "config.toml").write_text(
                    f'{base}\n[mcp_servers.probe]\ncommand = "{probe}"\n'
                )
                await transport.restart_backend()
                await transport.launch(LaunchSpec(cwd=cwd, session_id="s2"))
                second_pid = client._process.pid
                await asyncio.sleep(2.0)
                after = len(mcp_children(second_pid))
                return first_pid, second_pid, before, after
            finally:
                # Teardown inside the same loop: restart()/_stop_reader await
                # objects bound to it, and a fresh asyncio.run() would hang.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(client.restart(), timeout=15)

        first_pid, second_pid, before, after = asyncio.run(
            asyncio.wait_for(scenario(), timeout=90)
        )

        self.assertNotEqual(first_pid, second_pid, "restart_backend did not replace the process")
        self.assertEqual(before, 0)
        self.assertGreaterEqual(after, 1, "the replacement server did not read the edited config")

    def test_discarding_the_process_leaves_no_orphaned_app_server(self):
        """SIGKILL orphans the real server; the discard path must not use it.

        `codex app-server --stdio` is a node wrapper around the vendor binary.
        SIGKILL reaps only the wrapper, leaving the actual server alive holding
        our stdout pipe — `await process.wait()` then never returns, and for
        /reload the "restarted" server would still be the old one.
        """
        codex_home = tempfile.mkdtemp(prefix="walkcode-codex-discard-")
        self.addCleanup(shutil.rmtree, codex_home, ignore_errors=True)
        Path(codex_home, "config.toml").write_text('sandbox_mode = "read-only"\n')

        async def scenario():
            client = CodexStdioAppServerClient(request_timeout=30, codex_home=codex_home)
            await client._ensure_started()
            pid = client._process.pid
            descendants = subprocess.run(
                ["pgrep", "-P", str(pid)], capture_output=True, text=True
            ).stdout.split()
            # Bounded: an unbounded wait is the bug this guards against.
            await asyncio.wait_for(client.restart(), timeout=20)
            return pid, descendants

        pid, descendants = asyncio.run(asyncio.wait_for(scenario(), timeout=60))

        self.assertTrue(descendants, "expected the wrapper to have spawned the vendor binary")
        time.sleep(0.5)
        survivors = [
            kid
            for kid in descendants
            if subprocess.run(
                ["ps", "-o", "pid=", "-p", kid], capture_output=True, text=True
            ).stdout.strip()
        ]
        for kid in survivors:
            subprocess.run(["kill", "-9", kid], check=False)
        self.assertEqual(survivors, [], "app-server survived the discard")
