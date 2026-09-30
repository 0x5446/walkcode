import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    DeliveryStatus,
    DurableOutbox,
    FakeChannelAdapter,
    HitlStore,
    InboundEvent,
    InboundLedger,
    InteractionStore,
    JsonFileStateStore,
    LaunchSpec,
    Orchestrator,
    OutboxDispatcher,
    StateSnapshot,
    PermanentDeliveryError,
    SessionRegistry,
    SessionRole,
    TransientDeliveryError,
    TransportCapabilities,
    TurnInput,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "u1") -> ActorRef:
    return ActorRef(channel_kind="lark", actor_id=actor_id, display_name=f"User {actor_id}")


def _binding() -> ChannelBinding:
    return ChannelBinding(
        channel_kind="lark",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        root_message_id="root",
    )


def _channel_caps() -> ChannelCapabilities:
    return ChannelCapabilities(
        editable_message=True,
        private_callback_ack=True,
        attachment_download=True,
    )


class PersistenceTests(unittest.TestCase):
    def test_failed_atomic_write_keeps_previous_snapshot_and_removes_temp(self):
        snapshot = StateSnapshot(SessionRegistry(), InteractionStore(), DurableOutbox(),
                                 AuthorizationStore(), InboundLedger())
        with tempfile.TemporaryDirectory() as directory:
            store = JsonFileStateStore(Path(directory) / "state.json")
            store.save(snapshot)
            original = store.path.read_bytes()
            for target in ("json.dump", "os.fsync", "os.replace"):
                with self.subTest(target=target), patch(
                    f"walkcode.channel_native.{target}", side_effect=OSError("disk failure")
                ):
                    with self.assertRaises(OSError):
                        store.save(snapshot)
                self.assertEqual(store.path.read_bytes(), original)
                self.assertEqual(list(Path(directory).iterdir()), [store.path])
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_state_snapshot_round_trips_core_durable_state(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        structured = sessions.create_structured_session(
            session_id="s1",
            binding=_binding(),
            transport_kind="claude_headless",
            transport_ref={"handle_id": "h1", "session_id": "claude-1"},
            cwd="/tmp/project",
            owner=_actor("owner"),
        )
        observed = sessions.create_observed_session(
            session_id="observed-1",
            binding=ChannelBinding("lark", "bot", "chat", "topic", "observed-root"),
            cwd="/tmp/project",
            external_ref={"pid": 123},
            owner=_actor("owner"),
        )
        blocked = sessions.block_input(
            observed.session_id,
            actor=_actor("owner"),
            turn=TurnInput(text="blocked"),
            generation=observed.generation,
        )
        interactions = InteractionStore(now=clock)
        ctx = interactions.register_permission(
            session_id=structured.session_id,
            generation=structured.generation,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
            actions=["allow"],
        )
        token = interactions.create_callback_token(ctx.interaction_id, "allow", generation=structured.generation)
        outbox = DurableOutbox(now=clock)
        outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "pending"},
            idempotency_key="k1",
        )
        authz = AuthorizationStore()
        authz.grant(structured.session_id, _actor("owner"), SessionRole.OWNER)
        ledger = InboundLedger(now=clock)
        self.assertTrue(ledger.record("evt-1"))
        hitls = HitlStore(now=clock)
        hitl = hitls.register_request(
            session_id=structured.session_id,
            generation=structured.generation,
            transport_kind=structured.transport_kind,
            transport_request_id="approval-1",
            native_method="item/commandExecution/requestApproval",
            prompt_kind="permission",
        )
        hitls.attach_interaction(hitl.hitl_request_id, ctx.interaction_id)
        hitls.mark_decided(hitl.hitl_request_id)

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json", now=clock)
            store.save(
                StateSnapshot(
                    sessions=sessions,
                    interactions=interactions,
                    outbox=outbox,
                    authz=authz,
                    inbound_ledger=ledger,
                    hitls=hitls,
                )
            )
            restored = store.load()

        restored_structured = restored.sessions.get(structured.session_id)
        restored_observed = restored.sessions.get(observed.session_id)
        self.assertEqual(restored_structured.writer_owner.kind, "orchestrator")
        self.assertEqual(
            restored_observed.blocked_inputs[blocked.blocked_input_id].text,
            "blocked",
        )
        self.assertEqual(restored.outbox.pending_count(), 1)
        self.assertTrue(
            restored.interactions.decide_from_token(
                token,
                actor=_actor("owner"),
                current_generation=structured.generation,
            ).accepted
        )
        self.assertTrue(restored.authz.can_submit(structured.session_id, _actor("owner")).allowed)
        self.assertFalse(restored.inbound_ledger.record("evt-1"))
        restored_hitl = restored.hitls.get(hitl.hitl_request_id)
        self.assertEqual(restored_hitl.status, "decided")
        self.assertEqual(restored_hitl.interaction_id, ctx.interaction_id)
        self.assertEqual(restored_hitl.decided_at, clock())

    def test_state_snapshot_round_trips_retention_metadata(self):
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        interactions = InteractionStore(now=clock, token_ttl=10.0, decided_retention=20.0)
        ctx = interactions.register_permission(
            session_id="s1",
            generation=1,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
            actions=["allow"],
        )
        token = interactions.create_callback_token(ctx.interaction_id, "allow", generation=1)
        outbox = DurableOutbox(
            now=clock,
            sent_retention=30.0,
            dead_retention=40.0,
        )
        sent = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "sent"},
            idempotency_key="k1",
        )
        outbox.record_result(sent.delivery_id, "sent")

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json", now=clock)
            store.save(
                StateSnapshot(
                    sessions=sessions,
                    interactions=interactions,
                    outbox=outbox,
                    authz=AuthorizationStore(),
                    inbound_ledger=InboundLedger(now=clock),
                )
            )
            restored = store.load()

        self.assertEqual(restored.interactions.token_count(), 1)
        self.assertTrue(
            restored.interactions.decide_from_token(
                token,
                actor=_actor("owner"),
                current_generation=1,
            ).accepted
        )
        self.assertEqual(restored.outbox.sent_count(), 1)
        self.assertEqual(restored.outbox.get(sent.delivery_id).finished_at, clock.now)


