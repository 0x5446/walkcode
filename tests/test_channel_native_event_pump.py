"""Tests for the persistent per-worker event pump (ADR 0052).

The incident this guards against: a Feishu-started session ran a background
task (deep-research Workflow); when it completed, the CLI produced the final
report in a self-initiated turn inside the original worker — and the old
per-turn drain model never consumed it, so the report never reached the topic.

The fake client here is a scriptable multi-turn stream: ``receive_messages``
yields whatever the test feeds, across as many turns as the script wants,
and ends only when the test closes the stream (worker death) — mirroring the
real claude_agent_sdk contract the pump relies on.
"""

import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    BlockedReason,
    CapabilityUnsupported,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportHandle,
    TurnInput,
)

_EOF = object()


class _Options:
    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.can_use_tool = kwargs.get("can_use_tool")


class _PumpClient:
    """Scriptable multi-turn fake SDK client (receive_messages semantics)."""

    created: list = []  # replaced per test class instance via _client_class()

    def __init__(self, options=None):
        self.options = options
        self.option_kwargs = dict(getattr(options, "kwargs", {}) or {})
        self.queries: list[str] = []
        self.disconnect_calls = 0
        self.interrupt_calls: list = []
        self._queue: asyncio.Queue = asyncio.Queue()
        type(self).created.append(self)

    async def connect(self, prompt=None):
        return None

    async def query(self, prompt, session_id="default"):
        self.queries.append(str(prompt))

    def feed(self, *messages):
        for message in messages:
            self._queue.put_nowait(message)

    def end_stream(self):
        self._queue.put_nowait(_EOF)

    async def disconnect(self):
        self.disconnect_calls += 1
        # Process death closes stdout: the stream ends for any reader.
        self._queue.put_nowait(_EOF)

    async def receive_messages(self):
        while True:
            item = await self._queue.get()
            if item is _EOF:
                return
            yield item


def _client_class(**attrs):
    return type("PumpClient", (_PumpClient,), {"created": [], **attrs})


def _make_sdk(client_cls):
    class SDK:
        ClaudeAgentOptions = _Options
        ClaudeSDKClient = client_cls

    return SDK


def _delta(text):
    return {"content": [{"type": "text", "text": text}]}


def _result(text="done", session_id="claude-x"):
    return {"type": "result", "result": text, "session_id": session_id}


async def _wait_until(predicate, what="condition", rounds=20000):
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{what} never became true")


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


def _build(client_cls, *, defer=True, authz=None, on_state_changed=None):
    transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(client_cls))
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orch = Orchestrator(
        sessions=SessionRegistry(),
        interactions=InteractionStore(),
        outbox=DurableOutbox(),
        channels={"telegram": channel},
        transports={"claude_headless": transport},
        authz=authz,
        defer_event_drain=defer,
        on_state_changed=on_state_changed,
    )
    return transport, channel, orch


_OWNER = ActorRef("telegram", "owner", "Owner")
_BINDING = ChannelBinding("telegram", "bot", "chat", "topic", "root")


async def _start(orch):
    session = await orch.start_session(_BINDING, "claude_headless", "/tmp/project", _OWNER)
    return session


def _texts(channel):
    out = []
    for item in channel.sent_views:
        view = item.get("view", {})
        if view.get("type") in ("turn_delta", "turn_completed"):
            out.append(str(view.get("text") or view.get("message") or ""))
    return out


