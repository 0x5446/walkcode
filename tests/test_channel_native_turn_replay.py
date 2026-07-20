"""ADR 0058: an accepted submit that dies with the worker is replayed with backoff.

Live incident 2026-07-20 15:47: the revived worker hit the model-API outage
window and exited before answering; the only signal was a "请重发" line
routed through the event stream — which the generation fence then dropped.
The user stared at a silent Feishu thread for half an hour.

Contract under test:
- a pending_turn_lost SESSION_ERROR schedules an automatic replay of the
  last accepted submit, and says so (no silent death, no bare "请重发");
- replays are bounded by turn_replay_delays; exhaustion is announced;
- a replay yields to newer human input (watermark), to an external-TUI
  claim, and to an in-flight turn — silently;
- a replay-submit failure notifies the channel directly (not through the
  fence-droppable event stream).
"""

import asyncio
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEvent,
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
)
from walkcode.channel_native_runtime import _export_driver_label


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _actor(actor_id: str = "owner") -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id=actor_id, display_name=actor_id.title())


def _binding(root: str = "root") -> ChannelBinding:
    return ChannelBinding("telegram", "bot", "chat", "topic", root)


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


def _pending_turn_lost_event(*, traffic_seen: bool = False, pending_lost: int = 1) -> AgentEvent:
    return AgentEvent(
        AgentEventType.SESSION_ERROR,
        {
            "message": "代理进程在生成回复前退出了，你刚发送的消息没有被处理；请重发一次。",
            "reason": "pending_turn_lost",
            "traffic_seen": traffic_seen,
            "pending_lost": pending_lost,
        },
    )


def _orchestrator(transport=None):
    clock = _Clock()
    transport = transport or FakeAgentTransport("claude_headless", _transport_caps())
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(now=clock),
        interactions=InteractionStore(now=clock),
        outbox=DurableOutbox(now=clock),
        channels={"telegram": channel},
        transports={"claude_headless": transport},
        authz=AuthorizationStore(now=clock),
        now=clock,
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor("owner"))
    )
    # A real incident session carries a durable resume identity from earlier
    # turns; without it a replay is rejected with missing_resume_ref.
    session.transport_ref["agent_session_id"] = "agent-sess-1"
    return orchestrator, transport, channel, session, clock


async def _drain_replay_tasks(orchestrator, rounds: int = 20):
    for _ in range(rounds):
        await asyncio.sleep(0)
        if not orchestrator._turn_replay_tasks:
            break
    # One extra tick lets done-callbacks and follow-up drains settle.
    await asyncio.sleep(0)


def _error_texts(channel: FakeChannelAdapter) -> list[str]:
    return [
        str(entry["view"].get("message", ""))
        for entry in channel.sent_views
        if entry["view"].get("type") == "error"
    ]


class TurnReplayTests(unittest.TestCase):
    def test_pending_turn_lost_schedules_replay_and_resubmits(self):
        orchestrator, transport, channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            result = await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            self.assertTrue(result.accepted)
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        texts = [turn.text for turn in transport.submitted_turns]
        self.assertEqual(texts, ["ship it", "ship it"])
        # The user was told a replay is coming — not just "请重发".
        self.assertTrue(
            any("自动重发" in text for text in _error_texts(channel)),
            f"no replay announcement in {channel.sent_views!r}",
        )

    def test_replay_exhaustion_is_announced_and_stops(self):
        orchestrator, transport, channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            # The replay resubmits; make its drain lose the turn again.
            transport._scripted_events = [_pending_turn_lost_event()]
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        texts = [turn.text for turn in transport.submitted_turns]
        self.assertEqual(texts, ["ship it", "ship it"], "replay must stop after exhaustion")
        self.assertTrue(
            any("仍未成功" in text for text in _error_texts(channel)),
            f"no exhaustion notice in {channel.sent_views!r}",
        )
        self.assertNotIn(session.session_id, orchestrator._turn_replays)

    def test_replay_yields_to_newer_user_input(self):
        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            # Newer human input advanced the watermark before the replay fired.
            session.last_user_input_at = 995.0
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertEqual([turn.text for turn in transport.submitted_turns], ["ship it"])

    def test_replay_yields_to_external_tui_claim(self):
        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertEqual([turn.text for turn in transport.submitted_turns], ["ship it"])

    def test_empty_watermark_submit_is_not_replayable(self):
        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="   ", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        # The empty watermark submit died with the worker; nothing to replay.
        self.assertEqual(len(transport.submitted_turns), 1)
        self.assertNotIn(session.session_id, orchestrator._turn_replays)

    def test_replay_submit_exception_notifies_channel_directly(self):
        orchestrator, transport, channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        original_submit = transport.submit_turn
        calls = {"count": 0}

        async def flaky_submit(handle, turn, idempotency_key):
            calls["count"] += 1
            if calls["count"] >= 2:
                raise RuntimeError("api overloaded")
            await original_submit(handle, turn, idempotency_key=idempotency_key)

        transport.submit_turn = flaky_submit

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertTrue(
            any("自动重发" in text and "失败" in text for text in _error_texts(channel)),
            f"no direct failure notice in {channel.sent_views!r}",
        )


