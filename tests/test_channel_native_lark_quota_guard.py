"""Stop burning Lark API quota on calls that can never succeed.

2026-08-04: the tenant hit 9299/10000 monthly calls. The runtime logs held
~5100 failed calls, and the top two reasons were both retried forever:

    2064x  230002   "Bot/User can NOT be out of the chat"
     856x  99991403 "This month's API call quota has been exceeded"

Neither can succeed on retry — the second one is actively self-defeating,
since the retries spend the very budget the error is reporting gone. Both are
now permanent, and a binding that fails permanently is latched off so it costs
one failure instead of one per agent event.
"""

import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    PermanentDeliveryError,
    SessionRegistry,
    TransientDeliveryError,
    TransportCapabilities,
)
from walkcode.channel_native.lark_live import (
    PERMANENT_LARK_CODES,
    is_permanent_lark_code,
)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


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


def _transport_caps() -> TransportCapabilities:
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


def _binding() -> ChannelBinding:
    binding = ChannelBinding(
        channel_kind="fake",
        account_id="bot",
        chat_id="chat-1",
        thread_id="topic-1",
        root_message_id="root-1",
    )
    binding.capabilities["status_card"] = True
    return binding


def _actor() -> ActorRef:
    return ActorRef(channel_kind="fake", actor_id="user-1", display_name="User One")


class _DeadChannel(FakeChannelAdapter):
    """Every write fails the way an evicted bot / exhausted quota does."""

    def __init__(self, kind, capabilities, error=None):
        super().__init__(kind, capabilities)
        self.attempts = 0
        self.error = error or PermanentDeliveryError(
            "Lark reply failed: 230002 Bot/User can NOT be out of the chat."
        )

    async def send_view(self, binding, view_model):
        self.attempts += 1
        raise self.error

    async def edit_view(self, binding, message_id, view_model):
        self.attempts += 1
        raise self.error


class PermanentCodeClassificationTests(unittest.TestCase):
    def test_bot_out_of_chat_is_permanent(self):
        # 2064 failures in the measured window; the bot cannot re-add itself.
        self.assertTrue(is_permanent_lark_code(230002))

    def test_quota_exhausted_is_permanent(self):
        # 856 failures; retrying the "out of quota" error spends quota.
        self.assertTrue(is_permanent_lark_code(99991403))

    def test_content_rejection_stays_permanent(self):
        self.assertTrue(is_permanent_lark_code(230001))

    def test_unknown_and_server_side_codes_stay_retryable(self):
        # When unsure, prefer retry over dropping agent output.
        for code in (0, 500, 1061045, 230098):
            self.assertFalse(is_permanent_lark_code(code), code)
        self.assertNotIn(500, PERMANENT_LARK_CODES)


class StatusCardNoiseFoldingTests(unittest.TestCase):
    """The fingerprint must fold beats, not bill one patch per tool event."""

    def test_event_stream_progress_values_are_folded(self):
        # These are what _record_session_progress actually writes
        # (AgentEventType values). They used to match nothing, so every tool
        # event changed the fingerprint and cost a patch.
        regex = Orchestrator._STATUS_NOISE_PROGRESS
        for value in (
            "tool.started",
            "tool.completed",
            "tool.failed",
            "turn.delta",
            "turn.narration",
            "background.tasks",
        ):
            self.assertTrue(regex.match(value), value)

    def test_tui_hook_progress_values_stay_folded(self):
        regex = Orchestrator._STATUS_NOISE_PROGRESS
        for value in (
            "external_tui.pre-tool",
            "external_tui.post-tool",
            "external_tui.message-display",
            "external_tui.user-prompt-submit",
        ):
            self.assertTrue(regex.match(value), value)

    def test_daemon_tempo_detail_does_not_leak_into_the_fingerprint(self):
        regex = Orchestrator._STATUS_NOISE_PROGRESS
        self.assertTrue(regex.match("external_tui.daemon_working"))
        # Free text after the colon must fold too, or every distinct tool name
        # becomes a new fingerprint.
        self.assertTrue(regex.match("external_tui.daemon_working:Bash"))

    def test_real_state_changes_are_never_folded(self):
        # Folding these would make the card stop reporting what it exists for.
        regex = Orchestrator._STATUS_NOISE_PROGRESS
        for value in (
            "turn.completed",
            "permission.requested",
            "ask_user.requested",
            "session.error",
            "external_tui.stop",
        ):
            self.assertFalse(regex.match(value), value)

    def test_a_tool_burst_costs_one_patch_not_one_per_event(self):
        clock = _Clock()
        channel = FakeChannelAdapter("fake", _channel_caps())
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={
                "codex_app_server": FakeAgentTransport(
                    "codex_app_server", _transport_caps(), scripted_events=[]
                )
            },
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        asyncio.run(orchestrator.refresh_session_status_card(session))
        baseline = len(channel.sent_views)

        # 20 tool events, the shape of a real burst. Before the folding fix
        # every one of them changed the fingerprint and billed a patch; now the
        # only write is the single idle→active transition at the front.
        for _ in range(10):
            for value in ("tool.started", "tool.completed"):
                session.last_progress_event = value
                session.last_event_seq += 1
                asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(len(channel.sent_views) - baseline, 1)

        # …and a genuine transition still gets through.
        session.last_progress_event = "turn.completed"
        asyncio.run(orchestrator.refresh_session_status_card(session))
        self.assertEqual(len(channel.sent_views) - baseline, 2)


