"""Regression tests for Stop hook last-assistant-message extraction.

Background: Claude Code's Stop hook supplies `last_assistant_message`, but
when the final assistant turn ends on a pure tool_use (no text block) that
field arrives empty. Before v0.10.13 the Feishu thread then showed only the
"✅ Task complete" label with no reply body. _read_last_assistant_text is
the fallback that tails the transcript JSONL to recover the most recent
assistant text.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from unittest import mock

from walkcode.__main__ import (
    _find_codex_rollout,
    _is_user_turn_start,
    _read_codex_turn_messages,
    _read_last_assistant_text,
    _read_turn_assistant_texts,
)


def _assistant(text_blocks=None, tool_uses=None, sidechain=False, **extra):
    content = []
    for t in text_blocks or []:
        content.append({"type": "text", "text": t})
    for name in tool_uses or []:
        content.append({"type": "tool_use", "name": name, "input": {}})
    rec = {"type": "assistant", "message": {"content": content}}
    if sidechain:
        rec["isSidechain"] = True
    rec.update(extra)
    return rec


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _tool_result(text="ok"):
    """A `user` transcript record that is a tool_result echo, not a real prompt."""
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "x", "content": text},
    ]}}


def _write_jsonl(path: Path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _codex(ptype, message=""):
    return {"type": "response_item" if ptype != "user_message" else "event_msg",
            "payload": {"type": ptype, "message": message}}


class ReadLastAssistantTextTests(unittest.TestCase):

    def test_empty_path_returns_empty(self):
        self.assertEqual(_read_last_assistant_text(""), "")

    def test_missing_file_returns_empty(self):
        self.assertEqual(_read_last_assistant_text("/no/such/file.jsonl"), "")

    def test_final_assistant_text_returned(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("hi"),
                _assistant(text_blocks=["hello world"]),
            ])
            self.assertEqual(_read_last_assistant_text(str(p)), "hello world")

    def test_final_tool_use_falls_back_to_prior_text(self):
        # This is the bug shape: last assistant turn is a pure tool_use.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("q"),
                _assistant(text_blocks=["summary body"]),
                _user("tool_result"),
                _assistant(tool_uses=["TaskUpdate"]),  # no text
            ])
            self.assertEqual(_read_last_assistant_text(str(p)), "summary body")

    def test_mixed_text_and_tool_use_concatenated(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _assistant(text_blocks=["part1\n", "part2"], tool_uses=["Bash"]),
            ])
            self.assertEqual(_read_last_assistant_text(str(p)), "part1\npart2")

    def test_truncates_oversized_text(self):
        big = "x" * 50000
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [_assistant(text_blocks=[big])])
            out = _read_last_assistant_text(str(p), max_chars=1000)
            self.assertTrue(out.endswith("…(truncated)"))
            self.assertEqual(len(out), 1000 + len("\n…(truncated)"))

    def test_corrupt_line_skipped(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text(
                "not json\n"
                + json.dumps(_assistant(text_blocks=["ok"])) + "\n"
                + "{broken\n"
            )
            self.assertEqual(_read_last_assistant_text(str(p)), "ok")

    def test_no_assistant_text_returns_empty(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("hi"),
                _assistant(tool_uses=["Bash"]),
            ])
            self.assertEqual(_read_last_assistant_text(str(p)), "")


class IsUserTurnStartTests(unittest.TestCase):
    """Distinguishing a real prompt from a tool_result echo (the turn boundary)."""

    def test_string_content_is_boundary(self):
        self.assertTrue(_is_user_turn_start(_user("hello")))

    def test_text_array_is_boundary(self):
        rec = {"type": "user", "message": {"content": [
            {"type": "text", "text": "hi"}, {"type": "image", "source": {}},
        ]}}
        self.assertTrue(_is_user_turn_start(rec))

    def test_tool_result_is_not_boundary(self):
        self.assertFalse(_is_user_turn_start(_tool_result()))


class ReadTurnAssistantTextsTests(unittest.TestCase):
    """The fix: a whole turn's segments, not just the last one."""

    def test_empty_and_missing_path(self):
        self.assertEqual(_read_turn_assistant_texts(""), "")
        self.assertEqual(_read_turn_assistant_texts("/no/such.jsonl"), "")

    def test_single_segment_matches_last_message(self):
        # Simple turn (no tools): result equals the one segment — no behavior change.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [_user("hi"), _assistant(text_blocks=["just one"])])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "just one")

    def test_multi_segment_turn_concatenated_in_order(self):
        # The bug shape: narration interleaved with tool calls. All segments kept.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("do the thing"),
                _assistant(text_blocks=["step one"], tool_uses=["Bash"]),
                _tool_result(),
                _assistant(text_blocks=["step two"], tool_uses=["Bash"]),
                _tool_result(),
                _assistant(text_blocks=["all done"]),  # final segment
            ])
            self.assertEqual(
                _read_turn_assistant_texts(str(p)),
                "step one\n\nstep two\n\nall done",
            )

    def test_tool_result_does_not_reset_turn(self):
        # tool_result user records must NOT be treated as a new turn boundary.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("q"),
                _assistant(text_blocks=["a"], tool_uses=["Bash"]),
                _tool_result(),
                _assistant(text_blocks=["b"]),
            ])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "a\n\nb")

    def test_only_latest_turn_returned(self):
        # A real prior prompt's segments are dropped.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("turn 1"),
                _assistant(text_blocks=["old answer"]),
                _user("turn 2"),
                _assistant(text_blocks=["new part 1"], tool_uses=["Bash"]),
                _tool_result(),
                _assistant(text_blocks=["new part 2"]),
            ])
            self.assertEqual(
                _read_turn_assistant_texts(str(p)),
                "new part 1\n\nnew part 2",
            )

    def test_sidechain_segments_skipped(self):
        # Subagent (Task) chatter shares the file under isSidechain → excluded.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("q"),
                _assistant(text_blocks=["main 1"], tool_uses=["Task"]),
                _assistant(text_blocks=["subagent noise"], sidechain=True),
                _tool_result(),
                _assistant(text_blocks=["main 2"]),
            ])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "main 1\n\nmain 2")

    def test_meta_user_record_does_not_reset_turn(self):
        # Hook-injected meta user records must not truncate the turn.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            meta = _user("<system reminder>")
            meta["isMeta"] = True
            _write_jsonl(p, [
                _user("q"),
                _assistant(text_blocks=["seg1"], tool_uses=["Bash"]),
                _tool_result(),
                meta,
                _assistant(text_blocks=["seg2"]),
            ])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "seg1\n\nseg2")

    def test_truncation_preserves_tail_not_head(self):
        # Over budget → drop the LEADING narration, keep the final answer (the tail).
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("q"),
                _assistant(text_blocks=["A" * 4000], tool_uses=["Bash"]),  # narration
                _tool_result(),
                _assistant(text_blocks=["FINAL_ANSWER" + "z" * 500]),       # conclusion
            ])
            out = _read_turn_assistant_texts(str(p), max_chars=1000)
            self.assertTrue(out.startswith("…(truncated)"))
            self.assertTrue(out.endswith("z"))           # tail (conclusion) kept
            self.assertNotIn("A" * 4000, out)            # leading narration dropped
            self.assertEqual(len(out), len("…(truncated)\n") + 1000)

    def test_no_text_returns_empty(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [_user("q"), _assistant(tool_uses=["Bash"])])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "")

    def test_no_user_boundary_returns_empty(self):
        # Compacted/truncated transcript with no real prompt: must NOT dump history.
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _assistant(text_blocks=["orphan reply 1"]),
                _tool_result(),
                _assistant(text_blocks=["orphan reply 2"]),
            ])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "")

    def test_null_message_record_does_not_crash(self):
        # A `user`/`assistant` record whose message is JSON null must be skipped,
        # not raise AttributeError (which would kill the hook → no Feishu reply).
        with TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            _write_jsonl(p, [
                _user("q"),
                {"type": "assistant", "message": None},
                {"type": "user", "message": None},
                _assistant(text_blocks=["real answer"]),
            ])
            self.assertEqual(_read_turn_assistant_texts(str(p)), "real answer")


