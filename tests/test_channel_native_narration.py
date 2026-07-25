"""ADR 0055: mid-turn assistant narration mirrors onto the tool-progress card.

Narration is the text the agent emits right before tool calls ("what I'm
about to do"). Both mirror paths used to drop it entirely:
- headless: _convert_sdk_message discarded text sharing a message with
  tool_use blocks;
- TUI hooks: no hook payload ever carries it (only Stop's final text).

Final form (ADR 0055 revision 2, user's call): narration reaches the
channel as PLAIN MESSAGES on both paths — headless already emits them
naturally (each content block streams as its own assistant message, so
text-only messages become turn_delta bubbles); the TUI path drains the
transcript incrementally and posts the same bubbles. The card machinery
(TURN_NARRATION -> 💬 line) remains only as a fallback for combined
text+tool messages, which the live CLI never produces.
"""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

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
    return orchestrator, transport, channel, session


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
        orchestrator, _transport, channel, session = _orchestrator()

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
        orchestrator, _transport, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "   "}
            )
        )
        self.assertEqual(channel.sent_views, [])

    def test_narration_is_truncated_into_card_state(self):
        orchestrator, _transport, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "x" * 2000}
            )
        )
        lines = channel.sent_views[0]["view"]["lines"]
        self.assertEqual(len(lines[0]["text"]), 600)

    def test_seal_still_clears_narration_lines(self):
        orchestrator, _transport, channel, session = _orchestrator()
        asyncio.run(
            orchestrator._upsert_tool_progress_view(
                session, channel, {"type": "turn_narration", "text": "narrate"}
            )
        )
        orchestrator._seal_tool_progress_burst(session)
        binding = session.channel_binding
        self.assertNotIn("tool_progress_lines", binding.capabilities)
        self.assertNotIn("tool_progress_message_id", binding.capabilities)


class TuiHookNarrationDeliveryTests(unittest.TestCase):
    """The TUI mirror path posts drained narration as PLAIN MESSAGES before
    the tool line (final form). Pins the runtime wiring itself — reverting
    the _send_tui_hook_output change must redden this."""

    def setUp(self):
        import time as _time

        from walkcode.channel_native_runtime import ChannelNativeRuntime

        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.path = self.tmp.name
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

        orchestrator, _transport, channel, session = _orchestrator()
        self.orchestrator = orchestrator
        self.channel = channel
        self.session = session

        class _HookHost:
            _send_tui_hook_output = ChannelNativeRuntime._send_tui_hook_output
            _drain_tui_narration = ChannelNativeRuntime._drain_tui_narration
            _advance_tui_narration_cursor = ChannelNativeRuntime._advance_tui_narration_cursor
            _store_tui_narration_cursor = ChannelNativeRuntime._store_tui_narration_cursor

        host = _HookHost()
        host.orchestrator = orchestrator
        host.channels = {"telegram": channel}
        host._tui_transcript_cursors = {}
        host._now = _time.time
        self.host = host

    def _append(self, entry: dict) -> None:
        with open(self.path, "ab") as fh:
            fh.write(_transcript_line(entry))

    def _stamped_payload(self, **extra) -> dict:
        info = os.stat(self.path)
        payload = {
            "transcript_path": self.path,
            "_walkcode_transcript_size": int(info.st_size),
            "_walkcode_transcript_file_key": [int(info.st_dev), int(info.st_ino)],
        }
        payload.update(extra)
        return payload

    def _views(self, kind: str) -> list:
        return [v for v in self.channel.sent_views if v["view"].get("type") == kind]

    def test_narration_posts_as_plain_message_before_tool_line(self):
        # Hook 1 initializes the cursor (no history replay).
        asyncio.run(
            self.host._send_tui_hook_output(
                self.session,
                hook_type="pre-tool",
                payload=self._stamped_payload(tool_name="Bash", tool_use_id="t1"),
            )
        )
        self.assertEqual(self._views("turn_delta"), [])

        # Narration lands in the transcript, then the next tool hook fires.
        self._append(_assistant_entry([{"type": "text", "text": "先查日志"}]))
        payload2 = self._stamped_payload(tool_name="Bash", tool_use_id="t2")
        asyncio.run(
            self.host._send_tui_hook_output(self.session, hook_type="pre-tool", payload=payload2)
        )

        deltas = self._views("turn_delta")
        self.assertEqual([d["view"]["text"] for d in deltas], ["先查日志"])
        # Plain message, not a 💬 card line.
        for card in self._views("tool_progress"):
            for line in card["view"].get("lines", []):
                if isinstance(line, dict):
                    self.assertNotEqual(line.get("kind"), "narration")
        # Ordering: the narration bubble precedes the t2 tool card update.
        types = [v["view"].get("type") for v in self.channel.sent_views]
        self.assertLess(
            types.index("turn_delta"),
            max(i for i, t in enumerate(types) if t == "tool_progress"),
        )

        # Replaying the same hook must not repeat the narration (cursor moved).
        asyncio.run(
            self.host._send_tui_hook_output(self.session, hook_type="pre-tool", payload=payload2)
        )
        self.assertEqual(len(self._views("turn_delta")), 1)

    def test_stop_hook_final_text_is_not_reposted_by_narration_path(self):
        asyncio.run(
            self.host._send_tui_hook_output(
                self.session,
                hook_type="pre-tool",
                payload=self._stamped_payload(tool_name="Bash", tool_use_id="t1"),
            )
        )
        # Turn-final text lands in the transcript; Stop mirrors it as its own
        # message and the cursor must skip past it.
        self._append(_assistant_entry([{"type": "text", "text": "最终回复"}]))
        asyncio.run(
            self.host._send_tui_hook_output(
                self.session,
                hook_type="stop",
                payload=self._stamped_payload(last_assistant_message="最终回复"),
            )
        )
        completed = [v["view"].get("message") for v in self._views("turn_completed")]
        self.assertEqual(completed, ["最终回复"])  # the Stop bubble itself
        self.assertEqual(self._views("turn_delta"), [])  # no narration double

        # A later tool hook must not re-read the already-skipped final text.
        asyncio.run(
            self.host._send_tui_hook_output(
                self.session,
                hook_type="pre-tool",
                payload=self._stamped_payload(tool_name="Bash", tool_use_id="t2"),
            )
        )
        self.assertEqual(self._views("turn_delta"), [])


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


