"""Daemon-native session spawn + list-fallback adoption (ADR 0048).

Covers the three new pieces: `claude --bg` spawn via ClaudeDaemonTransport
(spawn_bg_job with an injected CLI runner), the orchestrator's daemon_spawner
hook (channel-born sessions become daemon bg workers, headless stays the
fallback), and the daemon watcher's list adoption of wild jobs.
"""

import asyncio
import time
import tempfile
import unittest
from pathlib import Path

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ChannelConfigError,
    ChannelNativeConfig,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    SessionRole,
    TransportCapabilities,
    TransportUnavailable,
)
from unittest.mock import patch

from walkcode.channel_native.claude_daemon import (
    OBSERVER_ATTACH_ID,
    ClaudeDaemonTransport,
    parse_backgrounded_short,
)
from walkcode.channel_native_runtime import ChannelNativeRuntime

AGENT_SESSION_ID = "b2eaf26a-c21d-499f-a1b5-6a1b7be9176f"
SHORT = "b2eaf26a"
BG_OUTPUT_PLAIN = f"backgrounded · {SHORT} (idle — send a prompt to start)\n"
# Live-captured shape: the CLI colors stdout even when piped (FORCE_COLOR).
BG_OUTPUT_ANSI = (
    f"backgrounded · \x1b[36m{SHORT}\x1b[39m\x1b[2m (idle — send a prompt to start)\x1b[22m\n"
)


class ParseBackgroundedShortTests(unittest.TestCase):
    def test_plain_output(self):
        self.assertEqual(parse_backgrounded_short(BG_OUTPUT_PLAIN), SHORT)

    def test_ansi_colored_output(self):
        self.assertEqual(parse_backgrounded_short(BG_OUTPUT_ANSI), SHORT)

    def test_no_short(self):
        self.assertEqual(parse_backgrounded_short("error: daemon refused"), "")
        self.assertEqual(parse_backgrounded_short(""), "")


class _SpawnStubClient:
    """ClaudeDaemonClient stand-in for spawn_bg_job: list/ready/kill/reply."""

    def __init__(self, *, listed: bool = True, ready: bool = True):
        self.listed = listed
        self.ready = ready
        self.kills: list[str] = []
        self.replies: list[tuple[str, str]] = []
        self.observes: list[str] = []

    async def attach_observe(self, short: str, *, on_ready=None, **kwargs):
        # Signal the readiness barrier (spawn awaits it), then "drop the
        # connection"; job_alive() below returns False so the observer loop
        # exits cleanly.
        self.observes.append(short)
        if on_ready is not None:
            on_ready.set()

    async def job_alive(self, short: str):
        return False

    async def subscribe(self, short: str, *, tail: int = 0):
        return
        yield  # pragma: no cover - makes this an (empty) async generator

    async def list_jobs(self):
        if not self.listed:
            return []
        return [
            {
                "short": SHORT,
                "sessionId": AGENT_SESSION_ID,
                "cwd": "/tmp/spawned",
                "source": "shell",
                "backend": "daemon",
                "tempo": "idle",
                "state": "ready",
            }
        ]

    async def job_ready(self, short: str) -> bool:
        return self.ready

    async def kill(self, short: str, *, signal: str = "SIGTERM"):
        self.kills.append(short)
        return {"ok": True, "op": "kill"}

    async def reply(self, short: str, text: str):
        self.replies.append((short, text))
        return {"ok": True, "op": "reply"}


def _canned_runner(output: str = BG_OUTPUT_ANSI):
    calls: list[dict] = []

    async def runner(argv, *, cwd, env):
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env)})
        return output

    return runner, calls


class SpawnBgJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_returns_short_session_and_cwd(self):
        client = _SpawnStubClient()
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=client)
        runner, calls = _canned_runner()

        job = await transport.spawn_bg_job(
            "/tmp/req-cwd", settings="/tmp/override.json", cli_path="/opt/claude", bg_runner=runner
        )

        self.assertEqual(
            job, {"short": SHORT, "session_id": AGENT_SESSION_ID, "cwd": "/tmp/spawned"}
        )
        self.assertEqual(
            calls[0]["argv"], ["/opt/claude", "--bg", "--settings", "/tmp/override.json"]
        )
        self.assertEqual(calls[0]["cwd"], "/tmp/req-cwd")
        self.assertEqual(calls[0]["env"].get("CLAUDE_CONFIG_DIR"), "/tmp/profile")
        self.assertNotIn("CLAUDECODE", calls[0]["env"])
        self.assertEqual(client.kills, [])

    async def test_unparseable_output_raises(self):
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=_SpawnStubClient())
        runner, _ = _canned_runner("some unexpected banner\n")
        with self.assertRaises(TransportUnavailable):
            await transport.spawn_bg_job("/tmp", bg_runner=runner)

    async def test_never_ready_reaps_job_and_raises(self):
        client = _SpawnStubClient(ready=False)
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=client)
        runner, _ = _canned_runner()
        with self.assertRaises(TransportUnavailable):
            await transport.spawn_bg_job("/tmp", bg_runner=runner, ready_timeout=0.1)
        self.assertEqual(client.kills, [SHORT])

    async def test_runner_failure_maps_to_transport_unavailable(self):
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=_SpawnStubClient())

        async def broken_runner(argv, *, cwd, env):
            raise RuntimeError("cli exploded")

        with self.assertRaises(TransportUnavailable):
            await transport.spawn_bg_job("/tmp", bg_runner=broken_runner)


class SpawnModeConfigTests(unittest.TestCase):
    def _env(self, tmp: str, **extra: str) -> dict[str, str]:
        return {
            "WALKCODE_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "fake",
            "WALKCODE_AGENT": "claude",
            "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
            "WALKCODE_CWD": tmp,
            **extra,
        }

    def test_invalid_spawn_mode_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChannelConfigError):
                ChannelNativeConfig.from_env(self._env(tmp, WALKCODE_CLAUDE_SPAWN_MODE="bg"))

    def test_spawn_daemon_conflicts_with_daemon_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChannelConfigError):
                ChannelNativeConfig.from_env(
                    self._env(
                        tmp,
                        WALKCODE_CLAUDE_SPAWN_MODE="daemon",
                        WALKCODE_CLAUDE_DAEMON_MODE="off",
                    )
                )

    def test_invalid_list_adopt_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ChannelConfigError):
                ChannelNativeConfig.from_env(self._env(tmp, WALKCODE_CLAUDE_LIST_ADOPT="maybe"))

    def test_valid_values_land_in_agent_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                self._env(
                    tmp,
                    WALKCODE_CLAUDE_SPAWN_MODE="daemon",
                    WALKCODE_CLAUDE_LIST_ADOPT="off",
                )
            )
            self.assertEqual(cfg.agent_options["claude"]["spawn_mode"], "daemon")
            self.assertEqual(cfg.agent_options["claude"]["list_adopt"], "off")

    def test_spawn_mode_defaults_to_headless(self):
        # ADR 0050: single-master UI is the default (reverting the ADR 0048
        # daemon default); the parser resolves the value so consumers see
        # one truth. Dual-UI daemon spawn is an explicit opt-in.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(self._env(tmp))
            self.assertEqual(cfg.agent_options["claude"]["spawn_mode"], "headless")
            self.assertNotIn("list_adopt", cfg.agent_options["claude"])

    def test_daemon_off_degrades_default_spawn_mode_to_headless(self):
        # Only the EXPLICIT daemon+off combination is a config error; the
        # default must keep WALKCODE_CLAUDE_DAEMON_MODE=off working as a
        # single-variable escape hatch instead of failing startup.
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                self._env(tmp, WALKCODE_CLAUDE_DAEMON_MODE="off")
            )
            self.assertEqual(cfg.agent_options["claude"]["spawn_mode"], "headless")

    def test_explicit_headless_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = ChannelNativeConfig.from_env(
                self._env(tmp, WALKCODE_CLAUDE_SPAWN_MODE="headless")
            )
            self.assertEqual(cfg.agent_options["claude"]["spawn_mode"], "headless")


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


def _inbound(text: str = "帮我修一下测试") -> InboundEvent:
    return InboundEvent(
        event_id="in-1",
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id="m-1",
        root_message_id="root",
        sender_id="owner",
        sender_display="Owner",
        text=text,
    )


def _fake_structured_transport() -> FakeAgentTransport:
    return FakeAgentTransport(
        "claude_headless",
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
    )


