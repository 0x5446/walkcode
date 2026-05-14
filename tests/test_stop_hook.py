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

from walkcode.__main__ import _read_last_assistant_text


def _assistant(text_blocks=None, tool_uses=None):
    content = []
    for t in text_blocks or []:
        content.append({"type": "text", "text": t})
    for name in tool_uses or []:
        content.append({"type": "tool_use", "name": name, "input": {}})
    return {"type": "assistant", "message": {"content": content}}


def _user(text):
    return {"type": "user", "message": {"content": text}}


def _write_jsonl(path: Path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


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


if __name__ == "__main__":
    unittest.main()