def _codex_agent_message(text: str) -> dict:
    return {"type": "event_msg", "payload": {"type": "agent_message", "message": text}}


def _codex_response_item(text: str) -> dict:
    # Codex writes every assistant message a second time as a response_item;
    # the reader must ignore this form or every message posts twice.
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


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

        cursor, texts = _read_transcript_narration(self.path, cursor, (boundary, None))
        self.assertEqual(texts, ["叙述"])
        self.assertEqual(cursor[1], boundary)

    def test_boundary_from_replaced_file_is_not_applied(self):
        # A boundary stamped on file A must not be applied to file B at the
        # same path: fast-forwarding a fresh cursor to A's boundary inside B
        # would expose B's remaining history as live narration on the next
        # read (deep-review R2 counterexample).
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "old"}])))
        info = os.stat(self.path)
        old_boundary = (os.path.getsize(self.path), (int(info.st_dev), int(info.st_ino)))

        replacement = self.path + ".new"
        with open(replacement, "wb") as fh:
            for i in range(5):
                fh.write(_transcript_line(_assistant_entry([{"type": "text", "text": f"history-{i}"}])))
        os.replace(replacement, self.path)

        cursor, texts = _read_transcript_narration(self.path, None, old_boundary)
        self.assertEqual(texts, [])
        # First sight of the replaced file lands at ITS EOF, not at the old
        # boundary — so no later read can emit its history.
        self.assertEqual(cursor[1], os.path.getsize(self.path))
        _, again = _read_transcript_narration(self.path, cursor)
        self.assertEqual(again, [])

    def test_overlong_line_tail_fragment_is_never_parsed(self):
        # If the bytes right after the batch cap happen to BE a valid
        # assistant JSON document, a naive skip would feed them to the
        # parser as a "line". The discard state must drop everything up to
        # the over-long line's real newline.
        cursor, _ = _read_transcript_narration(self.path, None)
        bait = _transcript_line(_assistant_entry([{"type": "text", "text": "poisoned"}]))
        self._append(b" " * (2 * 1024 * 1024) + bait)  # ONE physical line
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "clean"}])))

        texts: list[str] = []
        for _ in range(8):
            cursor, batch = _read_transcript_narration(self.path, cursor)
            texts.extend(batch)
            if cursor[1] >= os.path.getsize(self.path):
                break
        self.assertEqual(texts, ["clean"])

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

    def test_keyless_boundary_never_positions_fresh_cursor(self):
        # A size-only boundary (legacy payload, no file identity) cannot
        # prove which file it measured: using it to place a FRESH cursor
        # inside the current file could land mid-history of a replacement.
        # It must fast-forward to EOF instead.
        for i in range(5):
            self._append(_transcript_line(_assistant_entry([{"type": "text", "text": f"h-{i}"}])))
        cursor, texts = _read_transcript_narration(self.path, None, (10, None))
        self.assertEqual(texts, [])
        self.assertEqual(cursor[1], os.path.getsize(self.path))
        _, again = _read_transcript_narration(self.path, cursor)
        self.assertEqual(again, [])

    def test_discard_completion_parses_remainder_in_same_batch(self):
        # Once the over-long line's newline is found, the rest of the batch
        # must parse IN THE SAME CALL — returning early would delay legit
        # narration by one hook, or lose it to a following Stop advance.
        self._append(b"x" * 1000 + b"\n")
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "legit"}])))
        info = os.stat(self.path)
        cursor = (self.path, 500, (int(info.st_dev), int(info.st_ino)), True)  # mid junk line

        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["legit"])
        self.assertEqual(cursor[1], os.path.getsize(self.path))
        self.assertFalse(cursor[3])

    def test_advance_preserves_in_progress_discard(self):
        # A forward jump cannot prove it crossed the over-long line's real
        # newline; clearing the discard flag would hand the line's tail
        # (potentially a valid assistant JSON) to the parser.
        from walkcode.channel_native_runtime import ChannelNativeRuntime

        class _CursorHost:
            _advance_tui_narration_cursor = ChannelNativeRuntime._advance_tui_narration_cursor
            _store_tui_narration_cursor = ChannelNativeRuntime._store_tui_narration_cursor

            def __init__(self):
                self._tui_transcript_cursors = {}

        class _Session:
            session_id = "sess-1"

        self._append(b" " * 3000)  # over-long line still open, no newline
        info = os.stat(self.path)
        file_key = (int(info.st_dev), int(info.st_ino))
        host = _CursorHost()
        host._tui_transcript_cursors["sess-1"] = (self.path, 1000, file_key, True)

        payload = {
            "transcript_path": self.path,
            "_walkcode_transcript_size": 2500,
            "_walkcode_transcript_file_key": list(file_key),
        }
        host._advance_tui_narration_cursor(_Session(), payload)

        cursor = host._tui_transcript_cursors["sess-1"]
        self.assertEqual(cursor[1], 2500)
        self.assertTrue(cursor[3])  # discard state survives the jump

    def test_advance_with_keyless_boundary_and_no_prior_cursor_jumps_to_eof(self):
        # Same hole as the reader's: a legacy size-only boundary must not
        # position a FRESH advance cursor inside a (possibly replaced) file.
        from walkcode.channel_native_runtime import ChannelNativeRuntime

        class _CursorHost:
            _advance_tui_narration_cursor = ChannelNativeRuntime._advance_tui_narration_cursor
            _store_tui_narration_cursor = ChannelNativeRuntime._store_tui_narration_cursor

            def __init__(self):
                self._tui_transcript_cursors = {}

        class _Session:
            session_id = "sess-1"

        for i in range(5):
            self._append(_transcript_line(_assistant_entry([{"type": "text", "text": f"h-{i}"}])))
        host = _CursorHost()
        host._advance_tui_narration_cursor(
            _Session(), {"transcript_path": self.path, "_walkcode_transcript_size": 10}
        )
        cursor = host._tui_transcript_cursors["sess-1"]
        self.assertEqual(cursor[1], os.path.getsize(self.path))
        _, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, [])

    def test_trimmed_window_never_flags_overlong(self):
        # A cap-sized batch = [discarded line's newline + incomplete prefix
        # of the next line]: the trimmed window is partial, so it must NOT be
        # used to flag the next line as over-long — that would discard a
        # legitimate (near-cap but legal) narration line from its start.
        cap = 2 * 1024 * 1024
        junk = b"j" * 100 + b"\n"
        self._append(junk)
        base = _assistant_entry([{"type": "text", "text": "big"}])
        overhead = len(_transcript_line({**base, "padding": ""}))
        big_line = _transcript_line({**base, "padding": "y" * (cap - 60 - overhead)})
        self.assertTrue(cap - 101 < len(big_line) <= cap)  # legal, but fills the trimmed window
        self._append(big_line)
        self._append(_transcript_line(_assistant_entry([{"type": "text", "text": "fine"}])))
        info = os.stat(self.path)
        cursor = (self.path, 0, (int(info.st_dev), int(info.st_ino)), True)

        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, [])
        self.assertEqual(cursor[1], len(junk))  # parked at the line start
        self.assertFalse(cursor[3])  # NOT flagged over-long from a partial window

        texts_all: list[str] = []
        for _ in range(8):
            cursor, batch = _read_transcript_narration(self.path, cursor)
            texts_all.extend(batch)
            if cursor[1] >= os.path.getsize(self.path):
                break
        # The near-cap line survives; flagging it over-long would have
        # silently discarded "big".
        self.assertEqual(texts_all, ["big", "fine"])

    def test_missing_file_returns_no_cursor(self):
        # Storing (path, 0) for an unreadable file would replay its entire
        # history once it appears; the caller must get None and store nothing.
        cursor, texts = _read_transcript_narration("/nonexistent/transcript.jsonl", None)
        self.assertEqual(texts, [])
        self.assertIsNone(cursor)