class OrchestratorDaemonSpawnerTests(unittest.TestCase):
    def _orchestrator(self, *, daemon_client=None):
        clock = lambda: 1000.0
        sessions = SessionRegistry(now=clock)
        authz = AuthorizationStore(now=clock)
        channel = FakeChannelAdapter("telegram", _channel_caps())
        transports = {"claude_headless": _fake_structured_transport()}
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
        return orchestrator, sessions, channel

    def test_spawner_session_takes_first_turn_via_daemon_reply(self):
        client = _SpawnStubClient()
        orchestrator, sessions, _channel = self._orchestrator(daemon_client=client)
        spawned: list[str] = []

        async def spawner(binding, transport_kind, cwd, actor):
            session = sessions.create_observed_session(
                session_id="tui-claude-spawned",
                binding=binding,
                cwd=cwd,
                external_ref={
                    "source": "walkcode_daemon_spawn",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": AGENT_SESSION_ID,
                    },
                    "daemon_short": SHORT,
                    "daemon_live": True,
                },
                owner=_actor("daemon-writer"),
            )
            orchestrator.authz.grant(session.session_id, actor, SessionRole.OWNER)
            spawned.append(session.session_id)
            return session

        orchestrator.daemon_spawner = spawner
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _inbound(), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "daemon_reply")
        self.assertEqual(spawned, ["tui-claude-spawned"])
        self.assertEqual(client.replies, [(SHORT, "帮我修一下测试")])
        # The headless transport was never asked to launch anything.
        self.assertEqual(orchestrator.transports["claude_headless"].handles, [])

    def test_spawner_none_falls_back_to_headless_start_session(self):
        orchestrator, _sessions, _channel = self._orchestrator(daemon_client=_SpawnStubClient())

        async def spawner(binding, transport_kind, cwd, actor):
            return None

        orchestrator.daemon_spawner = spawner
        result = asyncio.run(
            orchestrator.handle_inbound_event(
                _inbound(), agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(orchestrator.transports["claude_headless"].handles), 1)


class SpawnBgJobReapTests(unittest.IsolatedAsyncioTestCase):
    """spawn_bg_job must reap the just-created worker on ANY probe failure,
    not only ready-timeout — else an orphan bg worker is leaked."""

    async def test_list_jobs_failure_after_bg_reaps(self):
        from walkcode.channel_native.claude_daemon import ClaudeDaemonError

        class _ListFailsClient(_SpawnStubClient):
            async def list_jobs(self):
                raise ClaudeDaemonError("EIO", "socket blip")

        client = _ListFailsClient()
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=client)
        runner, _ = _canned_runner()
        with self.assertRaises(TransportUnavailable):
            await transport.spawn_bg_job("/tmp", bg_runner=runner, ready_timeout=1.0)
        self.assertEqual(client.kills, [SHORT])

    async def test_job_ready_failure_after_bg_reaps(self):
        class _ReadyFailsClient(_SpawnStubClient):
            async def job_ready(self, short):
                raise RuntimeError("ready probe blew up")

        client = _ReadyFailsClient()
        transport = ClaudeDaemonTransport(config_dir="/tmp/profile", client=client)
        runner, _ = _canned_runner()
        with self.assertRaises(TransportUnavailable):
            await transport.spawn_bg_job("/tmp", bg_runner=runner, ready_timeout=1.0)
        self.assertEqual(client.kills, [SHORT])


class _FakeTelegramApi:
    def __init__(self):
        self.token = "fake"
        self.calls = []

    async def call(self, method, payload):
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        return {"ok": True, "result": {}}


def _runtime(tmp: str, **env: str) -> ChannelNativeRuntime:
    cfg = ChannelNativeConfig.from_env(
        {
            "WALKCODE_CHANNEL": "telegram",
            "TELEGRAM_BOT_TOKEN": "fake",
            "WALKCODE_TELEGRAM_TUI_CHAT_ID": "777",
            "WALKCODE_AGENT": "claude",
            "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
            "WALKCODE_CWD": tmp,
            "WALKCODE_CLAUDE_CONFIG_DIR": tmp,
            **env,
        }
    )
    client = _SpawnStubClient()
    return ChannelNativeRuntime.from_config(
        cfg,
        telegram_api=_FakeTelegramApi(),
        transports={
            "claude_headless": _fake_structured_transport(),
            "claude_daemon": ClaudeDaemonTransport(config_dir=tmp, client=client),
        },
    )


def _user_binding() -> ChannelBinding:
    return ChannelBinding(
        "telegram",
        "bot",
        "chat",
        "topic",
        "root",
        capabilities={"initial_title": "修测试"},
    )


