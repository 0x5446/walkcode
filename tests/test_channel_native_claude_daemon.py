"""Claude daemon multi-UI sync (ADR 0046) — client, transport, routing, watcher."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelCapabilities,
    ChannelConfigError,
    ChannelNativeConfig,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    LaunchSpec,
    Orchestrator,
    ResumeSpec,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
    TransportUnavailable,
    TurnInput,
    AttachmentRef,
)
from walkcode.channel_native.claude_daemon import (
    CLAUDE_DAEMON_PROTO,
    ClaudeDaemonClient,
    ClaudeDaemonError,
    ClaudeDaemonTransport,
    claude_daemon_short_from_resume_ref,
    claude_daemon_short_id,
    claude_daemon_socket_path,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime, _build_transports


AGENT_SESSION_ID = "5ca3e37c-1111-2222-3333-444455556666"
SHORT = "5ca3e37c"


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeDaemonServer:
    """ndjson protocol stub speaking the daemon control protocol over a real
    unix socket (protocol reference: daemon-appserver-protocol-reference.md)."""

    def __init__(self, socket_path: str, *, key: str = "a" * 32):
        self.socket_path = socket_path
        self.key = key
        self.proto = CLAUDE_DAEMON_PROTO
        self.version = "2.1.201"
        self.jobs: list[dict] = []
        self.job_status: dict[str, dict] = {}
        self.replies: list[tuple[str, str]] = []
        self.reply_error_code = ""
        self.subscribe_events: list[dict] = []
        self._server = None

    async def __aenter__(self):
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            request = json.loads(line)
            op = str(request.get("op", ""))
            if op == "ping":
                await self._send(writer, {"ok": True, "op": "ping", "version": self.version, "proto": self.proto})
            elif op == "list":
                await self._send(writer, {"ok": True, "op": "list", "jobs": self.jobs})
            elif op == "has":
                status = self.job_status.get(
                    str(request.get("short", "")),
                    {"alive": False, "present": False, "ready": False},
                )
                await self._send(writer, {"ok": True, "op": "has", **status})
            elif op == "reply":
                if request.get("auth") != self.key:
                    await self._send(writer, {"ok": False, "code": "EAUTH", "error": "control key mismatch"})
                elif self.reply_error_code:
                    await self._send(writer, {"ok": False, "code": self.reply_error_code, "error": "rejected"})
                else:
                    self.replies.append((str(request.get("short", "")), str(request.get("text", ""))))
                    await self._send(writer, {"ok": True, "op": "reply"})
            elif op == "subscribe":
                for event in self.subscribe_events:
                    await self._send(writer, event)
            else:
                await self._send(writer, {"ok": False, "code": "EUNKNOWN", "error": f"bad op {op}"})
        finally:
            writer.close()

    @staticmethod
    async def _send(writer, message: dict) -> None:
        writer.write(json.dumps(message).encode("utf-8") + b"\n")
        await writer.drain()


def _client_for(server: _FakeDaemonServer, tmp: str, *, key: str = "") -> ClaudeDaemonClient:
    key_path = Path(tmp) / "control.key"
    key_path.write_text(key or server.key, encoding="utf-8")
    return ClaudeDaemonClient(
        socket_path=server.socket_path,
        control_key_path=str(key_path),
        request_timeout=5.0,
    )


class SocketPathTests(unittest.TestCase):
    def test_socket_path_is_sha256_prefix_of_expanded_config_dir(self):
        config_dir = "/Users/alpha/.claude-profiles/work"
        digest = hashlib.sha256(config_dir.encode()).hexdigest()[:8]
        self.assertEqual(
            claude_daemon_socket_path(config_dir, uid=501),
            f"/tmp/cc-daemon-501/{digest}/control.sock",
        )

    def test_trailing_slash_does_not_change_the_hash(self):
        self.assertEqual(
            claude_daemon_socket_path("/x/y/", uid=1),
            claude_daemon_socket_path("/x/y", uid=1),
        )

    def test_short_id_normalization(self):
        self.assertEqual(claude_daemon_short_id(AGENT_SESSION_ID), SHORT)
        self.assertEqual(claude_daemon_short_id("5CA3E37C"), SHORT)
        self.assertEqual(claude_daemon_short_id("not-hex!"), "")
        self.assertEqual(claude_daemon_short_id(""), "")

    def test_short_from_resume_ref_handles_nesting(self):
        self.assertEqual(
            claude_daemon_short_from_resume_ref(
                {"resume_ref": {"transport_kind": "claude_headless", "agent_session_id": AGENT_SESSION_ID}}
            ),
            SHORT,
        )
        self.assertEqual(claude_daemon_short_from_resume_ref({"session_id": AGENT_SESSION_ID}), SHORT)
        self.assertEqual(claude_daemon_short_from_resume_ref({}), "")


class ClaudeDaemonClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_ping_probe_and_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.jobs = [{"short": SHORT, "sessionId": AGENT_SESSION_ID}]
                client = _client_for(server, tmp)
                pong = await client.probe()
                self.assertEqual(pong["version"], "2.1.201")
                jobs = await client.list_jobs()
                self.assertEqual(jobs[0]["short"], SHORT)

    async def test_probe_rejects_proto_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.proto = 2
                client = _client_for(server, tmp)
                with self.assertRaises(TransportUnavailable):
                    await client.probe()

    async def test_reply_sends_control_key_and_records_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                client = _client_for(server, tmp)
                await client.reply(SHORT, "hello from feishu")
                self.assertEqual(server.replies, [(SHORT, "hello from feishu")])

    async def test_reply_with_wrong_key_raises_eauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                client = _client_for(server, tmp, key="b" * 32)
                with self.assertRaises(ClaudeDaemonError) as ctx:
                    await client.reply(SHORT, "x")
                self.assertEqual(ctx.exception.code, "EAUTH")

    async def test_reply_enoreply_is_a_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.reply_error_code = "ENOREPLY"
                client = _client_for(server, tmp)
                with self.assertRaises(ClaudeDaemonError) as ctx:
                    await client.reply(SHORT, "x")
                self.assertEqual(ctx.exception.code, "ENOREPLY")

    async def test_missing_socket_degrades_to_transport_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ClaudeDaemonClient(
                socket_path=str(Path(tmp) / "missing.sock"),
                control_key_path=str(Path(tmp) / "missing.key"),
            )
            with self.assertRaises(TransportUnavailable):
                await client.ping()

    async def test_subscribe_yields_events_until_settled(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.subscribe_events = [
                    {"type": "snapshot", "record": {"short": SHORT}, "streamTail": []},
                    {"type": "state", "patch": {"tempo": "idle", "needs": ""}},
                    {"type": "settled", "outcome": "ok"},
                    {"type": "state", "patch": {"tempo": "never-delivered"}},
                ]
                client = _client_for(server, tmp)
                events = [event async for event in client.subscribe(SHORT)]
                self.assertEqual(
                    [event["type"] for event in events],
                    ["snapshot", "state", "settled"],
                )

    async def test_subscribe_error_response_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.subscribe_events = [{"ok": False, "code": "ENOJOB", "error": "gone"}]
                client = _client_for(server, tmp)
                with self.assertRaises(ClaudeDaemonError):
                    async for _event in client.subscribe(SHORT):
                        pass

    async def test_job_ready_requires_alive_present_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.job_status[SHORT] = {"alive": True, "present": True, "ready": True}
                client = _client_for(server, tmp)
                self.assertTrue(await client.job_ready(SHORT))
                self.assertFalse(await client.job_ready("deadbeef"))


class ClaudeDaemonTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_then_submit_turn_replies_with_attachment_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                server.job_status[SHORT] = {"alive": True, "present": True, "ready": True}
                transport = ClaudeDaemonTransport(client=_client_for(server, tmp))
                handle = await transport.resume(
                    ResumeSpec(
                        cwd="/tmp/project",
                        session_id="sess-1",
                        resume_ref={"agent_session_id": AGENT_SESSION_ID},
                    )
                )
                self.assertEqual(handle.ref["short"], SHORT)
                await transport.submit_turn(
                    handle,
                    TurnInput(
                        text="check this",
                        attachments=[AttachmentRef(source_id="a", local_path="/tmp/a.png")],
                    ),
                    idempotency_key="k1",
                )
                short, text = server.replies[0]
                self.assertEqual(short, SHORT)
                self.assertIn("check this", text)
                self.assertIn("/tmp/a.png", text)

    async def test_resume_requires_live_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            async with _FakeDaemonServer(str(Path(tmp) / "control.sock")) as server:
                transport = ClaudeDaemonTransport(client=_client_for(server, tmp))
                with self.assertRaises(TransportUnavailable):
                    await transport.resume(
                        ResumeSpec(
                            cwd="/tmp",
                            session_id="sess-1",
                            resume_ref={"agent_session_id": AGENT_SESSION_ID},
                        )
                    )

    async def test_launch_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = ClaudeDaemonTransport(
                client=ClaudeDaemonClient(
                    socket_path=str(Path(tmp) / "x.sock"),
                    control_key_path=str(Path(tmp) / "x.key"),
                )
            )
            with self.assertRaises(CapabilityUnsupported):
                await transport.launch(LaunchSpec(cwd="/tmp", session_id="s"))

    def test_capabilities_flags(self):
        caps = ClaudeDaemonTransport(config_dir="/tmp/profile").capabilities()
        self.assertTrue(caps.multi_client_observe)
        self.assertTrue(caps.multi_client_write)
        self.assertFalse(caps.external_tui_takeover)
        self.assertFalse(caps.permission_callback)
        self.assertFalse(caps.requires_single_writer)


def _channel_caps() -> ChannelCapabilities:
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


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


class _StubDaemonClient:
    """In-memory stand-in for ClaudeDaemonClient in orchestrator-level tests."""

    def __init__(self, *, ready: bool = True):
        self.ready = ready
        self.replies: list[tuple[str, str]] = []

    async def job_ready(self, short: str) -> bool:
        return self.ready

    async def reply(self, short: str, text: str) -> dict:
        self.replies.append((short, text))
        return {"ok": True, "op": "reply"}


def _orchestrator_with_observed_claude_session(*, daemon_client=None, resume_transport_kind="claude_headless"):
    clock = _Clock()
    sessions = SessionRegistry(now=clock)
    authz = AuthorizationStore(now=clock)
    channel = FakeChannelAdapter("telegram", _channel_caps())
    transports = {
        "fake-transport": FakeAgentTransport(
            "fake-transport",
            TransportCapabilities(
                structured_input=True,
                structured_output=True,
                permission_callback=True,
                ask_user_question=True,
                interrupt=True,
                set_model=True,
                set_permission_mode=True,
                checkpoint_rewind=True,
                resume_after_complete=True,
                resume_active_turn=False,
                multi_client_observe=False,
                multi_client_write=False,
                external_tui_takeover=True,
            ),
        ),
    }
    if daemon_client is not None:
        transports["claude_daemon"] = ClaudeDaemonTransport(client=daemon_client)
    orchestrator = Orchestrator(
        sessions=sessions,
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports=transports,
        authz=authz,
        now=clock,
    )
    session = sessions.create_observed_session(
        session_id="observed-1",
        binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
        cwd="/tmp/project",
        external_ref={
            "source": "native_tui_hook",
            "resume_ref": {
                "transport_kind": resume_transport_kind,
                "agent_session_id": AGENT_SESSION_ID,
            },
        },
        owner=_actor("owner"),
    )
    authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
    return orchestrator, channel, session


class DaemonReplyRoutingTests(unittest.TestCase):
    def test_observed_claude_session_input_replies_via_daemon_without_takeover(self):
        stub = _StubDaemonClient()
        orchestrator, channel, session = _orchestrator_with_observed_claude_session(daemon_client=stub)

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="continue with plan"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "daemon_reply")
        self.assertEqual(stub.replies, [(SHORT, "continue with plan")])
        # TUI keeps the session: no ownership churn, no takeover prompt.
        self.assertEqual(session.writer_owner.kind, "external_tui")
        self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
        view_types = [item["view"]["type"] for item in channel.sent_views]
        self.assertNotIn("takeover_prompt", view_types)

    def test_dead_daemon_job_falls_back_to_takeover_prompt(self):
        stub = _StubDaemonClient(ready=False)
        orchestrator, channel, session = _orchestrator_with_observed_claude_session(daemon_client=stub)

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="continue"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        self.assertTrue(result.blocked_input_id)
        self.assertEqual(stub.replies, [])
        view_types = [item["view"]["type"] for item in channel.sent_views]
        self.assertIn("takeover_prompt", view_types)

    def test_without_daemon_transport_falls_back_to_takeover_prompt(self):
        orchestrator, channel, session = _orchestrator_with_observed_claude_session(daemon_client=None)

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="continue"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, BlockedReason.EXTERNAL_TUI_READONLY)
        view_types = [item["view"]["type"] for item in channel.sent_views]
        self.assertIn("takeover_prompt", view_types)

    def test_daemon_reply_sends_ack_and_dedupes_prompt_echo(self):
        stub = _StubDaemonClient()
        orchestrator, channel, session = _orchestrator_with_observed_claude_session(daemon_client=stub)

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="改用方案B"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertTrue(result.accepted)
        texts = [
            str(item["view"].get("text", ""))
            for item in channel.sent_views
            if item["view"].get("type") == "text"
        ]
        self.assertTrue(any("已发送到终端" in text for text in texts))
        # The injected prompt echoes back once as user-prompt-submit; the echo
        # is consumed on first match and never suppresses a different prompt.
        self.assertFalse(orchestrator.consume_daemon_reply_echo(session.session_id, "别的话"))
        self.assertTrue(orchestrator.consume_daemon_reply_echo(session.session_id, "改用方案B"))
        self.assertFalse(orchestrator.consume_daemon_reply_echo(session.session_id, "改用方案B"))

    def test_non_claude_observed_session_still_uses_takeover(self):
        stub = _StubDaemonClient()
        orchestrator, channel, session = _orchestrator_with_observed_claude_session(
            daemon_client=stub, resume_transport_kind="codex_app_server"
        )

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="continue"),
                actor=_actor("owner"),
                generation=session.generation,
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(stub.replies, [])
        view_types = [item["view"]["type"] for item in channel.sent_views]
        self.assertIn("takeover_prompt", view_types)


class _FakeTelegramApi:
    def __init__(self):
        self.token = "fake"
        self.calls = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        return {"ok": True, "result": {}}


def _runtime_with_observed_session(tmp: str):
    cfg = ChannelNativeConfig.from_env(
        {
            "WALKCODE_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "fake",
            "WALKCODE_AGENT": "claude",
            "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
            "WALKCODE_CWD": tmp,
        }
    )
    api = _FakeTelegramApi()
    runtime = ChannelNativeRuntime.from_config(
        cfg,
        telegram_api=api,
        transports={
            "fake-transport": FakeAgentTransport(
                "fake-transport",
                ClaudeDaemonTransport(config_dir=tmp).capabilities(),
            )
        },
    )
    session = runtime.state.sessions.create_observed_session(
        session_id="observed-1",
        binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
        cwd=tmp,
        external_ref={
            "source": "native_tui_hook",
            "resume_ref": {
                "transport_kind": "claude_headless",
                "agent_session_id": AGENT_SESSION_ID,
            },
        },
        owner=_actor("owner"),
    )
    return runtime, session, api


def _notice_message_count(api: "_FakeTelegramApi", marker: str) -> int:
    return sum(
        1
        for method, payload in api.calls
        if method == "sendMessage" and marker in str(payload.get("text", ""))
    )


class DaemonWatcherStateSyncTests(unittest.TestCase):
    def test_needs_patch_flips_waiting_permission_and_sends_notice_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, api = _runtime_with_observed_session(tmp)

            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "blocked", "needs": "approve Bash - rm build/"},
                    "",
                )
            )

            self.assertEqual(last, "approve Bash - rm build/")
            self.assertEqual(session.lifecycle_state, "WAITING_PERMISSION")
            self.assertEqual(_notice_message_count(api, "approve Bash"), 1)

            # Same needs again: no duplicate notice.
            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"needs": "approve Bash - rm build/"},
                    last,
                )
            )
            self.assertEqual(_notice_message_count(api, "approve Bash"), 1)

    def test_cleared_needs_returns_to_observed_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)
            session.lifecycle_state = "WAITING_PERMISSION"

            last = asyncio.run(
                runtime._apply_claude_daemon_state_patch(
                    session.session_id,
                    {"tempo": "active", "needs": ""},
                    "approve Bash - rm build/",
                )
            )

            self.assertEqual(last, "")
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")

    def test_settled_marks_session_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, session, _api = _runtime_with_observed_session(tmp)

            asyncio.run(runtime._settle_claude_daemon_session(session.session_id, outcome="ok"))

            self.assertEqual(session.status, "stopped")
            self.assertEqual(session.stop_reason, "external_tui_daemon_settled_ok")


class DaemonConfigWiringTests(unittest.TestCase):
    def test_invalid_daemon_mode_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChannelConfigError):
                ChannelNativeConfig.from_env(
                    {
                        "WALKCODE_CHANNEL": "telegram",
                        "TELEGRAM_BOT_TOKEN": "fake",
                        "WALKCODE_AGENT": "claude",
                        "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                        "WALKCODE_CWD": tmp,
                        "WALKCODE_CLAUDE_DAEMON_MODE": "maybe",
                    }
                )

    def test_daemon_transport_registered_by_default_and_disabled_by_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
                "WALKCODE_CWD": tmp,
                "WALKCODE_CLAUDE_CONFIG_DIR": tmp,
            }
            transports = _build_transports(ChannelNativeConfig.from_env(base))
            self.assertIn("claude_daemon", transports)
            transports = _build_transports(
                ChannelNativeConfig.from_env({**base, "WALKCODE_CLAUDE_DAEMON_MODE": "off"})
            )
            self.assertNotIn("claude_daemon", transports)


if __name__ == "__main__":
    unittest.main()