class OutboxReliabilityTests(unittest.TestCase):
    def test_failed_claim_persistence_stops_delivery_and_can_recover(self):
        clock = _Clock()
        outbox = DurableOutbox(now=clock)
        outbox.enqueue(channel_binding_key=_binding().key(),
                       view_model={"type": "text", "text": "durable"}, idempotency_key="k")
        channel = FakeChannelAdapter("lark", _channel_caps())

        def fail_save():
            raise OSError("disk full")

        dispatcher = OutboxDispatcher(outbox, {"lark": channel}, on_state_changed=fail_save)
        with self.assertRaises(OSError):
            asyncio.run(dispatcher.flush_once())
        self.assertEqual(channel.sent_views, [])
        self.assertEqual(len(outbox.to_dict()["pending"]), 1)
        clock.now += 61
        dispatcher.on_state_changed = lambda: None
        asyncio.run(dispatcher.flush_once())
        self.assertEqual(len(channel.sent_views), 1)

    def test_failed_sent_persistence_surfaces_without_resending_in_process(self):
        outbox = DurableOutbox()
        outbox.enqueue(channel_binding_key=_binding().key(),
                       view_model={"type": "text", "text": "durable"}, idempotency_key="k")
        channel = FakeChannelAdapter("lark", _channel_caps())
        from unittest.mock import Mock
        save = Mock(side_effect=[None, OSError("disk full")])
        dispatcher = OutboxDispatcher(outbox, {"lark": channel}, on_state_changed=save)
        with self.assertRaises(OSError):
            asyncio.run(dispatcher.flush_once())
        dispatcher.on_state_changed = lambda: None
        asyncio.run(dispatcher.flush_once())
        self.assertEqual(len(channel.sent_views), 1)

    def test_transient_delivery_uses_backoff_and_eventually_dead_letters(self):
        from walkcode.channel_native import OutboxDispatcher

        clock = _Clock()
        outbox = DurableOutbox(now=clock, max_attempts=2, base_retry_delay=10.0)
        channel = FakeChannelAdapter("lark", _channel_caps())
        attempts = {"count": 0}

        async def always_transient(_binding, _view):
            attempts["count"] += 1
            raise TransientDeliveryError("rate limited")

        channel.send_view = always_transient
        outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "retry"},
            idempotency_key="k1",
        )
        dispatcher = OutboxDispatcher(outbox, {"lark": channel})

        asyncio.run(dispatcher.flush_once())
        asyncio.run(dispatcher.flush_once())
        self.assertEqual(attempts["count"], 1)
        self.assertEqual(outbox.pending_count(), 1)

        clock.now += 10.0
        asyncio.run(dispatcher.flush_once())

        self.assertEqual(attempts["count"], 2)
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.dead_count(), 1)

    def test_transient_delivery_retry_after_overrides_short_backoff(self):
        from walkcode.channel_native import OutboxDispatcher

        clock = _Clock()
        outbox = DurableOutbox(now=clock, max_attempts=3, base_retry_delay=1.0)
        channel = FakeChannelAdapter("lark", _channel_caps())

        async def rate_limited(_binding, _view):
            raise TransientDeliveryError("rate limited", retry_after=30.0)

        channel.send_view = rate_limited
        item = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "retry"},
            idempotency_key="k1",
        )

        asyncio.run(OutboxDispatcher(outbox, {"lark": channel}).flush_once())

        self.assertEqual(outbox.pending_count(), 1)
        self.assertEqual(outbox.get(item.delivery_id).next_attempt_at, clock.now + 30.0)

    def test_concurrent_dispatchers_send_one_claimed_delivery_once(self):
        from walkcode.channel_native import OutboxDispatcher

        clock = _Clock()
        outbox = DurableOutbox(now=clock)
        channel = FakeChannelAdapter("lark", _channel_caps())
        sends = {"count": 0}
        release = asyncio.Event()

        async def slow_send(_binding, _view):
            sends["count"] += 1
            await release.wait()
            return "msg-1"

        channel.send_view = slow_send
        outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "once"},
            idempotency_key="k1",
        )
        first = OutboxDispatcher(outbox, {"lark": channel}, owner="first")
        second = OutboxDispatcher(outbox, {"lark": channel}, owner="second")

        async def run():
            task1 = asyncio.create_task(first.flush_once())
            await asyncio.sleep(0)
            task2 = asyncio.create_task(second.flush_once())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(task1, task2)

        asyncio.run(run())

        self.assertEqual(sends["count"], 1)
        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.sent_count(), 1)

    def test_claimed_delivery_is_not_ready_until_claim_expires(self):
        clock = _Clock()
        outbox = DurableOutbox(now=clock)
        item = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "leased"},
            idempotency_key="k1",
        )

        claimed = outbox.claim_ready(owner="runtime-a", lease_ttl=30.0)

        self.assertEqual([value.delivery_id for value in claimed], [item.delivery_id])
        self.assertEqual(outbox.pending_items(), [])
        clock.now += 31.0
        self.assertEqual([value.delivery_id for value in outbox.pending_items()], [item.delivery_id])