class RuntimeDaemonSpawnTests(unittest.TestCase):
    def test_explicit_headless_spawn_mode_skips_daemon_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="headless")
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    _user_binding(), "claude_headless", tmp, _actor()
                )
            )
            self.assertIsNone(session)

    def test_default_spawn_mode_is_headless(self):
        # No env override: the resolved default must skip the daemon spawner
        # so channel-born sessions fall through to the headless SDK path
        # (ADR 0050 single-master default).
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    _user_binding(), "claude_headless", tmp, _actor()
                )
            )
            self.assertIsNone(session)

    def test_daemon_mode_spawns_external_shaped_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="daemon")
            transport = runtime._claude_daemon_transport()

            async def fake_spawn(cwd, *, settings="", cli_path="", **kwargs):
                return {"short": SHORT, "session_id": AGENT_SESSION_ID, "cwd": cwd}

            transport.spawn_bg_job = fake_spawn
            binding = _user_binding()
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    binding, "claude_headless", tmp, _actor()
                )
            )

            self.assertIsNotNone(session)
            self.assertEqual(session.transport_kind, "external_tui")
            self.assertEqual(session.lifecycle_state, "EXTERNAL_OBSERVED_READONLY")
            self.assertEqual(session.writer_owner.kind, "external_tui")
            self.assertEqual(session.transport_ref.get("daemon_short"), SHORT)
            self.assertTrue(session.transport_ref.get("daemon_live"))
            self.assertEqual(binding.capabilities.get("origin"), "daemon_spawn")
            self.assertEqual(session.cached_title, "修测试")
            # The daemon watcher and the hook pipeline both find it by uuid.
            self.assertEqual(
                runtime.state.sessions.find_by_resume_ref(
                    transport_kind="claude_headless",
                    resume_ref={"agent_session_id": AGENT_SESSION_ID},
                ),
                session.session_id,
            )
            # The requesting user owns the session (can stop/submit).
            self.assertTrue(
                runtime.state.authz.can_submit(session.session_id, _actor()).allowed
            )
            # Observer attach must be requested at spawn time — BEFORE the
            # first turn is injected — so dialog state patches are published
            # from the session's very first turn (the daemon only publishes
            # tempo/needs while >=1 attacher is connected).
            self.assertIn(SHORT, transport._observers)

    def test_spawn_failure_returns_none_for_headless_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="daemon")
            transport = runtime._claude_daemon_transport()

            async def failing_spawn(cwd, **kwargs):
                raise TransportUnavailable("daemon is down")

            transport.spawn_bg_job = failing_spawn
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    _user_binding(), "claude_headless", tmp, _actor()
                )
            )
            self.assertIsNone(session)

    def test_non_claude_transport_kind_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="daemon")
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    _user_binding(), "codex_app_server", tmp, _actor()
                )
            )
            self.assertIsNone(session)

    def test_register_failure_reaps_job_and_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="daemon")
            transport = runtime._claude_daemon_transport()

            async def fake_spawn(cwd, *, settings="", cli_path="", **kwargs):
                return {"short": SHORT, "session_id": AGENT_SESSION_ID, "cwd": cwd}

            transport.spawn_bg_job = fake_spawn
            kills: list[str] = []

            async def fake_kill(short, *, signal="SIGTERM"):
                kills.append(short)
                return {"ok": True}

            transport.client.kill = fake_kill

            # Force the registration to blow up after spawn succeeded.
            def boom(*a, **k):
                raise RuntimeError("registry exploded")

            runtime.state.sessions.create_observed_session = boom
            session = asyncio.run(
                runtime._spawn_claude_daemon_native_session(
                    _user_binding(), "claude_headless", tmp, _actor()
                )
            )
            self.assertIsNone(session)
            self.assertEqual(kills, [SHORT])  # orphan job reaped

    def test_spawn_under_ingress_lock_does_not_deadlock(self):
        # Regression for the ADR 0048 review Critical: the real Lark/Telegram
        # entry holds _ingress_lock around handle_inbound_event; the spawner
        # must not re-acquire it (asyncio.Lock is not reentrant → self-deadlock).
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_SPAWN_MODE="daemon")
            transport = runtime._claude_daemon_transport()

            async def fake_spawn(cwd, *, settings="", cli_path="", **kwargs):
                return {"short": SHORT, "session_id": AGENT_SESSION_ID, "cwd": cwd}

            transport.spawn_bg_job = fake_spawn

            async def drive():
                # Exactly what serve_lark_ws / poll_telegram_once do.
                async with runtime._ingress_lock:
                    return await asyncio.wait_for(
                        runtime.orchestrator.handle_inbound_event(
                            _inbound(),
                            agent_transport_kind="claude_headless",
                            cwd=tmp,
                        ),
                        timeout=5.0,
                    )

            result = asyncio.run(drive())
            self.assertTrue(result.accepted)
            self.assertEqual(result.reason, "daemon_reply")
            self.assertEqual(transport.client.replies, [(SHORT, "帮我修一下测试")])