class TurnReplaySafetyTests(unittest.TestCase):
    """Review R1 adoptions: partial execution, supersession, stash hygiene."""

    def test_traffic_seen_refuses_replay_and_says_so(self):
        # A turn that already streamed output may have executed side effects;
        # replaying it re-executes them. Refuse and tell the truth.
        orchestrator, transport, channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event(traffic_seen=True)]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="deploy prod", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertEqual([turn.text for turn in transport.submitted_turns], ["deploy prod"])
        self.assertTrue(
            any("不自动重发" in text for text in _error_texts(channel)),
            f"no partial-execution notice in {channel.sent_views!r}",
        )
        self.assertNotIn(session.session_id, orchestrator._turn_replays)

    def test_superseded_entry_is_not_replayed(self):
        # replay_id identity pin: a newer accepted submit overwrites the
        # stash; the old sleeping replay task must yield even when the new
        # input landed inside the 0.5s watermark tolerance (R1 old-new-old).
        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.05,)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="old", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            # New human input 0.2s later — inside the watermark tolerance.
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="new", created_at=990.2),
                actor=_actor("owner"),
                generation=session.generation,
            )
            await asyncio.sleep(0.15)
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertEqual(
            [turn.text for turn in transport.submitted_turns],
            ["old", "new"],
            "superseded replay must not resubmit the old message",
        )

    def test_empty_submit_clears_previous_stash(self):
        # An empty watermark submit is newer human input: a stale entry left
        # behind would replay an already-answered message if the empty
        # submit's worker dies (R1 tests#4).
        orchestrator, transport, _channel, session, _clock = _orchestrator()

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            self.assertIn(session.session_id, orchestrator._turn_replays)
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="   ", created_at=991.0),
                actor=_actor("owner"),
                generation=session.generation,
            )

        asyncio.run(scenario())

        self.assertNotIn(session.session_id, orchestrator._turn_replays)

    def test_missing_resume_ref_rejection_notifies_channel(self):
        # A failure-class rejection has no successor speaking for us — the
        # user holds a "will auto-resend" promise and must hear the truth.
        orchestrator, transport, channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        # No durable resume identity: the replay's fresh resume is rejected.
        session.transport_ref.pop("agent_session_id", None)
        transport._scripted_events = [_pending_turn_lost_event()]

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            # Simulate the worker being gone so the replay must resume fresh.
            session.lifecycle_state = "ERROR_RECOVERABLE"
            session.writer_lease = None
            await _drain_replay_tasks(orchestrator)

        asyncio.run(scenario())

        self.assertTrue(
            any("自动重发失败" in text for text in _error_texts(channel)),
            f"no failure notice in {channel.sent_views!r}",
        )


