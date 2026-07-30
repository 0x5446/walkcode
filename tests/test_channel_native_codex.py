import asyncio
import base64
import contextlib
import hashlib
import json
import os
import unittest
import uuid

from walkcode.channel_native import (
    ActorRef,
    AgentEventType,
    AuthorizationStore,
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


def _drain_events(transport, handle):
    """Consume the transport's event generator to exhaustion.

    ``events()`` is a persistent listener: it re-enters the bounded collector
    while the turn stays open. Tests hand it a fake client whose batches run
    dry, so the silence ceiling is pinned to 0 at construction — the listen
    then ends on the first empty batch instead of waiting an hour.
    """

    async def run():
        return [event async for event in transport.events(handle)]

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

        events = _drain_events(transport, handle)

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

        events = _drain_events(transport, handle)

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

        events = _drain_events(transport, handle)

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
        prompt = channel.sent_views[-1]["view"]
        self.assertEqual(prompt["type"], "permission_prompt")
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

        events = _drain_events(transport, handle)

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

        events = _drain_events(transport, handle)

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
        # A permission card hands the turn to a human; the answer re-enters
        # through a fresh drain. Closing the turn here would tell the channel
        # the agent finished while it is actually blocked on the card.
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

        self.assertEqual(client.event_calls, 1)
        self.assertEqual(events[-1].type, AgentEventType.PERMISSION_REQUESTED)
        self.assertNotIn(AgentEventType.TURN_COMPLETED, [event.type for event in events])

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
        client = _ScriptedWireCodexStdioClient(
            [
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "whose?"}},
            ]
        )

        async def scenario():
            # Two live threads: thread-b asks, and must not be given the
            # unaddressed message.
            client._queue_for("thread-a")
            client._queue_for("thread-b")
            return await client.events("thread-b")

        events = asyncio.run(scenario())

        self.assertEqual(events, [])
        self.assertEqual(len(client._buffered_notifications), 1)

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