class DeliveryLatchTests(unittest.TestCase):
    def _fixture(self, channel):
        clock = _Clock()
        orchestrator = Orchestrator(
            sessions=SessionRegistry(now=clock),
            interactions=InteractionStore(now=clock),
            outbox=DurableOutbox(now=clock),
            channels={"fake": channel},
            transports={
                "codex_app_server": FakeAgentTransport(
                    "codex_app_server", _transport_caps(), scripted_events=[]
                )
            },
            now=clock,
        )
        session = asyncio.run(
            orchestrator.start_session(
                binding=_binding(),
                transport_kind="codex_app_server",
                cwd="/tmp/project",
                owner=_actor(),
            )
        )
        return orchestrator, session

    def test_permanent_failure_latches_the_binding_off(self):
        channel = _DeadChannel("fake", _channel_caps())
        orchestrator, session = self._fixture(channel)

        for _ in range(20):
            session.last_event_seq += 1
            session.last_progress_event = f"event-{session.last_event_seq}"
            asyncio.run(orchestrator.refresh_session_status_card(session))

        # One failure total, not one per event: this is the 5100-failure
        # pattern the latch exists to kill.
        self.assertEqual(channel.attempts, 1)
        self.assertIn("230002", orchestrator._delivery_is_dead(session.channel_binding))

    def test_quota_error_latches_too(self):
        channel = _DeadChannel(
            "fake",
            _channel_caps(),
            error=PermanentDeliveryError(
                "Lark reply failed: 99991403 This month's API call quota has been exceeded"
            ),
        )
        orchestrator, session = self._fixture(channel)

        for _ in range(10):
            session.last_event_seq += 1
            session.last_progress_event = f"event-{session.last_event_seq}"
            asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.attempts, 1)

    def test_transient_failure_does_not_latch(self):
        # A network blip must stay retryable; latching on it would mute a
        # perfectly healthy session.
        channel = _DeadChannel(
            "fake", _channel_caps(), error=TransientDeliveryError("connection reset")
        )
        orchestrator, session = self._fixture(channel)

        for _ in range(3):
            session.last_event_seq += 1
            session.last_progress_event = f"event-{session.last_event_seq}"
            asyncio.run(orchestrator.refresh_session_status_card(session))

        self.assertEqual(channel.attempts, 3)
        self.assertEqual(orchestrator._delivery_is_dead(session.channel_binding), "")

    def test_tool_progress_respects_the_latch(self):
        channel = _DeadChannel("fake", _channel_caps())
        orchestrator, session = self._fixture(channel)
        view = {"type": "tool_progress", "tool": "Bash", "status": "started", "tool_id": "t1"}

        for _ in range(15):
            asyncio.run(orchestrator._upsert_tool_progress_view(session, channel, view))

        self.assertEqual(channel.attempts, 1)

    def test_revive_clears_the_latch(self):
        channel = _DeadChannel("fake", _channel_caps())
        orchestrator, session = self._fixture(channel)
        asyncio.run(orchestrator.refresh_session_status_card(session))
        self.assertTrue(orchestrator._delivery_is_dead(session.channel_binding))

        # Inbound traffic proves the channel can reach us again.
        Orchestrator.revive_delivery(session.channel_binding)

        self.assertEqual(orchestrator._delivery_is_dead(session.channel_binding), "")
        session.last_progress_event = "after-revive"
        asyncio.run(orchestrator.refresh_session_status_card(session))
        self.assertEqual(channel.attempts, 2)


if __name__ == "__main__":
    unittest.main()
