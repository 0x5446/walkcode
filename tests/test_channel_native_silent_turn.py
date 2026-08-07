"""A turn that produces nothing must say so instead of closing in silence.

2026-08-07: the commandcode relay's Chat Completions upstream answered
`400 user message must have content` for every request in one codex thread
(a blank user message had been persisted into its history). codex retried six
times per turn, then closed the turn with `last_agent_message: null`. The
drain rendered that completion to "" and `if not visible_text: continue`
dropped it — the Feishu thread showed nothing at all, for hours, with no error
anywhere the user could see.
"""

import asyncio
import unittest

from walkcode.channel_native import (
    EMPTY_TURN_NOTICE,
    ActorRef,
    AgentEvent,
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    CodexAppServerTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
    TurnInput,
    render_view_text,
)

from test_channel_native_codex import _FakeCodexClient


def _actor() -> ActorRef:
    return ActorRef(channel_kind="telegram", actor_id="owner", display_name="Owner")


def _binding() -> ChannelBinding:
    return ChannelBinding("telegram", "bot", "chat", "topic", "root")


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
        external_tui_takeover=False,
    )


def _orchestrator(scripted_events=None):
    transport = FakeAgentTransport(
        "fake-transport",
        _transport_caps(),
        scripted_events=list(scripted_events or []),
    )
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(),
        interactions=InteractionStore(),
        outbox=DurableOutbox(),
        channels={"telegram": channel},
        transports={"fake-transport": transport},
        authz=AuthorizationStore(),
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "fake-transport", "/tmp/project", _actor())
    )
    channel.sent_views.clear()
    return orchestrator, transport, channel, session


def _submit(orchestrator, session, text="run"):
    asyncio.run(
        orchestrator.submit_user_input(
            session.session_id,
            TurnInput(text=text),
            actor=_actor(),
            generation=session.generation,
        )
    )


def _completed(message: str = "") -> AgentEvent:
    return AgentEvent(AgentEventType.TURN_COMPLETED, {"message": message})


def _sent_texts(channel) -> list[str]:
    return [render_view_text(entry["view"]) for entry in channel.sent_views]


def _notice_count(channel) -> int:
    return sum(1 for text in _sent_texts(channel) if EMPTY_TURN_NOTICE in text)


class SilentTurnNoticeTests(unittest.TestCase):
    def test_turn_completing_with_no_output_at_all_warns(self):
        orchestrator, _transport, channel, session = _orchestrator([_completed("")])

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 1)

    def test_completion_after_a_delta_stays_silent(self):
        orchestrator, _transport, channel, session = _orchestrator(
            [AgentEvent(AgentEventType.TURN_DELTA, {"text": "答案"}), _completed("")]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 0)
        self.assertTrue(any("答案" in text for text in _sent_texts(channel)))

    def test_completion_after_tool_progress_stays_silent(self):
        # The tool card already told the user something happened.
        orchestrator, _transport, channel, session = _orchestrator(
            [
                AgentEvent(
                    AgentEventType.TOOL_STARTED,
                    {"tool_name": "Bash", "tool_id": "t1", "summary": "ls"},
                ),
                AgentEvent(
                    AgentEventType.TOOL_COMPLETED,
                    {"tool_name": "Bash", "tool_id": "t1", "summary": "ok"},
                ),
                _completed(""),
            ]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 0)

    def test_two_silent_turns_in_one_drain_warn_twice(self):
        # Both completions arrive inside ONE drain, which is how the resident
        # codex listener actually delivers them (ADR 0060). Re-entering the
        # drain per submit would reset the dedupe watermark and pass even if
        # the per-turn scoping regressed.
        orchestrator, _transport, channel, session = _orchestrator(
            [_completed(""), _completed("")]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 2)

    def test_whitespace_only_output_still_counts_as_silence(self):
        # A "\n\n" delta is not something a human can read: it must not
        # suppress the notice, and it must not post a blank bubble either.
        orchestrator, _transport, channel, session = _orchestrator(
            [AgentEvent(AgentEventType.TURN_DELTA, {"text": "   \n"}), _completed("")]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 1)
        self.assertEqual(len(_sent_texts(channel)), 1)

    def test_two_turns_answering_the_same_thing_both_reach_the_channel(self):
        # The dedupe watermark used to live for the whole drain, so on this
        # cross-turn stream the second turn's completion looked like a repeat
        # of the first and was dropped — silence of exactly the kind this
        # module exists to prevent.
        orchestrator, _transport, channel, session = _orchestrator(
            [_completed("同一个答复"), _completed("同一个答复")]
        )

        _submit(orchestrator, session)

        self.assertEqual(_sent_texts(channel), ["同一个答复", "同一个答复"])

    def test_completion_repeating_its_own_delta_is_still_deduped(self):
        # Within one turn the completion echoes what the deltas already
        # streamed; that duplicate must stay suppressed.
        orchestrator, _transport, channel, session = _orchestrator(
            [AgentEvent(AgentEventType.TURN_DELTA, {"text": "答案"}), _completed("答案")]
        )

        _submit(orchestrator, session)

        self.assertEqual(_sent_texts(channel), ["答案"])


class SilentTurnOverRealCodexEventsTests(unittest.TestCase):
    """Same behaviour, driven through CodexAppServerTransport's own conversion.

    docs/review/.review-learnings.md: a FakeAgentTransport test cannot prove
    the real protocol path works — codex's `turn/completed` carries no message
    and the answer only ever arrives as `item/agentMessage/delta`.
    """

    def _orchestrator_with_codex(self, raw_events):
        client = _FakeCodexClient()
        client.event_batches["thread-1"] = list(raw_events)
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
            orchestrator.start_session(
                _binding(), "codex_app_server", "/tmp/project", _actor()
            )
        )
        channel.sent_views.clear()
        return orchestrator, channel, session

    def test_codex_task_complete_without_message_warns(self):
        # The outage shape verbatim: six upstream 400s, then task_complete
        # with last_agent_message = null and no delta anywhere.
        orchestrator, channel, session = self._orchestrator_with_codex(
            [
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": None,
                    },
                }
            ]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 1)

    def test_codex_jsonrpc_turn_completed_without_delta_warns(self):
        # The JSON-RPC wire shape: `turn/completed` never carries the answer,
        # so an empty turn looks identical to a normal one at this layer.
        orchestrator, channel, session = self._orchestrator_with_codex(
            [
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
                }
            ]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 1)

    def test_codex_delta_then_bare_completion_stays_silent(self):
        # The normal shape: the answer rides `item/agentMessage/delta` and the
        # completion is bare. This must NOT warn.
        orchestrator, channel, session = self._orchestrator_with_codex(
            [
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "item-1",
                        "delta": "真实答复",
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {"threadId": "thread-1", "turn": {"id": "turn-1"}},
                },
            ]
        )

        _submit(orchestrator, session)

        self.assertEqual(_notice_count(channel), 0)
        self.assertTrue(any("真实答复" in text for text in _sent_texts(channel)))


if __name__ == "__main__":
    unittest.main()
