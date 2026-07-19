"""ADR 0055: mid-turn assistant narration mirrors onto the tool-progress card.

Narration is the text the agent emits right before tool calls ("what I'm
about to do"). Both mirror paths used to drop it entirely:
- headless: _convert_sdk_message discarded text sharing a message with
  tool_use blocks;
- TUI hooks: no hook payload ever carries it (only Stop's final text).

It now flows as TURN_NARRATION events / transcript-cursor drains into 💬
lines on the rolling tool-progress card — never a channel bubble, never
sealing the burst.
"""

import asyncio
import json
import os
import tempfile
import unittest

from walkcode.channel_native import (
    ActorRef,
    AgentEventType,
    AuthorizationStore,
    ChannelBinding,
    ChannelCapabilities,
    ClaudeHeadlessTransport,
    DurableOutbox,
    FakeAgentTransport,
    FakeChannelAdapter,
    InteractionStore,
    Orchestrator,
    SessionRegistry,
    TransportCapabilities,
    render_view_text,
)
from walkcode.channel_native.lark_cards import _tool_progress_card
from walkcode.channel_native_runtime import _read_transcript_narration


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
        external_tui_takeover=True,
    )


def _orchestrator():
    transport = FakeAgentTransport("claude_headless", _transport_caps())
    channel = FakeChannelAdapter("telegram", _channel_caps())
    orchestrator = Orchestrator(
        sessions=SessionRegistry(),
        interactions=InteractionStore(),
        outbox=DurableOutbox(),
        channels={"telegram": channel},
        transports={"claude_headless": transport},
        authz=AuthorizationStore(),
    )
    session = asyncio.run(
        orchestrator.start_session(_binding(), "claude_headless", "/tmp/project", _actor())
    )
    channel.sent_views.clear()
    return orchestrator, channel, session


class SdkNarrationConversionTests(unittest.TestCase):
    def test_text_sharing_message_with_tool_use_becomes_narration(self):
        message = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "先看下配置文件"},
                {"type": "tool_use", "id": "tu-1", "name": "Read", "input": {"file_path": "/tmp/x"}},
            ],
        }
        events = ClaudeHeadlessTransport._convert_sdk_dict_message(message)
        self.assertIsInstance(events, list)
        self.assertEqual(events[0].type, AgentEventType.TURN_NARRATION)
        self.assertEqual(events[0].payload["text"], "先看下配置文件")
        # Narration precedes the tool it narrates.
        self.assertEqual(events[1].type, AgentEventType.TOOL_STARTED)
        # And it is NOT doubled as a turn_delta bubble.
        self.assertNotIn(AgentEventType.TURN_DELTA, [e.type for e in events])

    def test_text_only_message_stays_turn_delta(self):
        message = {"role": "assistant", "content": [{"type": "text", "text": "最终回复"}]}
        events = ClaudeHeadlessTransport._convert_sdk_dict_message(message)
        self.assertEqual([e.type for e in events], [AgentEventType.TURN_DELTA])

    def test_user_role_tool_message_emits_no_narration(self):
        # Tool results ride user-role messages; their text is machine input,
        # not agent narration.
        message = {
            "role": "user",
            "content": [
                {"type": "text", "text": "<task-notification>done</task-notification>"},
                {"type": "tool_result", "tool_use_id": "tu-1", "content": "ok"},
            ],
        }
        events = ClaudeHeadlessTransport._convert_sdk_dict_message(message)
        types = [e.type for e in (events or [])]
        self.assertNotIn(AgentEventType.TURN_NARRATION, types)
        self.assertNotIn(AgentEventType.TURN_DELTA, types)