class RetentionPolicyTests(unittest.TestCase):
    def test_interaction_compaction_removes_expired_open_state_and_awaiting_binding(self):
        clock = _Clock()
        store = InteractionStore(now=clock, token_ttl=10.0, decided_retention=30.0)
        ctx = store.register_ask_user_question(
            session_id="s1",
            generation=1,
            questions=[{"prompt": "Pick", "options": ["A"], "allow_other": True}],
        )
        store.create_callback_token(ctx.interaction_id, "answer:0:0", generation=1)
        store.begin_awaiting_other(ctx.interaction_id, _binding().key(), question_index=0)

        clock.now += 11.0
        removed = store.compact()

        self.assertEqual(removed["interactions"], 1)
        self.assertEqual(store.interaction_count(), 0)
        self.assertEqual(store.token_count(), 0)
        self.assertEqual(store.awaiting_other_count(), 0)
        self.assertFalse(
            store.answer_awaiting_other(
                _binding().key(),
                actor=_actor("owner"),
                text="custom",
                current_generation=1,
            ).accepted
        )

    def test_interaction_compaction_keeps_decisions_until_retention_expires(self):
        clock = _Clock()
        store = InteractionStore(now=clock, token_ttl=10.0, decided_retention=20.0)
        ctx = store.register_permission(
            session_id="s1",
            generation=1,
            tool_name="Bash",
            tool_input={"cmd": "pwd"},
            actions=["allow"],
        )
        token = store.create_callback_token(ctx.interaction_id, "allow", generation=1)

        self.assertTrue(
            store.decide_from_token(token, actor=_actor("owner"), current_generation=1).accepted
        )
        clock.now += 19.0
        store.compact()
        self.assertEqual(store.interaction_count(), 1)
        self.assertEqual(store.token_count(), 0)

        clock.now += 2.0
        removed = store.compact()

        self.assertEqual(removed["interactions"], 1)
        self.assertEqual(store.interaction_count(), 0)

    def test_sent_message_id_survives_a_state_round_trip(self):
        # A blocking-gate card is retired by editing it after a restart too;
        # that needs the platform message id to be persisted with the item.
        outbox = DurableOutbox(now=lambda: 1000.0)
        item = outbox.enqueue(
            channel_binding_key=("lark", "bot", "chat", "", "root"),
            view_model={"type": "text", "text": "card"},
            idempotency_key="s1:0:gate:toolu_1",
        )
        outbox.record_result(item.delivery_id, DeliveryStatus.SENT, message_id="om_card")

        restored = DurableOutbox.from_dict(outbox.to_dict(), now=lambda: 1000.0)

        self.assertEqual(restored.sent_message_id("s1:0:gate:toolu_1"), "om_card")
        self.assertEqual(restored.sent_message_id("missing"), "")

    def test_sent_items_keep_only_the_view_type(self):
        # Delivered items stay a day for dedupe and the message id; their card
        # bodies were ~1 MB of dead weight in a busy state file.
        outbox = DurableOutbox(now=lambda: 1000.0)
        item = outbox.enqueue(
            channel_binding_key=("lark", "bot", "chat", "", "root"),
            view_model={"type": "turn_completed", "message": "x" * 10000},
            idempotency_key="k-body",
        )
        outbox.record_result(item.delivery_id, DeliveryStatus.SENT, message_id="om_1")
        self.assertEqual(outbox.get(item.delivery_id).view_model, {"type": "turn_completed"})

        legacy = outbox.to_dict()
        legacy["sent"][item.delivery_id]["view_model"] = {"type": "text", "text": "y" * 10000}
        restored = DurableOutbox.from_dict(legacy, now=lambda: 1000.0)
        self.assertEqual(restored.get(item.delivery_id).view_model, {"type": "text"})
        self.assertEqual(restored.sent_message_id("k-body"), "om_1")

    def test_outbox_compaction_prunes_sent_and_dead_after_retention(self):
        clock = _Clock()
        outbox = DurableOutbox(
            now=clock,
            sent_retention=20.0,
            dead_retention=50.0,
        )
        sent = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "sent"},
            idempotency_key="sent",
        )
        dead = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "dead"},
            idempotency_key="dead",
        )
        outbox.record_result(sent.delivery_id, "sent")
        outbox.record_result(dead.delivery_id, "permanent_failure")

        clock.now += 19.0
        outbox.compact()
        self.assertEqual(outbox.sent_count(), 1)
        self.assertEqual(outbox.dead_count(), 1)

        clock.now += 2.0
        removed = outbox.compact()
        self.assertEqual(removed["sent"], 1)
        self.assertEqual(outbox.sent_count(), 0)
        self.assertEqual(outbox.dead_count(), 1)

        clock.now += 30.0
        removed = outbox.compact()
        self.assertEqual(removed["dead"], 1)
        self.assertEqual(outbox.dead_count(), 0)

    def test_permanent_delivery_still_dead_letters_immediately(self):
        from walkcode.channel_native import OutboxDispatcher

        outbox = DurableOutbox(now=_Clock())
        channel = FakeChannelAdapter("lark", _channel_caps())

        async def permanent(_binding, _view):
            raise PermanentDeliveryError("bad chat")

        channel.send_view = permanent
        outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "dead"},
            idempotency_key="k1",
        )
        asyncio.run(OutboxDispatcher(outbox, {"lark": channel}).flush_once())

        self.assertEqual(outbox.pending_count(), 0)
        self.assertEqual(outbox.dead_count(), 1)