class ReadCodexTurnMessagesTests(unittest.TestCase):
    """Codex: rollout-by-session-id, all agent_message since last user_message."""

    def _write_rollout(self, d, records):
        p = Path(d) / "rollout.jsonl"
        _write_jsonl(p, records)
        return str(p)

    def test_multi_segment_turn_concatenated(self):
        with TemporaryDirectory() as d:
            path = self._write_rollout(d, [
                _codex("user_message", "do it"),
                _codex("agent_message", "preamble one"),
                _codex("function_call"),
                _codex("agent_message", "preamble two"),
                _codex("function_call"),
                _codex("agent_message", "final answer"),
            ])
            with mock.patch("walkcode.__main__._find_codex_rollout", return_value=path):
                self.assertEqual(
                    _read_codex_turn_messages("sid"),
                    "preamble one\n\npreamble two\n\nfinal answer",
                )

    def test_only_latest_turn(self):
        with TemporaryDirectory() as d:
            path = self._write_rollout(d, [
                _codex("user_message", "t1"),
                _codex("agent_message", "old"),
                _codex("user_message", "t2"),
                _codex("agent_message", "new a"),
                _codex("agent_message", "new b"),
            ])
            with mock.patch("walkcode.__main__._find_codex_rollout", return_value=path):
                self.assertEqual(_read_codex_turn_messages("sid"), "new a\n\nnew b")

    def test_missing_rollout_returns_empty(self):
        with mock.patch("walkcode.__main__._find_codex_rollout", return_value=""):
            self.assertEqual(_read_codex_turn_messages("sid"), "")

    def test_no_user_boundary_returns_empty(self):
        # rollout with agent_messages but no real user_message → don't dump history
        with TemporaryDirectory() as d:
            path = self._write_rollout(d, [
                _codex("agent_message", "orphan a"),
                _codex("agent_message", "orphan b"),
            ])
            with mock.patch("walkcode.__main__._find_codex_rollout", return_value=path):
                self.assertEqual(_read_codex_turn_messages("sid"), "")

    def test_message_role_records_not_double_counted(self):
        # response_item `message`/role=assistant duplicates the agent_message event;
        # only agent_message must be counted, so each segment appears once.
        with TemporaryDirectory() as d:
            p = Path(d) / "rollout.jsonl"
            _write_jsonl(p, [
                _codex("user_message", "go"),
                _codex("agent_message", "seg one"),
                {"type": "response_item", "payload": {"type": "message",
                 "role": "assistant", "content": [{"type": "text", "text": "seg one"}]}},
                _codex("agent_message", "seg two"),
            ])
            with mock.patch("walkcode.__main__._find_codex_rollout", return_value=str(p)):
                self.assertEqual(_read_codex_turn_messages("sid"), "seg one\n\nseg two")

    def test_prefers_provided_rollout_path_over_lookup(self):
        # When the hook hands a rollout path, use it without scanning the tree.
        with TemporaryDirectory() as d:
            p = Path(d) / "rollout-2026-06-19T13-41-50-sid.jsonl"
            _write_jsonl(p, [
                _codex("user_message", "go"), _codex("agent_message", "from path"),
            ])
            # _find_codex_rollout must NOT be consulted when a rollout path is given
            with mock.patch("walkcode.__main__._find_codex_rollout",
                            side_effect=AssertionError("should not scan")):
                self.assertEqual(_read_codex_turn_messages("sid", str(p)), "from path")

    def test_ignores_non_rollout_path_and_falls_back_to_lookup(self):
        # A provided path that isn't a rollout file (wrong format/name) is ignored;
        # the session-id lookup is used instead, so codex never regresses to silence.
        with TemporaryDirectory() as d:
            good = Path(d) / "rollout-x-sid.jsonl"
            _write_jsonl(good, [
                _codex("user_message", "go"), _codex("agent_message", "via lookup"),
            ])
            with mock.patch("walkcode.__main__._find_codex_rollout", return_value=str(good)):
                self.assertEqual(
                    _read_codex_turn_messages("sid", "/some/other.jsonl"), "via lookup")

    def test_missing_rollout_returns_empty_when_path_blank(self):
        with mock.patch("walkcode.__main__._find_codex_rollout", return_value=""):
            self.assertEqual(_read_codex_turn_messages("sid", ""), "")

    def test_find_codex_rollout_picks_newest_of_multiple_matches(self):
        import os as _os
        with TemporaryDirectory() as home:
            sid = "019ede66-5be5-7a93-a74f-8dde343f839f"
            day = Path(home) / ".codex" / "sessions" / "2026" / "06" / "19"
            day.mkdir(parents=True)
            older = day / f"rollout-2026-06-19T10-00-00-{sid}.jsonl"
            newer = day / f"rollout-2026-06-19T13-41-50-{sid}.jsonl"
            older.write_text("{}\n"); newer.write_text("{}\n")
            _os.utime(older, (1_000_000, 1_000_000))
            _os.utime(newer, (2_000_000, 2_000_000))
            with mock.patch("walkcode.__main__.Path.home", return_value=Path(home)):
                self.assertEqual(_find_codex_rollout(sid), str(newer))

    def test_find_codex_rollout_by_session_id(self):
        with TemporaryDirectory() as home:
            sid = "019ede66-5be5-7a93-a74f-8dde343f839f"
            day = Path(home) / ".codex" / "sessions" / "2026" / "06" / "19"
            day.mkdir(parents=True)
            target = day / f"rollout-2026-06-19T13-41-50-{sid}.jsonl"
            target.write_text("{}\n")
            (day / "rollout-2026-06-19T10-00-00-other.jsonl").write_text("{}\n")
            with mock.patch("walkcode.__main__.Path.home", return_value=Path(home)):
                self.assertEqual(_find_codex_rollout(sid), str(target))
                self.assertEqual(_find_codex_rollout(""), "")

    def test_find_codex_rollout_rejects_glob_metachars(self):
        # A session_id with glob metacharacters must not widen the match.
        with TemporaryDirectory() as home:
            day = Path(home) / ".codex" / "sessions" / "2026" / "06" / "19"
            day.mkdir(parents=True)
            (day / "rollout-2026-06-19T10-00-00-realsid.jsonl").write_text("{}\n")
            with mock.patch("walkcode.__main__.Path.home", return_value=Path(home)):
                self.assertEqual(_find_codex_rollout("*"), "")
                self.assertEqual(_find_codex_rollout("re?lsid"), "")


if __name__ == "__main__":
    unittest.main()