class EventPumpRelayTests(unittest.TestCase):
    def test_self_initiated_turn_output_reaches_channel(self):
        """Incident regression: background-task results must reach the topic."""

        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="调研"), actor=_OWNER, generation=session.generation
            )
            client.feed(_delta("后台任务已启动"), _result("先给你个占位", session_id="claude-a2b"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            # Self-initiated continuation: NO query was submitted — the CLI
            # woke itself when the background task finished.
            client.feed(_delta("最终报告全文"), _result("最终报告全文", session_id="claude-a2b"))
            await _wait_until(
                lambda: any("最终报告全文" in t for t in _texts(channel)),
                "self-initiated output relayed",
            )
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "back to IDLE")
            self.assertEqual(session.transport_ref.get("agent_session_id"), "claude-a2b")
            self.assertEqual(len(client.queries), 1)
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_idle_with_live_pump_reuses_worker(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_result("r1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            self.assertEqual(client.queries, ["one", "two"])
            # Same worker, same client, no resume.
            self.assertEqual(len(client_cls.created), 1)
            self.assertEqual(len(transport._clients), 1)
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_dead_pump_resumes_new_worker_and_reaps_old(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client1 = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client1.feed(_result("r1", session_id="claude-live"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            client1.end_stream()  # worker dies while IDLE
            await _wait_until(lambda: not orch._event_pumps, "pump exited")
            # Pump epilogue reaped the dead worker's client entry.
            self.assertEqual(transport._clients, {})
            self.assertEqual(client1.disconnect_calls, 1)
            self.assertEqual(session.lifecycle_state, "IDLE")  # silent death while idle

            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            self.assertEqual(len(client_cls.created), 2)
            client2 = client_cls.created[1]
            self.assertEqual(client2.option_kwargs.get("resume"), "claude-live")
            self.assertEqual(client2.queries, ["two"])
            self.assertTrue(orch._event_pumps, "new pump running")
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_pump_death_mid_turn_marks_error_recoverable(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_delta("working..."))
            await _wait_until(lambda: session.lifecycle_state == "ACTIVE", "mid-turn")
            client.end_stream()  # worker dies mid-turn
            await _wait_until(lambda: not orch._event_pumps, "pump exited")
            self.assertEqual(session.lifecycle_state, "ERROR_RECOVERABLE")
            self.assertIsNone(session.writer_lease)
            errors = [
                item for item in channel.sent_views
                if item.get("view", {}).get("type") == "error"
            ]
            self.assertTrue(errors, "mid-turn worker death must surface an error card")

        asyncio.run(scenario())

    def test_dedup_resets_across_turns(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            # Turn 1: delta "X" then completed "X" — completed is deduped.
            client.feed(_delta("X"), _result("X"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            self.assertEqual(_texts(channel).count("X"), 1)
            # Self-initiated turn 2 whose final text is also "X": must be sent.
            client.feed(_result("X"))
            await _wait_until(lambda: _texts(channel).count("X") == 2, "turn2 X relayed")
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_state_changed_fires_on_turn_completed_and_pump_exit(self):
        async def scenario():
            counts = {"n": 0}

            def bump():
                counts["n"] += 1

            client_cls = _client_class()
            transport, channel, orch = _build(client_cls, on_state_changed=bump)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            base = counts["n"]
            client.feed(_result("r1"))
            await _wait_until(lambda: counts["n"] > base, "state saved on turn completion")
            after_turn = counts["n"]
            client.end_stream()
            await _wait_until(lambda: not orch._event_pumps, "pump exited")
            self.assertGreater(counts["n"], after_turn, "state saved on pump exit")

        asyncio.run(scenario())


class EventPumpLifecycleTests(unittest.TestCase):
    def test_generation_bump_stops_pump_silently(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_delta("working..."))
            await _wait_until(lambda: session.lifecycle_state == "ACTIVE", "mid-turn")
            session.generation += 1  # a claim/takeover moved ownership
            client.feed(_delta("late event"))
            await _wait_until(lambda: not orch._event_pumps, "pump exited")
            # Silent exit: no error card, no worker reap (claim path owns it).
            self.assertEqual(session.lifecycle_state, "ACTIVE")
            self.assertEqual(client.disconnect_calls, 0)
            errors = [
                item for item in channel.sent_views
                if item.get("view", {}).get("type") == "error"
            ]
            self.assertEqual(errors, [])

        asyncio.run(scenario())

    def test_external_claim_cancels_pump_and_reaps_worker(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_result("r1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            prior_gen = session.generation
            prior_handle = TransportHandle(
                handle_id=str(session.transport_ref.get("handle_id", "")),
                transport_kind="claude_headless",
            )
            handoff = orch.sessions.handoff_to_external_tui(
                session.session_id,
                generation=prior_gen,
                owner=_OWNER,
                resume_ref={"agent_session_id": "claude-x"},
                external_ref={"pid": 4242},
            )
            self.assertTrue(handoff.accepted)
            await orch.settle_hitls_for_external_claim(
                session,
                prior_transport_kind="claude_headless",
                prior_handle=prior_handle,
                through_generation=prior_gen,
            )
            self.assertEqual(orch._event_pumps, {})
            self.assertEqual(client.disconnect_calls, 1)
            self.assertEqual(transport._clients, {})
            errors = [
                item for item in channel.sent_views
                if item.get("view", {}).get("type") == "error"
            ]
            self.assertEqual(errors, [], "claim must not surface a spurious worker-death card")

        asyncio.run(scenario())

    def test_close_session_cancels_pump_and_disconnects(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls, authz=AuthorizationStore())
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_result("r1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            result = await orch.close_session(
                session.session_id, actor=_OWNER, reason="user_closed"
            )
            self.assertTrue(result.accepted)
            self.assertEqual(session.status, "stopped")
            self.assertEqual(client.disconnect_calls, 1)
            self.assertEqual(orch._event_pumps, {})
            errors = [
                item for item in channel.sent_views
                if item.get("view", {}).get("type") == "error"
            ]
            self.assertEqual(errors, [], "closing a session must not read as a worker crash")

        asyncio.run(scenario())

    def test_close_session_succeeds_after_worker_already_gone(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls, authz=AuthorizationStore())
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_result("r1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            client.end_stream()
            await _wait_until(lambda: not orch._event_pumps, "pump exited")
            self.assertEqual(transport._clients, {})
            # Regression: shutdown on a vanished worker used to report
            # NOT_FOUND and wedge close_session forever.
            result = await orch.close_session(
                session.session_id, actor=_OWNER, reason="user_closed"
            )
            self.assertTrue(result.accepted)
            self.assertEqual(session.status, "stopped")

        asyncio.run(scenario())

    def test_restart_shape_resumes_via_agent_session_id(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            # A persisted IDLE session from a previous runtime: stale handle,
            # durable agent_session_id, and (naturally) no pump registered.
            session = orch.sessions.create_structured_session(
                session_id="sess-restart",
                binding=_BINDING,
                transport_kind="claude_headless",
                transport_ref={"handle_id": "stale-handle", "agent_session_id": "claude-old"},
                cwd="/tmp/project",
                owner=_OWNER,
            )
            session.lifecycle_state = "IDLE"
            session.writer_lease = None
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="continue"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            self.assertEqual(len(client_cls.created), 1)
            client = client_cls.created[0]
            self.assertEqual(client.option_kwargs.get("resume"), "claude-old")
            self.assertEqual(client.queries, ["continue"])
            self.assertTrue(orch._event_pumps, "pump started for the resumed worker")
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_once_mode_has_no_pump_and_drains_synchronously(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls, defer=False)
            session = await _start(orch)
            self.assertEqual(orch._event_pumps, {}, "no pump outside serve mode")
            client = client_cls.created[0]
            # Pre-script the whole turn: the synchronous drain collects the
            # stream until it ends, exactly like serve --once expects.
            client.feed(_delta("hello"), _result("hello done"))
            client.end_stream()
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            self.assertIn("hello", _texts(channel))
            self.assertEqual(orch._event_pumps, {})

        asyncio.run(scenario())


class EventPumpTransportGuardTests(unittest.TestCase):
    def test_second_stream_on_same_handle_raises(self):
        async def scenario():
            client_cls = _client_class()
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(client_cls))
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            client = client_cls.created[0]
            client.feed(_delta("x"))
            stream1 = await transport.open_event_stream(handle)
            first = await stream1.__anext__()  # activates the guard
            self.assertIsNotNone(first)
            stream2 = await transport.open_event_stream(handle)
            with self.assertRaises(CapabilityUnsupported):
                await stream2.__anext__()
            await stream1.aclose()
            # Guard releases with the stream: a fresh one may open again.
            client.feed(_delta("y"))
            stream3 = await transport.open_event_stream(handle)
            self.assertIsNotNone(await stream3.__anext__())
            await stream3.aclose()

        asyncio.run(scenario())

    def test_background_drain_noops_while_pump_alive(self):
        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            handle = TransportHandle(
                handle_id=str(session.transport_ref.get("handle_id", "")),
                transport_kind="claude_headless",
            )
            self.assertTrue(orch._event_pumps)
            orch._start_background_event_drain(session.session_id, transport, handle)
            self.assertEqual(set(orch._background_event_drains), set())
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_interrupt_supports_no_arg_clients(self):
        async def scenario():
            class _NoArgInterrupt(_PumpClient):
                created = []

                async def interrupt(self):
                    self.interrupt_calls.append(())

            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_NoArgInterrupt))
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            result = await transport.interrupt(handle, "user_requested")
            self.assertTrue(result.accepted)
            self.assertEqual(_NoArgInterrupt.created[0].interrupt_calls, [()])

            class _ReasonInterrupt(_PumpClient):
                created = []

                async def interrupt(self, reason):
                    self.interrupt_calls.append(reason)

            transport2 = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_ReasonInterrupt))
            handle2 = await transport2.launch_session(cwd="/tmp/project", session_id="s2")
            result2 = await transport2.interrupt(handle2, "user_requested")
            self.assertTrue(result2.accepted)
            self.assertEqual(_ReasonInterrupt.created[0].interrupt_calls, ["user_requested"])

        asyncio.run(scenario())

    def test_shutdown_disconnects_and_tolerates_missing_worker(self):
        async def scenario():
            client_cls = _client_class()
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(client_cls))
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            client = client_cls.created[0]
            result = await transport.shutdown(handle, "test")
            self.assertTrue(result.accepted)
            self.assertEqual(client.disconnect_calls, 1)
            self.assertEqual(transport._clients, {})
            # Second shutdown: vacuously successful, not NOT_FOUND.
            again = await transport.shutdown(handle, "test")
            self.assertTrue(again.accepted)
            self.assertEqual(again.state, "already_stopped")

        asyncio.run(scenario())

    def test_shutdown_failure_keeps_client_for_retry(self):
        """deep-review Cluster A: a failed disconnect must not drop the only
        reference to a possibly-live subprocess."""

        class _FlakyDisconnect(_PumpClient):
            created = []
            fail = True

            async def disconnect(self):
                if type(self).fail:
                    self.disconnect_calls += 1
                    raise RuntimeError("boom")
                await super().disconnect()

        async def scenario():
            transport = ClaudeHeadlessTransport(sdk_loader=lambda: _make_sdk(_FlakyDisconnect))
            handle = await transport.launch_session(cwd="/tmp/project", session_id="s1")
            client = _FlakyDisconnect.created[0]
            result = await transport.shutdown(handle, "test")
            # Accepted (session cleanup must proceed) but the client stays
            # registered so a later retry can still reach the process.
            self.assertTrue(result.accepted)
            self.assertIn(handle.handle_id, transport._clients)
            _FlakyDisconnect.fail = False
            retry = await transport.shutdown(handle, "test")
            self.assertTrue(retry.accepted)
            self.assertEqual(transport._clients, {})
            self.assertEqual(client.disconnect_calls, 2)

        asyncio.run(scenario())

    def test_no_receive_messages_client_disables_pump(self):
        """deep-review Cluster C: without receive_messages the pump would reap
        a healthy worker every turn — fail closed to the per-turn drain."""

        class _ResponseOnly(_PumpClient):
            created = []
            receive_messages = None  # shadow: not callable

            async def receive_response(self):
                yield _delta("hello")
                yield _result("hello done")

        async def scenario():
            transport, channel, orch = _build(_ResponseOnly)
            self.assertFalse(transport.capabilities().persistent_event_stream)
            session = await _start(orch)
            self.assertEqual(orch._event_pumps, {}, "no pump for per-turn clients")
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            drains = list(orch._background_event_drains)
            self.assertTrue(drains, "legacy background drain must carry the turn")
            await asyncio.wait_for(asyncio.gather(*drains, return_exceptions=True), timeout=5.0)
            self.assertIn("hello", _texts(channel))

        asyncio.run(scenario())


class EventPumpSubmitBoundaryTests(unittest.TestCase):
    def test_error_recoverable_resumes_instead_of_reusing_pump(self):
        """deep-review Cluster E: a worker that reported SESSION_ERROR keeps
        its stream open, but the next inbound must NOT be queried into it."""

        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client1 = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            # Record a durable resume ref first, then flip to error state.
            client1.feed(_result("r1", session_id="claude-live"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            client1.feed({"is_error": True, "error": "sdk exploded"})
            await _wait_until(
                lambda: session.lifecycle_state == "ERROR_RECOVERABLE", "error state"
            )
            self.assertTrue(orch._pump_alive(session), "stream is still open")
            result = await orch.submit_user_input(
                session.session_id, TurnInput(text="two"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(result.accepted)
            self.assertEqual(len(client_cls.created), 2, "must resume a fresh worker")
            client2 = client_cls.created[1]
            self.assertEqual(client2.option_kwargs.get("resume"), "claude-live")
            self.assertEqual(client2.queries, ["two"])
            self.assertEqual(client1.queries, ["one"], "errored worker gets no new turn")
            self.assertEqual(client1.disconnect_calls, 1, "errored worker reaped")
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())

    def test_inbound_during_self_initiated_turn_is_lease_blocked(self):
        """ADR 0052 documented behavior: mid self-initiated turn, an inbound
        gets LEASE_EXPIRED and relies on channel redelivery."""

        async def scenario():
            client_cls = _client_class()
            transport, channel, orch = _build(client_cls)
            session = await _start(orch)
            client = client_cls.created[0]
            await orch.submit_user_input(
                session.session_id, TurnInput(text="one"), actor=_OWNER, generation=session.generation
            )
            client.feed(_result("r1"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "turn1 IDLE")
            # Self-initiated turn begins: ACTIVE with no writer lease.
            client.feed(_delta("后台整理中"))
            await _wait_until(lambda: session.lifecycle_state == "ACTIVE", "self turn ACTIVE")
            blocked = await orch.submit_user_input(
                session.session_id, TurnInput(text="early"), actor=_OWNER, generation=session.generation
            )
            self.assertFalse(blocked.accepted)
            self.assertEqual(blocked.reason, BlockedReason.LEASE_EXPIRED)
            self.assertEqual(client.queries, ["one"], "no query during the self turn")
            # Turn finishes → redelivered message goes into the live worker.
            client.feed(_result("done"))
            await _wait_until(lambda: session.lifecycle_state == "IDLE", "self turn done")
            retry = await orch.submit_user_input(
                session.session_id, TurnInput(text="early"), actor=_OWNER, generation=session.generation
            )
            self.assertTrue(retry.accepted)
            self.assertEqual(client.queries, ["one", "early"])
            await orch.stop_all_event_pumps()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