class CodexRolloutNarrationReaderTests(unittest.TestCase):
    """Codex rollouts carry assistant text as event_msg/agent_message records;
    the reader used to parse only the Claude dialect and silently yielded
    nothing for codex sessions — mid-turn narration never reached the channel
    (2026-07-25 sigma-feishu-client incident)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.path = self.tmp.name
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _append(self, entry: dict) -> None:
        with open(self.path, "ab") as fh:
            fh.write(_transcript_line(entry))

    def test_agent_message_yields_narration(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(_codex_agent_message("你这四点都抓得对"))
        cursor, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["你这四点都抓得对"])
        _, again = _read_transcript_narration(self.path, cursor)
        self.assertEqual(again, [])

    def test_response_item_duplicate_is_ignored(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(_codex_agent_message("中段叙述"))
        self._append(_codex_response_item("中段叙述"))
        _, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["中段叙述"])

    def test_non_message_event_msg_records_are_ignored(self):
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append({"type": "event_msg", "payload": {"type": "agent_reasoning", "text": "内心戏"}})
        self._append({"type": "event_msg", "payload": {"type": "token_count", "info": {}}})
        self._append({"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}})
        self._append({"type": "event_msg", "payload": "not-a-dict"})
        self._append(_codex_agent_message("可见文本"))
        _, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["可见文本"])

    def test_mixed_claude_and_codex_dialects_both_parse(self):
        # One reader serves both agents; a rollout line must not break the
        # Claude branch or vice versa.
        cursor, _ = _read_transcript_narration(self.path, None)
        self._append(_codex_agent_message("codex 叙述"))
        self._append(_assistant_entry([{"type": "text", "text": "claude 叙述"}]))
        _, texts = _read_transcript_narration(self.path, cursor)
        self.assertEqual(texts, ["codex 叙述", "claude 叙述"])


class CodexStopDrainTests(unittest.TestCase):
    """A codex turn that says several things has no hook carrier for any of
    them except the last (stop's last_assistant_message). The stop handler
    must drain the un-mirrored tail from the rollout, minus the stop bubble
    itself; Claude sessions keep the advance-only path (MessageDisplay already
    mirrored their text)."""

    def setUp(self):
        import time as _time

        from walkcode.channel_native_runtime import ChannelNativeRuntime

        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jsonl")
        self.path = self.tmp.name
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

        orchestrator, _transport, channel, session = _orchestrator()
        self.orchestrator = orchestrator
        self.channel = channel
        self.session = session

        class _HookHost:
            _send_tui_hook_output = ChannelNativeRuntime._send_tui_hook_output
            _drain_tui_narration = ChannelNativeRuntime._drain_tui_narration
            _advance_tui_narration_cursor = ChannelNativeRuntime._advance_tui_narration_cursor
            _store_tui_narration_cursor = ChannelNativeRuntime._store_tui_narration_cursor

        host = _HookHost()
        host.orchestrator = orchestrator
        host.channels = {"telegram": channel}
        host._tui_transcript_cursors = {}
        host._now = _time.time
        self.host = host

    def _append(self, entry: dict) -> None:
        with open(self.path, "ab") as fh:
            fh.write(_transcript_line(entry))

    def _stamped_payload(self, **extra) -> dict:
        info = os.stat(self.path)
        payload = {
            "transcript_path": self.path,
            "_walkcode_transcript_size": int(info.st_size),
            "_walkcode_transcript_file_key": [int(info.st_dev), int(info.st_ino)],
        }
        payload.update(extra)
        return payload

    def _views(self, kind: str) -> list:
        return [v for v in self.channel.sent_views if v["view"].get("type") == kind]

    def _run(self, hook_type: str, payload: dict, agent: str = "codex") -> None:
        asyncio.run(
            self.host._send_tui_hook_output(
                self.session, hook_type=hook_type, payload=payload, agent=agent
            )
        )

    def test_stop_drains_trailing_narration_and_dedupes_final_bubble(self):
        # Cursor initialization (first sight fast-forwards, no replay).
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"))
        # The turn then says something mid-stream AND closes with a final
        # message; no tool hook follows, so stop is the only drain point.
        self._append(_codex_agent_message("中段叙述，后面没有工具调用"))
        self._append(_codex_agent_message("最终回复"))
        self._append(_codex_response_item("最终回复"))
        self._run("stop", self._stamped_payload(last_assistant_message="最终回复"))

        deltas = [v["view"]["text"] for v in self._views("turn_delta")]
        self.assertEqual(deltas, ["中段叙述，后面没有工具调用"])
        completed = [v["view"].get("message") for v in self._views("turn_completed")]
        self.assertEqual(completed, ["最终回复"])  # the stop bubble, exactly once

        # A later hook must not re-read the drained region.
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t2"))
        self.assertEqual(len(self._views("turn_delta")), 1)

    def test_mid_turn_agent_message_drains_at_next_tool_hook(self):
        # The primary carrier for mid-turn codex text: the drain that runs
        # before every tool line (ADR 0055), now codex-format-aware.
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"))
        self._append(_codex_agent_message("你这四点都抓得对"))
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t2"))

        deltas = [v["view"]["text"] for v in self._views("turn_delta")]
        self.assertEqual(deltas, ["你这四点都抓得对"])

    def test_claude_stop_keeps_advance_only_path(self):
        # Claude's closing text arrives via its MessageDisplay hook; draining
        # at stop would double it. agent="claude" (and the legacy no-agent
        # call) must keep skipping the tail instead of posting it.
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"), agent="claude")
        self._append(_assistant_entry([{"type": "text", "text": "最终回复"}]))
        self._run("stop", self._stamped_payload(last_assistant_message="最终回复"), agent="claude")

        self.assertEqual(self._views("turn_delta"), [])
        completed = [v["view"].get("message") for v in self._views("turn_completed")]
        self.assertEqual(completed, ["最终回复"])

    def test_stop_with_empty_final_text_still_drains_narration(self):
        # An aborted codex turn can stop without last_assistant_message; the
        # tail narration must still come out instead of being skipped.
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"))
        self._append(_codex_agent_message("说到一半"))
        self._run("stop", self._stamped_payload())

        deltas = [v["view"]["text"] for v in self._views("turn_delta")]
        self.assertEqual(deltas, ["说到一半"])
        self.assertEqual(self._views("turn_completed"), [])

    def test_stop_dedupe_keeps_earlier_message_with_same_text(self):
        # Only the LAST match is the stop bubble: an earlier agent message
        # that happens to repeat the final text is a real message and must
        # not be swallowed by the dedupe (deep-review round 1).
        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"))
        self._append(_codex_agent_message("重复的话"))
        self._append(_codex_agent_message("中间插一句"))
        self._append(_codex_agent_message("重复的话"))
        self._run("stop", self._stamped_payload(last_assistant_message="重复的话"))

        deltas = [v["view"]["text"] for v in self._views("turn_delta")]
        self.assertEqual(deltas, ["重复的话", "中间插一句"])
        completed = [v["view"].get("message") for v in self._views("turn_completed")]
        self.assertEqual(completed, ["重复的话"])

    def test_stop_drains_past_single_batch_read_cap(self):
        # One drain call reads at most _TRANSCRIPT_READ_MAX_BYTES; the stop
        # path must LOOP until the cursor reaches the capture boundary, or
        # the advance would silently skip everything after the first batch
        # (deep-review round 1). Cap shrunk so each read consumes ~1 line.
        import walkcode.channel_native_runtime as runtime_module

        self._run("pre-tool", self._stamped_payload(tool_name="Bash", tool_use_id="t1"))
        for i in range(5):
            self._append(_codex_agent_message(f"第{i}段"))
        self._append(_codex_agent_message("最终回复"))
        with patch.object(runtime_module, "_TRANSCRIPT_READ_MAX_BYTES", 128):
            self._run("stop", self._stamped_payload(last_assistant_message="最终回复"))

        deltas = [v["view"]["text"] for v in self._views("turn_delta")]
        self.assertEqual(deltas, [f"第{i}段" for i in range(5)])
        completed = [v["view"].get("message") for v in self._views("turn_completed")]
        self.assertEqual(completed, ["最终回复"])


if __name__ == "__main__":
    unittest.main()