class InboundLedgerReliabilityTests(unittest.TestCase):
    def test_inbound_event_can_retry_after_exception(self):
        class _FailingTransport:
            kind = "failing"

            def __init__(self):
                self.launch_count = 0

            def capabilities(self):
                return TransportCapabilities(
                    structured_input=True,
                    structured_output=True,
                    permission_callback=False,
                    ask_user_question=False,
                    set_model=False,
                    resume_after_complete=False,
                    external_tui_takeover=False,
                )

            async def launch(self, spec: LaunchSpec):
                self.launch_count += 1
                raise RuntimeError("launch failed")

        transport = _FailingTransport()
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=_Clock()),
            interactions=InteractionStore(now=_Clock()),
            outbox=DurableOutbox(now=_Clock()),
            channels={"lark": FakeChannelAdapter("lark", _channel_caps())},
            transports={"failing": transport},
            inbound_ledger=InboundLedger(now=_Clock()),
            now=_Clock(),
        )
        inbound = InboundEvent(
            event_id="evt-retry",
            channel_kind="lark",
            account_id="bot",
            chat_id="chat",
            thread_id="topic",
            message_id="m1",
            root_message_id="",
            sender_id="owner",
            sender_display="Owner",
            text="run",
        )

        with self.assertRaises(RuntimeError):
            asyncio.run(orchestrator.handle_inbound_event(inbound, agent_transport_kind="failing", cwd="/tmp/project"))
        with self.assertRaises(RuntimeError):
            asyncio.run(orchestrator.handle_inbound_event(inbound, agent_transport_kind="failing", cwd="/tmp/project"))

        self.assertEqual(transport.launch_count, 2)