def _wild_job(*, age_seconds: float = 120.0, source: str = "shell") -> dict:
    return {
        "short": SHORT,
        "sessionId": AGENT_SESSION_ID,
        "cwd": "/tmp/wild",
        "source": source,
        "backend": "daemon",
        "tempo": "idle",
        "state": "ready",
        "createdAt": (time.time() - age_seconds) * 1000.0,
    }


class ListAdoptionTests(unittest.TestCase):
    def test_adopts_old_shell_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            session_id = asyncio.run(runtime._maybe_adopt_wild_claude_daemon_job(_wild_job()))
            self.assertTrue(session_id)
            session = runtime.state.sessions.get(session_id)
            self.assertEqual(session.transport_kind, "external_tui")
            self.assertEqual(session.cwd, "/tmp/wild")
            self.assertTrue(session.transport_ref.get("daemon_live"))
            self.assertEqual(session.transport_ref.get("source"), "claude_daemon_list")
            self.assertEqual(
                runtime.state.sessions.find_by_resume_ref(
                    transport_kind="claude_headless",
                    resume_ref={"agent_session_id": AGENT_SESSION_ID},
                ),
                session_id,
            )

    def test_young_job_is_left_for_the_spawner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            session_id = asyncio.run(
                runtime._maybe_adopt_wild_claude_daemon_job(_wild_job(age_seconds=1.0))
            )
            self.assertEqual(session_id, "")

    def test_job_within_spawn_window_not_adopted(self):
        # 45s is older than the retired 30s threshold but still inside the
        # spawner's worst-case register window; must not be adopted, or the
        # watcher would steal the runtime's own in-flight job.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            session_id = asyncio.run(
                runtime._maybe_adopt_wild_claude_daemon_job(_wild_job(age_seconds=45.0))
            )
            self.assertEqual(session_id, "")

    def test_non_shell_source_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            session_id = asyncio.run(
                runtime._maybe_adopt_wild_claude_daemon_job(_wild_job(source="agent"))
            )
            self.assertEqual(session_id, "")

    def test_known_session_returns_existing_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            existing = runtime.state.sessions.create_observed_session(
                session_id="observed-existing",
                binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": AGENT_SESSION_ID,
                    },
                },
                owner=_actor(),
            )
            session_id = asyncio.run(runtime._maybe_adopt_wild_claude_daemon_job(_wild_job()))
            self.assertEqual(session_id, existing.session_id)

    def test_list_adopt_off_disables_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp, WALKCODE_CLAUDE_LIST_ADOPT="off")
            session_id = asyncio.run(runtime._maybe_adopt_wild_claude_daemon_job(_wild_job()))
            self.assertEqual(session_id, "")


class _ObserverStubClient:
    """Client stub whose attach_observe holds until told to drop."""

    def __init__(self):
        self.attach_started = asyncio.Event()
        self.drop = asyncio.Event()
        self.attach_calls = 0
        self.alive: bool | None = True

    async def attach_observe(self, short: str, *, on_ready=None, **kwargs):
        self.attach_calls += 1
        if on_ready is not None:
            on_ready.set()
        self.attach_started.set()
        await self.drop.wait()
        self.drop.clear()
        raise TransportUnavailable("connection dropped")

    async def job_alive(self, short: str):
        return self.alive


class ObserverAttachLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Persistent observer attach (ADR 0048): the daemon only publishes
    tempo/needs state patches while a job has >=1 attacher, so walkcode holds
    one long-lived attach per watched job."""

    def _transport(self, tmp: str) -> tuple[ClaudeDaemonTransport, _ObserverStubClient]:
        client = _ObserverStubClient()
        return ClaudeDaemonTransport(config_dir=tmp, client=client), client

    async def test_ensure_observer_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)
            transport.ensure_observer(SHORT)
            task = transport._observers[SHORT]
            transport.ensure_observer(SHORT)
            self.assertIs(transport._observers[SHORT], task)
            await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
            self.assertEqual(client.attach_calls, 1)
            transport.stop_all_observers()
            await asyncio.gather(task, return_exceptions=True)

    async def test_observer_reconnects_while_job_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)
            with patch(
                "walkcode.channel_native.claude_daemon.OBSERVER_RECONNECT_SECONDS", 0.01
            ):
                transport.ensure_observer(SHORT)
                await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
                client.attach_started.clear()
                client.drop.set()  # connection drops; job still alive
                await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
                self.assertEqual(client.attach_calls, 2)
                transport.stop_all_observers()
                await asyncio.gather(*transport._observers.values(), return_exceptions=True)

    async def test_observer_exits_when_job_is_gone(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)
            transport.ensure_observer(SHORT)
            await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
            client.alive = False
            client.drop.set()
            task = transport._observers[SHORT]
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=2.0)
            self.assertTrue(task.done())
            self.assertEqual(client.attach_calls, 1)

    async def test_stop_observer_cancels_the_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)
            transport.ensure_observer(SHORT)
            await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
            task = transport._observers.get(SHORT)
            transport.stop_observer(SHORT)
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(task.cancelled())
            self.assertNotIn(SHORT, transport._observers)

    async def test_transient_none_probe_does_not_kill_observer(self):
        # A single job_alive()==None (control-plane blip) must NOT end the
        # observer — the round-2 finding. It should reconnect; only
        # OBSERVER_PROBE_GIVEUP consecutive None probes end it.
        from walkcode.channel_native import claude_daemon as cd
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)
            client.alive = None
            with patch.object(cd, "OBSERVER_RECONNECT_SECONDS", 0.001):
                transport.ensure_observer(SHORT)
                # First attach happens, then one drop → None probe → reconnect.
                await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
                client.attach_started.clear()
                client.drop.set()
                await asyncio.wait_for(client.attach_started.wait(), timeout=2.0)
                self.assertGreaterEqual(client.attach_calls, 2)  # survived the blip
                self.assertFalse(transport._observers[SHORT].done())
            transport.stop_all_observers()
            await asyncio.gather(*transport._observers.values(), return_exceptions=True)

    async def test_ready_event_fires_on_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)

            async def attach_and_set(short, *, on_ready=None, **kw):
                client.attach_calls += 1
                if on_ready is not None:
                    on_ready.set()
                await client.drop.wait()
                raise TransportUnavailable("drop")

            client.attach_observe = attach_and_set
            ok = await transport.ensure_observer_ready(SHORT, timeout=2.0)
            self.assertTrue(ok)
            transport.stop_all_observers()
            await asyncio.gather(*transport._observers.values(), return_exceptions=True)

    async def test_ready_times_out_when_attach_never_lands(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport, client = self._transport(tmp)

            async def attach_never_ready(short, *, on_ready=None, **kw):
                # never sets on_ready; blocks until dropped
                await client.drop.wait()
                raise TransportUnavailable("drop")

            client.attach_observe = attach_never_ready
            ok = await transport.ensure_observer_ready(SHORT, timeout=0.05)
            self.assertFalse(ok)
            transport.stop_all_observers()
            await asyncio.gather(*transport._observers.values(), return_exceptions=True)


class WatcherWiringTests(unittest.IsolatedAsyncioTestCase):
    """_sync_claude_daemon_watchers must create subscribe watchers for known
    external-TUI sessions and not let a slow wild-job adoption block them."""

    async def test_known_session_gets_watcher_before_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            transport = runtime._claude_daemon_transport()
            # Register a known external-TUI session for a "known" job.
            known_uuid = "aaaa1111-2222-3333-4444-555566667777"
            known_short = "aaaa1111"
            runtime.state.sessions.create_observed_session(
                session_id="tui-claude-known",
                binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": known_uuid,
                    },
                },
                owner=_actor(),
            )

            order: list[str] = []

            async def fake_list_jobs():
                return [
                    {"short": known_short, "sessionId": known_uuid, "source": "shell",
                     "tempo": "active", "state": "running"},
                    _wild_job(),
                ]

            transport.client.list_jobs = fake_list_jobs

            async def slow_adopt(job):
                order.append("adopt")
                await asyncio.sleep(0.05)
                return ""

            runtime._maybe_adopt_wild_claude_daemon_job = slow_adopt

            def fake_start(session_id, short, transport_, watchers):
                order.append(f"watch:{short}")
                # Sentinel task so the watcher dict is populated without a socket.
                watchers[short] = asyncio.ensure_future(asyncio.sleep(0))

            runtime._start_daemon_watcher_if_eligible = fake_start

            watchers: dict = {}
            await runtime._sync_claude_daemon_watchers(transport, watchers)
            await asyncio.gather(*watchers.values(), return_exceptions=True)

            # Known job's watcher must be wired before the wild-job adoption runs.
            self.assertEqual(order[0], f"watch:{known_short}")
            self.assertIn("adopt", order)
            self.assertLess(order.index(f"watch:{known_short}"), order.index("adopt"))

    async def test_start_watcher_also_ensures_observer_attach(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            transport = runtime._claude_daemon_transport()
            known_uuid = "aaaa1111-2222-3333-4444-555566667777"
            known_short = "aaaa1111"
            runtime.state.sessions.create_observed_session(
                session_id="tui-claude-known",
                binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": known_uuid,
                    },
                },
                owner=_actor(),
            )
            watchers: dict = {}
            runtime._start_daemon_watcher_if_eligible(
                "tui-claude-known", known_short, transport, watchers
            )
            self.assertIn(known_short, watchers)
            self.assertIn(known_short, transport._observers)
            transport.stop_all_observers()
            for task in watchers.values():
                task.cancel()
            await asyncio.gather(
                *watchers.values(),
                *[t for t in transport._observers.values()],
                return_exceptions=True,
            )

    async def test_sync_rebuilds_dead_observer_while_watcher_alive(self):
        # The Critical: observer exited (transient outage) but the subscribe
        # watcher is still alive. The old sync `continue`d on `short in
        # watchers` and never rebuilt the observer → zero attachers → frozen
        # state patches. The sync must re-ensure the observer every tick.
        known_uuid = "aaaa1111-2222-3333-4444-555566667777"
        known_short = "aaaa1111"
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            transport = runtime._claude_daemon_transport()
            runtime.state.sessions.create_observed_session(
                session_id="tui-claude-known",
                binding=ChannelBinding("telegram", "bot", "chat", "topic", "root"),
                cwd=tmp,
                external_ref={
                    "source": "native_tui_hook",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": known_uuid,
                    },
                },
                owner=_actor(),
            )

            async def fake_list_jobs():
                return [{"short": known_short, "sessionId": known_uuid,
                         "source": "shell", "tempo": "active", "state": "running"}]

            transport.client.list_jobs = fake_list_jobs

            # A live subscribe watcher and a DONE observer task (simulating an
            # observer that exited on a transient outage).
            async def _idle():
                await asyncio.sleep(3600)
            live_watcher = asyncio.ensure_future(_idle())
            watchers = {known_short: live_watcher}
            done_obs = asyncio.ensure_future(asyncio.sleep(0))
            await done_obs
            transport._observers[known_short] = done_obs

            await runtime._sync_claude_daemon_watchers(transport, watchers)

            # Same live watcher kept (not orphaned), observer rebuilt fresh.
            self.assertIs(watchers[known_short], live_watcher)
            self.assertIn(known_short, transport._observers)
            self.assertIsNot(transport._observers[known_short], done_obs)
            self.assertFalse(transport._observers[known_short].done())

            live_watcher.cancel()
            transport.stop_all_observers()
            await asyncio.gather(
                live_watcher, *transport._observers.values(), return_exceptions=True
            )


class BindingCapabilityGuardTests(unittest.TestCase):
    def test_daemon_spawn_binding_is_not_repainted(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = _runtime(tmp)
            binding = ChannelBinding(
                "telegram",
                "bot",
                "chat",
                "topic",
                "root",
                capabilities={"origin": "daemon_spawn"},
            )
            session = runtime.state.sessions.create_observed_session(
                session_id="tui-claude-guard",
                binding=binding,
                cwd=tmp,
                external_ref={
                    "source": "walkcode_daemon_spawn",
                    "resume_ref": {
                        "transport_kind": "claude_headless",
                        "agent_session_id": AGENT_SESSION_ID,
                    },
                },
                owner=_actor(),
            )
            changed = runtime._ensure_tui_observed_binding_capabilities(session)
            self.assertFalse(changed)
            self.assertNotIn("readonly_topic", binding.capabilities)
            self.assertNotIn("status_card", binding.capabilities)
            self.assertEqual(binding.capabilities.get("origin"), "daemon_spawn")


if __name__ == "__main__":
    unittest.main()
