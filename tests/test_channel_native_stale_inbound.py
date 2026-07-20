"""ADR 0057: a stranded (offline-queued) message must not chase a session
that has since been advanced by newer input.

Feishu re-pushes un-acked events after reconnect; the ledger only dedups.
The guard compares the message's CHANNEL creation time against the session's
last-user-input watermark, with two anti-collision safeties (5s tolerance,
≥30s minimum age) — same-clock comparisons (channel vs channel) are exact,
the single cross-clock comparison (channel vs terminal) is bounded.
"""

import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InboundEvent,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
)
from walkcode.channel_native import _session_from_dict, _session_to_dict


class _Clock:
    def __init__(self, now: float = 100_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="owner", display_name="Owner")


def _caps() -> ChannelCapabilities:
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


def _tcaps() -> TransportCapabilities:
    return TransportCapabilities(
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
    )


def _setup():
    clock = _Clock()
    transport = FakeAgentTransport("claude_headless", _tcaps())
    channel = FakeChannelAdapter("telegram", _caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports={"claude_headless": transport},
        authz=AuthorizationStore(now=clock),
        now=clock,
    )
    binding = ChannelBinding("telegram", "bot", "chat", "topic", "root")
    session = asyncio.run(
        orchestrator.start_session(binding, "claude_headless", "/tmp/project", _actor())
    )
    return orchestrator, transport, channel, session, clock


def _inbound(text: str, created_at: float, message_id: str = "m-1") -> InboundEvent:
    return InboundEvent(
        event_id=f"evt-{message_id}",
        channel_kind="telegram",
        account_id="bot",
        chat_id="chat",
        thread_id="topic",
        message_id=message_id,
        root_message_id="root",
        sender_id="owner",
        sender_display="Owner",
        text=text,
        created_at=created_at,
    )


class StaleInboundGuardTests(unittest.TestCase):
    def _handle(self, orchestrator, inbound):
        return asyncio.run(
            orchestrator.handle_inbound_event(
                inbound, agent_transport_kind="claude_headless", cwd="/tmp/project"
            )
        )

    def test_stranded_message_behind_watermark_is_refused(self):
        orchestrator, transport, _channel, session, clock = _setup()
        session.last_user_input_at = clock.now - 60.0  # someone spoke a minute ago
        stranded = _inbound("旧消息", created_at=clock.now - 600.0)  # 10min old

        result = self._handle(orchestrator, stranded)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "stale_inbound")
        self.assertNotIn("submit_turn", transport.call_log)

    def test_rapid_double_send_is_never_misjudged(self):
        # Watermark records the CHANNEL creation time, not submit time: the
        # second of two quick messages stays ahead of the watermark even
        # when processed much later.
        orchestrator, transport, _channel, session, clock = _setup()
        first = _inbound("第一条", created_at=clock.now - 601.0, message_id="m-1")
        second = _inbound("第二条", created_at=clock.now - 600.0, message_id="m-2")

        r1 = self._handle(orchestrator, first)
        self.assertTrue(r1.accepted)
        self.assertEqual(session.last_user_input_at, clock.now - 601.0)

        r2 = self._handle(orchestrator, second)
        self.assertTrue(r2.accepted)
        self.assertEqual([t.text for t in transport.submitted_turns], ["第一条", "第二条"])

    def test_fresh_message_is_always_allowed(self):
        # Under the 30s minimum age nothing is ever blocked, even behind the
        # watermark (benign near-concurrency, clock skew).
        orchestrator, transport, _channel, session, clock = _setup()
        session.last_user_input_at = clock.now
        recent = _inbound("刚发的", created_at=clock.now - 10.0)

        result = self._handle(orchestrator, recent)
        self.assertTrue(result.accepted)

    def test_within_tolerance_is_allowed(self):
        orchestrator, transport, _channel, session, clock = _setup()
        session.last_user_input_at = clock.now - 100.0
        near = _inbound("边界", created_at=clock.now - 103.0)  # only 3s behind

        result = self._handle(orchestrator, near)
        self.assertTrue(result.accepted)

    def test_message_without_timestamp_is_allowed(self):
        orchestrator, transport, _channel, session, clock = _setup()
        session.last_user_input_at = clock.now - 60.0
        untimed = _inbound("无时间戳", created_at=0.0)

        result = self._handle(orchestrator, untimed)
        self.assertTrue(result.accepted)

    def test_submit_stamps_watermark_with_channel_time(self):
        orchestrator, transport, _channel, session, clock = _setup()
        turn = TurnInput(text="hi", created_at=clock.now - 42.0)
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id, turn, actor=_actor(), generation=session.generation
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(session.last_user_input_at, clock.now - 42.0)

        # Untimed input falls back to local now and never regresses.
        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="again"),
                actor=_actor(),
                generation=session.generation,
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(session.last_user_input_at, clock.now)

    def test_watermark_survives_persistence_round_trip(self):
        _orchestrator, _transport, _channel, session, clock = _setup()
        session.last_user_input_at = 12345.5
        restored = _session_from_dict(_session_to_dict(session))
        self.assertEqual(restored.last_user_input_at, 12345.5)


if __name__ == "__main__":
    unittest.main()