_DAY = 86400.0
_FIXTURE = Path(__file__).parent / "data" / "state_pre_v0_14_36.json"


def _empty_state(clock, sessions, authz=None) -> StateSnapshot:
    return StateSnapshot(
        sessions=sessions,
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        authz=authz or AuthorizationStore(),
        inbound_ledger=InboundLedger(now=clock),
        hitls=HitlStore(now=clock),
    )


class OldStateCompatibilityTests(unittest.TestCase):
    """v0.14.36 dropped write-only keys; live files written before it still
    carry them and must keep loading (the fixture copies their structure)."""

    RETIRED_SESSION_KEYS = {"writer_lease", "interrupt_reason"}

    def test_pre_v0_14_36_state_file_loads_and_rewrites_without_retired_keys(self):
        head_id = "sess-" + "a" * 32
        tui_id = "tui-claude-" + "b" * 12
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
            store = JsonFileStateStore(path, now=_Clock(1790000100.0))
            loaded = store.load()

            head = loaded.sessions.get(head_id)
            tui = loaded.sessions.get(tui_id)
            self.assertEqual(head.writer_owner.kind, "orchestrator")
            self.assertEqual(tui.stop_reason, "external_tui_process_gone")
            [blocked] = tui.blocked_inputs.values()
            self.assertEqual(blocked.text, "synthetic blocked message")
            self.assertEqual(loaded.sessions.resolve_binding(tui.channel_binding.key()), tui_id)
            owner = ActorRef("lark", "ou_owner")
            self.assertEqual(loaded.authz.role_for(tui_id, owner), SessionRole.OWNER)
            [hitl] = loaded.hitls.to_dict()["requests"].values()
            self.assertEqual(hitl["interaction_id"], "int-" + "f" * 32)
            self.assertEqual(hitl["status"], "decided")
            [interaction] = loaded.interactions.to_dict()["interactions"].values()
            self.assertEqual(interaction["answers"], {"0": "yes"})

            store.save(loaded)
            rewritten = json.loads(path.read_text(encoding="utf-8"))
            reloaded = store.load()

        self.assertNotIn("audit", rewritten["authz"])
        self.assertEqual(len(rewritten["authz"]["grants"]), 2)
        self.assertNotIn("lease_ttl", rewritten["sessions"])
        self.assertNotIn("pending", rewritten["sessions"])
        for session in rewritten["sessions"]["sessions"].values():
            self.assertFalse(self.RETIRED_SESSION_KEYS & set(session))
            self.assertNotIn("subscribed", session["channel_binding"])
            for blocked in session["blocked_inputs"].values():
                self.assertNotIn("expires_at", blocked)
        self.assertNotIn("decisions", rewritten["hitls"])
        for request in rewritten["hitls"]["requests"].values():
            self.assertNotIn("native_params", request)
            self.assertNotIn("channel_binding_key", request)
        for interaction in rewritten["interactions"]["interactions"].values():
            self.assertNotIn("current_index", interaction)
        # The rewrite is lossless for everything still modelled.
        self.assertEqual(reloaded.sessions.to_dict(), loaded.sessions.to_dict())
        self.assertEqual(reloaded.hitls.to_dict(), loaded.hitls.to_dict())
        self.assertEqual(reloaded.authz.to_dict(), loaded.authz.to_dict())

    def test_state_with_retired_telegram_binding_still_loads(self):
        # ADR 0069 retired the Telegram channel. State files from before it may
        # still hold channel_kind="telegram" sessions/grants; they must load
        # and round-trip untouched rather than crash the Lark runtime.
        clock = _Clock()
        sessions = SessionRegistry(now=clock)
        binding = ChannelBinding("telegram", "bot", "123", "77", "110")
        session = sessions.create_structured_session(
            session_id="legacy-telegram",
            binding=binding,
            transport_kind="claude_headless",
            transport_ref={"handle_id": "h1", "agent_session_id": "claude-1"},
            cwd="/tmp/project",
            owner=ActorRef("telegram", "456", "Ada"),
        )
        authz = AuthorizationStore()
        authz.grant(session.session_id, ActorRef("telegram", "456", "Ada"), SessionRole.OWNER)
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json", now=clock)
            store.save(_empty_state(clock, sessions, authz))
            loaded = store.load()

        reloaded = loaded.sessions.get(session.session_id)
        self.assertEqual(reloaded.channel_binding.channel_kind, "telegram")
        self.assertEqual(loaded.sessions.resolve_binding(binding.key()), session.session_id)
        self.assertEqual(loaded.sessions.list_sessions(channel_kind="lark"), [])
        self.assertEqual(
            loaded.authz.role_for(session.session_id, ActorRef("telegram", "456")),
            SessionRole.OWNER,
        )

    def test_grants_no_longer_accumulate_an_audit_log(self):
        authz = AuthorizationStore()
        for _ in range(3):
            authz.grant("s1", _actor("owner"), SessionRole.OWNER)
        self.assertEqual(
            authz.to_dict(),
            {"grants": [{"session_id": "s1", "channel_kind": "lark", "actor_id": "owner", "role": "owner"}]},
        )

    def test_decided_hitl_without_decided_at_is_retained_from_expiry(self):
        clock = _Clock()
        hitls = HitlStore(now=clock, request_ttl=100.0, decided_retention=50.0)
        request = hitls.register_request(
            session_id="s1",
            generation=1,
            transport_kind="claude_headless",
            transport_request_id="rid",
            native_method="can_use_tool",
            prompt_kind="permission",
        )
        request.status = "decided"  # an old file's decided request: decided_at == 0
        clock.now += 149.0
        self.assertEqual(hitls.compact(), {"requests": 0})
        clock.now += 1.0
        self.assertEqual(hitls.compact(), {"requests": 1})