class BurstCardNarrationTests(unittest.TestCase):
    def test_narration_then_tool_share_one_card_in_order(self):
        orchestrator, channel, session = _orchestrator()

        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "先看下配置文件"}
            )
        )
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session,
                channel,
                {
                    "type": "tool_progress",
                    "status": "running",
                    "tool_name": "Read",
                    "tool_id": "tu-1",
                    "summary": "/tmp/x",
                },
            )
        )

        self.assertEqual(len(channel.sent_views), 2)
        first, second = channel.sent_views
        self.assertNotIn("edited", first)  # narration opened the card
        self.assertTrue(second.get("edited"))  # tool line patched it in place
        lines = second["view"]["lines"]
        self.assertEqual(lines[0], {"kind": "narration", "text": "先看下配置文件"})
        self.assertEqual(lines[1]["tool_name"], "Read")

    def test_empty_narration_is_dropped(self):
        orchestrator, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "   "}
            )
        )
        self.assertEqual(channel.sent_views, [])

    def test_narration_is_truncated_into_card_state(self):
        orchestrator, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "x" * 2000}
            )
        )
        lines = channel.sent_views[0]["view"]["lines"]
        self.assertEqual(len(lines[0]["text"]), 600)

    def test_seal_still_clears_narration_lines(self):
        orchestrator, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "narrate"}
            )
        )
        orchestrator._seal_tool_progress_burst(session)
        binding = session.channel_binding
        self.assertNotIn("tool_progress_lines", binding.capabilities)
        self.assertNotIn("tool_progress_message_id", binding.capabilities)


class NarrationRenderingTests(unittest.TestCase):
    def test_lark_card_renders_narration_line_and_ignores_it_for_color(self):
        view = {
            "type": "tool_progress",
            "lines": [
                {"kind": "narration", "text": "先看下配置文件"},
                {"tool_name": "Read", "status": "completed", "summary": "", "tool_id": "t1"},
            ],
        }
        card = _tool_progress_card(view)
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("💬 先看下配置文件", body)
        self.assertIn('"green"', body)  # narration must not hold green on grey

    def test_lark_card_folds_marathon_bursts(self):
        view = {
            "type": "tool_progress",
            "lines": [
                {"tool_name": f"T{i}", "status": "completed", "summary": "", "tool_id": str(i)}
                for i in range(40)
            ],
        }
        body = json.dumps(_tool_progress_card(view), ensure_ascii=False)
        self.assertIn("已折叠前 10 行", body)
        self.assertNotIn("`T5`", body)
        self.assertIn("`T39`", body)

    def test_text_renderer_quotes_narration(self):
        view = {
            "type": "tool_progress",
            "lines": [
                {"kind": "narration", "text": "先看下配置文件"},
                {"tool_name": "Read", "status": "running", "summary": "", "tool_id": "t1"},
            ],
        }
        text = render_view_text(view)
        self.assertIn("> 先看下配置文件", text)
        self.assertIn("Read", text)

    def test_text_renderer_quotes_every_line_of_multiline_narration(self):
        view = {
            "type": "tool_progress",
            "lines": [{"kind": "narration", "text": "第一行\n第二行"}],
        }
        text = render_view_text(view)
        self.assertIn("> 第一行", text)
        self.assertIn("> 第二行", text)
        self.assertNotIn("\n第二行\n", text + "\n")


