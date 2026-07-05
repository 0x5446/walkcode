import asyncio
import json
import tempfile
import threading
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
    PermanentDeliveryError,
    SessionRegistry,
    SessionRole,
    TelegramBotApi,
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
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=f"User {actor_id}")


def _binding() -> ChannelBinding:
    return ChannelBinding(
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        root_message_id="root",
    )


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


class PersistenceTests(unittest.TestCase):
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
            binding=ChannelBinding("telegram", "bot", "chat", "topic", "observed-root"),
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
        authz = AuthorizationStore(now=clock)
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
            native_params={"command": "pwd"},
            prompt_kind="permission",
            channel_binding_key=_binding().key(),
        )
        hitls.attach_interaction(hitl.hitl_request_id, ctx.interaction_id)
        hitls.mark_decided(
            hitl.hitl_request_id,
            actor=_actor("owner"),
            action="accept",
            native_response={"decision": "accept"},
            delivery_status=DeliveryStatus.SENT,
        )

        with tempfile.TemporaryDirectory() as tmp:
            store = JsonFileStateStore(Path(tmp) / "state.json", now=clock)
            store.save(
                sessions=sessions,
                interactions=interactions,
                outbox=outbox,
                authz=authz,
                inbound_ledger=ledger,
                hitls=hitls,
            )
            restored = store.load()

        restored_structured = restored.sessions.get(structured.session_id)
        restored_observed = restored.sessions.get(observed.session_id)
        self.assertEqual(restored_structured.writer_lease.lease_id, structured.writer_lease.lease_id)
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
        self.assertEqual(
            restored.hitls.decision_for(hitl.hitl_request_id).native_response,
            {"decision": "accept"},
        )

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
                sessions=sessions,
                interactions=interactions,
                outbox=outbox,
                authz=AuthorizationStore(now=clock),
                inbound_ledger=InboundLedger(now=clock),
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
    def test_transient_delivery_uses_backoff_and_eventually_dead_letters(self):
        from walkcode.channel_native import OutboxDispatcher

        clock = _Clock()
        outbox = DurableOutbox(now=clock, max_attempts=2, base_retry_delay=10.0)
        channel = FakeChannelAdapter("telegram", _channel_caps())
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
        dispatcher = OutboxDispatcher(outbox, {"telegram": channel})

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
        channel = FakeChannelAdapter("telegram", _channel_caps())

        async def rate_limited(_binding, _view):
            raise TransientDeliveryError("rate limited", retry_after=30.0)

        channel.send_view = rate_limited
        item = outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "retry"},
            idempotency_key="k1",
        )

        asyncio.run(OutboxDispatcher(outbox, {"telegram": channel}).flush_once())

        self.assertEqual(outbox.pending_count(), 1)
        self.assertEqual(outbox.get(item.delivery_id).next_attempt_at, clock.now + 30.0)

    def test_concurrent_dispatchers_send_one_claimed_delivery_once(self):
        from walkcode.channel_native import OutboxDispatcher

        clock = _Clock()
        outbox = DurableOutbox(now=clock)
        channel = FakeChannelAdapter("telegram", _channel_caps())
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
        first = OutboxDispatcher(outbox, {"telegram": channel}, owner="first")
        second = OutboxDispatcher(outbox, {"telegram": channel}, owner="second")

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
        channel = FakeChannelAdapter("telegram", _channel_caps())

        async def permanent(_binding, _view):
            raise PermanentDeliveryError("bad chat")

        channel.send_view = permanent
        outbox.enqueue(
            channel_binding_key=_binding().key(),
            view_model={"type": "text", "text": "dead"},
            idempotency_key="k1",
        )
        asyncio.run(OutboxDispatcher(outbox, {"telegram": channel}).flush_once())

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
                    interrupt=False,
                    set_model=False,
                    set_permission_mode=False,
                    checkpoint_rewind=False,
                    resume_after_complete=False,
                    resume_active_turn=False,
                    multi_client_observe=False,
                    multi_client_write=False,
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
            channels={"telegram": FakeChannelAdapter("telegram", _channel_caps())},
            transports={"failing": transport},
            inbound_ledger=InboundLedger(now=_Clock()),
            now=_Clock(),
        )
        inbound = InboundEvent(
            event_id="evt-retry",
            channel_kind="telegram",
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


class TelegramHttpTests(unittest.TestCase):
    def test_real_http_branch_runs_off_event_loop(self):
        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({"ok": True, "result": {"message_id": 1}}).encode()

        thread_ids = []

        def fake_urlopen(_request, timeout):
            thread_ids.append(threading.get_ident())
            time.sleep(0.05)
            return _Response()

        async def run_call():
            api = TelegramBotApi("token")
            main_thread = threading.get_ident()
            started = time.perf_counter()
            task = asyncio.create_task(api.call("sendMessage", {"chat_id": "c", "text": "hi"}))
            await asyncio.sleep(0.01)
            elapsed = time.perf_counter() - started
            result = await task
            return main_thread, elapsed, result

        with patch("urllib.request.urlopen", fake_urlopen):
            main_thread, elapsed, result = asyncio.run(run_call())

        self.assertLess(elapsed, 0.04)
        self.assertNotEqual(thread_ids[0], main_thread)
        self.assertEqual(result["result"]["message_id"], 1)