class SessionRetentionTests(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock(1_800_000_000.0)
        self.sessions = SessionRegistry(now=self.clock)
        self.authz = AuthorizationStore()
        self.state = _empty_state(self.clock, self.sessions, self.authz)

    @staticmethod
    def _binding(name: str) -> ChannelBinding:
        return ChannelBinding("lark", "bot", "chat", f"omt_{name}", f"om_{name}")

    def _tui(self, name: str, *, resume: bool = True):
        ref = {"source": "native_tui_hook"}
        if resume:
            ref["resume_ref"] = {"agent_session_id": f"agent-{name}", "transport_kind": "claude_headless"}
        session = self.sessions.create_observed_session(
            session_id=f"tui-{name}",
            binding=self._binding(name),
            cwd="/tmp",
            external_ref=ref,
            owner=_actor("owner"),
        )
        session.status = "stopped"
        session.stop_reason = "external_tui_process_gone"
        self.authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
        return session

    def _headless(self, name: str, *, agent_id: str = "agent-x", stop_reason: str = "runtime_restart"):
        ref = {"handle_id": f"h-{name}"}
        if agent_id:
            ref["agent_session_id"] = agent_id
        session = self.sessions.create_structured_session(
            session_id=f"sess-{name}",
            binding=self._binding(name),
            transport_kind="claude_headless",
            transport_ref=ref,
            cwd="/tmp",
            owner=_actor("owner"),
        )
        if stop_reason:
            session.status = "stopped"
            session.stop_reason = stop_reason
        self.authz.grant(session.session_id, _actor("owner"), SessionRole.OWNER)
        return session

    def _compact(self, days: float) -> set[str]:
        from walkcode.channel_native import compact_sessions

        self.clock.now += days * _DAY
        before = {s.session_id for s in self.sessions.iter_sessions()}
        removed = compact_sessions(self.state)["sessions"]
        after = {s.session_id for s in self.sessions.iter_sessions()}
        self.assertEqual(len(before - after), removed)
        return before - after

    def test_revivable_sessions_get_the_long_window(self):
        # ADR 0054 revival candidate + TUI session resumable via takeover.
        tui = self._tui("tui")
        revivable = self._headless("revive")
        self.assertEqual(self._compact(30), set())
        self.assertEqual(self._compact(59.9), set())
        self.assertEqual(self._compact(0.1), {tui.session_id, revivable.session_id})

    def test_sessions_that_can_never_continue_get_the_short_window(self):
        no_ref_tui = self._tui("noref", resume=False)
        no_ref_headless = self._headless("noref-h", agent_id="")
        archived = self._tui("archived")
        archived.archived_at = self.clock.now
        topicless = self._tui("topicless")
        topicless.channel_binding = None
        kept = self._tui("kept")
        self.assertEqual(self._compact(6.9), set())
        self.assertEqual(
            self._compact(0.1),
            {no_ref_tui.session_id, no_ref_headless.session_id, archived.session_id, topicless.session_id},
        )
        self.assertIn(kept.session_id, {s.session_id for s in self.sessions.iter_sessions()})

    def test_running_sessions_are_never_pruned(self):
        running = self._headless("running", stop_reason="")
        self.assertEqual(self._compact(365), set())
        self.assertEqual(self.sessions.get(running.session_id).status, "running")

    def test_long_idle_running_session_expires_then_revives_and_prunes(self):
        # "running" sessions without a worker were never stopped, so never
        # pruned (work-claude: 81 of them, idle 23-90 days).
        idle = self._headless("idle", stop_reason="")
        idle.lifecycle_state = "IDLE"
        busy = self._headless("busy", stop_reason="")
        busy.lifecycle_state = "ACTIVE"
        live = self._headless("live", stop_reason="")
        live.lifecycle_state = "IDLE"
        self.clock.now += 30 * _DAY

        expired = self.sessions.expire_idle_sessions(is_live=lambda s: s.session_id == live.session_id)

        self.assertEqual(expired, [idle.session_id])
        self.assertEqual(idle.stop_reason, "idle_expired")
        self.assertEqual(live.status, "running")
        self.assertEqual(busy.status, "running")
        # A reply in its topic still revives it (ADR 0054)...
        from walkcode.channel_native import _session_is_channel_revival_candidate

        self.assertTrue(_session_is_channel_revival_candidate(idle))
        # ...and it leaves under the revivable window like any other stop.
        self.assertEqual(self._compact(59.9), set())
        self.assertEqual(self._compact(0.2), {idle.session_id})

    def test_recently_idle_session_is_not_expired(self):
        idle = self._headless("fresh", stop_reason="")
        idle.lifecycle_state = "IDLE"
        self.clock.now += 29 * _DAY
        self.assertEqual(self.sessions.expire_idle_sessions(is_live=lambda s: False), [])

    def test_pruning_drops_bindings_takeovers_grants_and_blocked_inputs(self):
        tui = self._tui("gone")
        blocked = self.sessions.block_input(
            tui.session_id, actor=_actor("owner"), turn=TurnInput(text="secret"), generation=tui.generation,
        )
        self.sessions.request_takeover(
            tui.session_id, blocked.blocked_input_id, requested_by=_actor("owner"), generation=tui.generation,
        )
        other = self._tui("other")
        other.last_progress_at = self.clock.now + 80 * _DAY

        self.assertEqual(self._compact(91), {tui.session_id})

        data = self.sessions.to_dict()
        self.assertEqual(list(data["binding_to_session"].values()), [other.session_id])
        self.assertEqual(data["takeovers"], {})
        self.assertEqual({g["session_id"] for g in self.authz.to_dict()["grants"]}, {other.session_id})
        self.assertNotIn("secret", json.dumps(data))

    def test_a_recent_blocked_input_counts_as_activity(self):
        tui = self._tui("replied")
        self.clock.now += 89 * _DAY
        self.sessions.block_input(
            tui.session_id, actor=_actor("owner"), turn=TurnInput(text="hi"), generation=tui.generation,
        )
        self.assertEqual(self._compact(2), set())

    def test_still_referenced_sessions_are_kept(self):
        outbox_ref = self._tui("outbox", resume=False)
        interaction_ref = self._tui("interaction", resume=False)
        hitl_ref = self._tui("hitl", resume=False)
        self.state.outbox.enqueue(
            channel_binding_key=outbox_ref.channel_binding.key(),
            view_model={"type": "text", "text": "undelivered"},
            idempotency_key="k1",
        )
        self.state.interactions.register_permission(
            session_id=interaction_ref.session_id,
            generation=0,
            tool_name="Bash",
            tool_input={},
            actions=["allow"],
            ttl=30 * _DAY,
        )
        self.state.hitls.register_request(
            session_id=hitl_ref.session_id,
            generation=0,
            transport_kind="claude_headless",
            transport_request_id="rid",
            native_method="can_use_tool",
            prompt_kind="permission",
        )
        self.assertEqual(self._compact(8), set())
        # Once nothing points at them any more they go like the rest.
        self.state.outbox.record_result(
            next(iter(self.state.outbox.to_dict()["pending"])), DeliveryStatus.PERMANENT_FAILURE
        )
        for ctx in self.state.interactions._interactions.values():
            ctx.decision = {"action": "allow"}
        for request in self.state.hitls._requests.values():
            request.status = "stale"
        self.assertEqual(
            self._compact(0), {outbox_ref.session_id, interaction_ref.session_id, hitl_ref.session_id}
        )


class StateTempSweepTests(unittest.TestCase):
    def test_sweep_removes_only_stale_temp_files_of_this_state_file(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonFileStateStore(root / "work-claude-state.json")
            stale = root / ".work-claude-state.json.39n1wzuy.tmp"
            fresh = root / ".work-claude-state.json.fresh123.tmp"
            other = root / ".work-codex-state.json.ck7_k9fu.tmp"
            for path in (stale, fresh, other):
                path.write_text("{partial", encoding="utf-8")
            old = time.time() - 3600
            os.utime(stale, (old, old))
            os.utime(other, (old, old))

            self.assertEqual(store.sweep_stale_temp_files(), 1)

            self.assertFalse(stale.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(other.exists())


class StateSaveFailureTrackingTests(unittest.TestCase):
    def test_last_save_failed_tracks_the_latest_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json")
            state = _empty_state(_Clock(), SessionRegistry(now=_Clock()))
            with patch("walkcode.channel_native._atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.save(state)
            self.assertTrue(store.last_save_failed)
            store.save(state)
            self.assertFalse(store.last_save_failed)