def _transcript_line(entry: dict) -> bytes:
    return (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")


def _assistant_entry(blocks: list[dict], **extra) -> dict:
    entry = {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    entry.update(extra)
    return entry


class TranscriptNarrationReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.path = self.tmp.name
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _append(self, data: bytes) -> None:
        with open(self.path, "ab") as fh:
            fh.write(data)

    def test_first_sight_fast_forwards_without_emitting(self):
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "history"}])))
        cursor, texts = _read_transcript_narration(self.path, None)
        self.assertEqual(texts, [])
        self.assertEqual(cursor[0], self.path)
        self.assertEqual(cursor[1], os.path.getsize(self.path))

    def test_incremental_append_yields_narration(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(
            _transcript_line(
                _assistant_entry(
                    [
                        {"type": "text", "text": "先看下配置文件"},
                        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                    ]
                )
            )
        )
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["先看下配置文件"])
        # Cursor advanced: a re-read returns nothing.
        _, again = _read_transcript_narration(self.path, cursor)
        self.assertEqual(again, [])

    def test_filters_sidechain_user_and_non_text(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(
            _transcript_line(
                _assistant_entry([{"type": "text", "text": "subagent"}], isSidechain=True)
            )
        )
        self._append(
            _transcript_line({"type": "user", "message": {"content": [{"type": "text", "text": "input"}]}})
        )
        self._append(
            _transcript_line(
                _assistant_entry(
                    [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
                    ]
                )
            )
        )
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "visible"}])))
        _, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["visible"])

    def test_partial_line_stays_for_next_read(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        full = _transcript_line(_assistant_entry([{"type": "text", "text": "complete"}]))
        self._append(full)
        self._append(b'{"type":"assistant","message":')  # torn tail
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["complete"])
        # Finish the torn line; only it comes out on the next read.
        rest = _transcript_line(_assistant_entry([{"type": "text", "text": "tail"}]))[
            len(b'{"type":"assistant","message":') :
        ]
        # Simpler: complete the torn JSON into a valid entry.
        with open(self.path, "ab") as fh:
            fh.write(b'{"role":"assistant","content":[{"type":"text","text":"tail"}]}}\n')
        _ = rest
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["tail"])

    def test_replaced_or_shrunk_file_fast_forwards(self):
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "old"}])))
        cursor, _ = _read_transcript_narration(self.path, None)
        # Truncate below the cursor (e.g. rotated/replaced transcript).
        with open(self.path, "wb") as fh:
            fh.write(_transcript_line(_assistant_entry([{"type": "text", "text": "new file"}]))[:10])
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, [])
        self.assertEqual(cursor[1], os.path.getsize(self.path))

    def test_replaced_file_with_larger_size_is_not_read_from_old_offset(self):
        # An atomic replace can leave a BIGGER file at the same path; reading
        # it from the old offset would leak its history as live narration.
        # The (st_dev, st_ino) identity in the cursor catches it.
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "old"}])))
        cursor, _ = _read_transcript_narration(self.path, None)
        replacement = self.path + ".new"
        with open(replacement, "wb") as fh:
            for i in range(5):
                fh.write(_transcript_line(_assistant_entry([{"type": "text", "text": f"history-{i}"}])))
        os.replace(replacement, self.path)
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, [])
        self.assertEqual(cursor[1], os.path.getsize(self.path))

    def test_boundary_caps_read_at_hook_capture_time(self):
        # Bytes written after the hook fired belong to a later hook: a
        # delayed drain must not lift the turn-final text into the card (it
        # is about to go out as the Stop bubble).
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "叙述"}])))
        boundary = os.path.getsize(self.path)
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "回合末文本"}])))

        cursor, texts = _read_transcript_narration(self.path, cursor, boundary)
        self.assertEqual(texts, ["叙述"])
        self.assertEqual(cursor[1], boundary)

    def test_monster_line_does_not_wedge_cursor(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(b'{"pad":"' + b"x" * (2 * 1024 * 1024 + 100_000) + b'"}\n')
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "after"}])))

        texts: list[str] = []
        for _ in range(6):  # bounded loop: each read must make progress
            cursor, batch = _read_transcript_narration(self.path, cursor)
            texts.extend(batch)
            if cursor[1] >= os.path.getsize(self.path):
                break
        self.assertEqual(texts, ["after"])
        self.assertEqual(cursor[1], os.path.getsize(self.path))

    def test_missing_file_returns_no_cursor(self):
        # Storing (path, 0) for an unreadable file would replay its entire
        # history once it appears; the caller must get None and store nothing.
        cursor, texts = _read_transcript_narration("/nonexistent/transcript.jsonl", None)
        self.assertEqual(texts, [])
        self.assertIsNone(cursor)


if __name__ == "__main__":
    unittest.main()
