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

    def test_two_silent_turns_in_a_row_warn_twice(self):
        # The turn_completed dedupe (same text as the last bubble) must not
        # swallow the second warning — that is the outage all over again.
        orchestrator, transport, channel, session = _orchestrator([_completed("")])

        _submit(orchestrator, session)
        transport._scripted_events = [_completed("")]
        _submit(orchestrator, session, text="再试一次")

        self.assertEqual(_notice_count(channel), 2)


if __name__ == "__main__":
    unittest.main()
