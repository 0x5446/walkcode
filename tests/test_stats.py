"""Tests for walkcode.stats — read-only session statistics collection."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from walkcode import stats
from walkcode.stats import (
    collect_stats,
    collect_claude_stats,
    collect_codex_stats,
    ModelTokens,
    SessionStats,
)


def _write_jsonl(records) -> str:
    d = tempfile.mkdtemp()
    p = Path(d) / "sess.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records))
    return str(p)


class ClaudeStatsTest(unittest.TestCase):
    def test_basic_multimodel(self):
        records = [
            {"type": "ai-title", "aiTitle": "调研 X", "sessionId": "s1"},
            {"type": "user", "timestamp": "2026-06-23T10:00:00Z", "message": {"content": "do X"}},
            {"type": "assistant", "timestamp": "2026-06-23T10:01:00Z",
             "message": {"model": "claude-opus-4-8",
                         "usage": {"input_tokens": 100, "output_tokens": 20,
                                   "cache_creation_input_tokens": 5, "cache_read_input_tokens": 3}}},
            # tool_result echo — NOT a real turn start
            {"type": "user", "timestamp": "2026-06-23T10:05:00Z",
             "message": {"content": [{"type": "tool_result", "content": "r"}]}},
            {"type": "user", "timestamp": "2026-06-23T10:10:00Z", "message": {"content": "again"}},
            {"type": "assistant", "timestamp": "2026-06-23T10:11:00Z",
             "message": {"model": "claude-haiku-4-5",
                         "usage": {"input_tokens": 50, "output_tokens": 10,
                                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}},
            {"type": "ai-title", "aiTitle": "调研 X 完成", "sessionId": "s1"},
        ]
        path = _write_jsonl(records)
        with mock.patch.object(stats, "_find_claude_transcript", return_value=path):
            s = collect_claude_stats("s1")
        self.assertEqual(s.title, "调研 X 完成")  # last ai-title wins
        self.assertEqual(s.input_rounds, 2)  # two real prompts, tool_result excluded
        self.assertEqual(s.source, "ok")
        self.assertEqual(s.duration_minutes, 11)
        self.assertIsNone(s.last_error)
        models = {m.model: m for m in s.per_model}
        self.assertEqual(models["claude-opus-4-8"].input, 100)
        self.assertEqual(models["claude-opus-4-8"].output, 20)
        self.assertEqual(models["claude-opus-4-8"].cache, 8)  # 5 + 3
        self.assertEqual(models["claude-haiku-4-5"].output, 10)

    def test_api_error_detected(self):
        records = [
            {"type": "user", "message": {"content": "x"}},
            {"type": "assistant", "isApiErrorMessage": True,
             "message": {"model": "m", "content": [{"type": "text", "text": "rate limit hit"}]}},
        ]
        path = _write_jsonl(records)
        with mock.patch.object(stats, "_find_claude_transcript", return_value=path):
            s = collect_claude_stats("s1")
        self.assertEqual(s.last_error, "rate limit hit")

    def test_error_cleared_by_later_turn(self):
        records = [
            {"type": "assistant", "isApiErrorMessage": True,
             "message": {"model": "m", "content": [{"type": "text", "text": "err"}]}},
            {"type": "assistant",
             "message": {"model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]
        path = _write_jsonl(records)
        with mock.patch.object(stats, "_find_claude_transcript", return_value=path):
            s = collect_claude_stats("s1")
        self.assertIsNone(s.last_error)  # cleared by the clean turn after

    def test_missing_transcript(self):
        with mock.patch.object(stats, "_find_claude_transcript", return_value=""):
            s = collect_claude_stats("nope")
        self.assertEqual(s.source, "unavailable")

    def test_malformed_lines_dont_crash(self):
        d = tempfile.mkdtemp()
        p = Path(d) / "sess.jsonl"
        p.write_text("not json\n{}\n" + json.dumps(
            {"type": "assistant", "message": {"model": "m",
             "usage": {"input_tokens": 7, "output_tokens": 0}}}))
        with mock.patch.object(stats, "_find_claude_transcript", return_value=str(p)):
            s = collect_claude_stats("s1")
        self.assertEqual(s.per_model[0].input, 7)


class CodexStatsTest(unittest.TestCase):
    def test_rollout_token_split(self):
        records = [
            {"timestamp": "2026-06-23T10:00:00Z", "type": "session_meta", "payload": {"id": "s2"}},
            {"timestamp": "2026-06-23T10:00:30Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "hi"}},
            {"timestamp": "2026-06-23T10:01:00Z", "type": "turn_context",
             "payload": {"model": "gpt-5.5-test", "cwd": "/x"}},
            {"timestamp": "2026-06-23T10:02:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": {
                 "input_tokens": 1000, "cached_input_tokens": 400,
                 "output_tokens": 50, "reasoning_output_tokens": 10}}}},
        ]
        path = _write_jsonl(records)
        with mock.patch.object(stats, "_codex_thread_row", return_value={}), \
             mock.patch.object(stats, "_find_codex_rollout", return_value=path):
            s = collect_codex_stats("s2")
        self.assertEqual(len(s.per_model), 1)
        mt = s.per_model[0]
        self.assertEqual(mt.model, "gpt-5.5-test")
        self.assertEqual(mt.input, 600)   # 1000 - 400 cached
        self.assertEqual(mt.cache, 400)
        self.assertEqual(mt.output, 60)   # 50 + 10 reasoning
        self.assertEqual(s.input_rounds, 1)
        self.assertIsNone(s.last_error)

    def test_rollout_error_event(self):
        records = [
            {"timestamp": "2026-06-23T10:00:00Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "go"}},
            {"timestamp": "2026-06-23T10:01:00Z", "type": "event_msg",
             "payload": {"type": "error", "message": "unauthorized"}},
        ]
        path = _write_jsonl(records)
        with mock.patch.object(stats, "_codex_thread_row", return_value={}), \
             mock.patch.object(stats, "_find_codex_rollout", return_value=path):
            s = collect_codex_stats("s2")
        self.assertEqual(s.last_error, "unauthorized")

    def test_title_from_sqlite_row(self):
        records = [
            {"timestamp": "2026-06-23T10:00:00Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": {
                 "input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1}}}},
        ]
        path = _write_jsonl(records)
        row = {"title": "整理目录\n第二行", "model": "gpt-5.5-test", "rollout_path": path,
               "created_at": None, "updated_at": None}
        with mock.patch.object(stats, "_codex_thread_row", return_value=row), \
             mock.patch.object(stats, "_find_codex_rollout", return_value=path):
            s = collect_codex_stats("s2")
        self.assertEqual(s.title, "整理目录")  # first line only
        self.assertEqual(s.per_model[0].model, "gpt-5.5-test")

    def test_missing_everything(self):
        with mock.patch.object(stats, "_codex_thread_row", return_value={}), \
             mock.patch.object(stats, "_find_codex_rollout", return_value=""):
            s = collect_codex_stats("nope")
        self.assertEqual(s.source, "unavailable")


class DispatchTest(unittest.TestCase):
    def test_unknown_agent(self):
        self.assertEqual(collect_stats("gemini", "s").source, "unavailable")

    def test_empty_session_id(self):
        self.assertEqual(collect_stats("claude", "").source, "unavailable")

    def test_exception_degrades(self):
        with mock.patch.object(stats, "collect_claude_stats", side_effect=RuntimeError("boom")):
            s = collect_stats("claude", "s1")
        self.assertEqual(s.source, "unavailable")


class CodexSqliteTest(unittest.TestCase):
    """_codex_thread_row reads the real threads-table schema (mode=ro)."""

    def test_thread_row_reads_real_sqlite(self):
        import sqlite3
        d = tempfile.mkdtemp()
        db = Path(d) / "state_5.sqlite"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, title TEXT, "
                    "first_user_message TEXT, model TEXT, rollout_path TEXT, "
                    "created_at REAL, updated_at REAL)")
        con.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)",
                    ("sx", "整理目录", "整理我的下载目录全文", "gpt-5.5-test", "/r/x.jsonl", 1.0, 2.0))
        con.commit()
        con.close()
        ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        with mock.patch.object(stats, "_open_codex_db", return_value=ro):
            row = stats._codex_thread_row("sx")
        self.assertEqual(row["title"], "整理目录")
        self.assertEqual(row["model"], "gpt-5.5-test")
        self.assertEqual(row["rollout_path"], "/r/x.jsonl")

    def test_thread_row_missing_db(self):
        with mock.patch.object(stats, "_open_codex_db", return_value=None):
            self.assertEqual(stats._codex_thread_row("sx"), {})


if __name__ == "__main__":
    unittest.main()