class ReplayGuardTests(unittest.TestCase):
    def test_stale_replay_guard_is_rejected_before_submit(self):
        # The last-instant identity re-check inside submit_user_input: a
        # replay whose stash entry was overwritten during its writer-ready
        # awaits must abort with replay_superseded, never reach the transport
        # (review R2 Critical: new-then-old submit order).
        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator._turn_replays[session.session_id] = {
            "replay_id": "current-id",
            "turn": TurnInput(text="new"),
            "actor": _actor("owner"),
            "attempt": 0,
            "watermark": 990.0,
        }

        result = asyncio.run(
            orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="old", created_at=980.0),
                actor=_actor("owner"),
                generation=session.generation,
                replay_guard="stale-id",
            )
        )

        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "replay_superseded")
        self.assertEqual(transport.submitted_turns, [])

    def test_failed_submit_rolls_back_prestaged_entry(self):
        # Stash is written BEFORE the transport submit (so an EOF during the
        # submit window sees this turn, not the previous one); a submit that
        # raises must roll its own entry back.
        orchestrator, transport, _channel, session, _clock = _orchestrator()

        async def boom(handle, turn, idempotency_key):
            raise RuntimeError("submit boom")

        transport.submit_turn = boom

        async def scenario():
            with self.assertRaises(RuntimeError):
                await orchestrator.submit_user_input(
                    session.session_id,
                    TurnInput(text="ship it", created_at=990.0),
                    actor=_actor("owner"),
                    generation=session.generation,
                )

        asyncio.run(scenario())
        self.assertNotIn(session.session_id, orchestrator._turn_replays)

    def test_degrade_markers_on_yield_paths(self):
        # R1 promised every silent yield leaves a grep-able trace; pin the
        # marker names so a rename can't silently unwire them (R2 tests#6).
        import contextlib as _ctx
        import io

        orchestrator, transport, _channel, session, _clock = _orchestrator()
        orchestrator.turn_replay_delays = (0.0,)
        transport._scripted_events = [_pending_turn_lost_event()]

        stderr = io.StringIO()

        async def scenario():
            await orchestrator.submit_user_input(
                session.session_id,
                TurnInput(text="ship it", created_at=990.0),
                actor=_actor("owner"),
                generation=session.generation,
            )
            session.lifecycle_state = "EXTERNAL_OBSERVED_READONLY"
            await _drain_replay_tasks(orchestrator)

        with _ctx.redirect_stderr(stderr):
            asyncio.run(scenario())

        self.assertIn("turn_replay_skipped", stderr.getvalue())
        self.assertIn("reason=lifecycle", stderr.getvalue())


class DriverLabelExportTests(unittest.TestCase):
    def test_config_is_the_label_source_of_truth(self):
        # The env-file stem is a documentation convention; the real label
        # comes from _launchd_service_label(config) — a renamed env file must
        # not export a wrong marker (review R2 tests#3).
        from types import SimpleNamespace

        config = SimpleNamespace(
            channel=SimpleNamespace(kind="lark"),
            agent="claude",
            profile="personal",
        )
        env = {"WALKCODE_ENV_FILE": "/tmp/renamed-anything.env"}
        from walkcode.channel_native_runtime import _export_driver_label as export

        self.assertEqual(export(config, environ=env), "com.walkcode.personal-claude")
        self.assertEqual(env["WALKCODE_DRIVER_LABEL"], "com.walkcode.personal-claude")

    def test_label_derived_from_env_file(self):
        env = {"WALKCODE_ENV_FILE": "/Users/x/.walkcode/personal-claude.env"}
        self.assertEqual(_export_driver_label(environ=env), "com.walkcode.personal-claude")
        self.assertEqual(env["WALKCODE_DRIVER_LABEL"], "com.walkcode.personal-claude")

    def test_existing_label_wins(self):
        env = {
            "WALKCODE_ENV_FILE": "/Users/x/.walkcode/personal-claude.env",
            "WALKCODE_DRIVER_LABEL": "com.walkcode.custom",
        }
        self.assertEqual(_export_driver_label(environ=env), "com.walkcode.custom")
        self.assertEqual(env["WALKCODE_DRIVER_LABEL"], "com.walkcode.custom")

    def test_no_env_file_no_label(self):
        env = {}
        self.assertEqual(_export_driver_label(environ=env), "")
        self.assertNotIn("WALKCODE_DRIVER_LABEL", env)


if __name__ == "__main__":
    unittest.main()
