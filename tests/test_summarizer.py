"""Tests for walkcode.summarizer — title generation with graceful degradation."""

import threading
import unittest
from unittest import mock

from walkcode import summarizer


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class SummarizeTitleTest(unittest.TestCase):
    def test_ok(self):
        client = mock.MagicMock()
        client.messages.create.return_value = _Resp("调研网络搜索能力")
        with mock.patch.object(summarizer, "_build_client", return_value=client):
            t = summarizer.summarize_title("去研究 agent 的网络搜索能力来源",
                                           project="p", region="global", sa_path="/sa")
        self.assertEqual(t, "调研网络搜索能力")

    def test_strips_quotes_and_extra_lines(self):
        client = mock.MagicMock()
        client.messages.create.return_value = _Resp("「标题」\n多余解释")
        with mock.patch.object(summarizer, "_build_client", return_value=client):
            t = summarizer.summarize_title("x", project="p", region="r", sa_path="/sa")
        self.assertEqual(t, "标题")

    def test_empty_input_none(self):
        self.assertIsNone(summarizer.summarize_title("   ", project="p", region="r", sa_path="/sa"))

    def test_missing_project_none(self):
        self.assertIsNone(summarizer.summarize_title("x", project="", region="r", sa_path="/sa"))

    def test_failure_degrades_to_none(self):
        with mock.patch.object(summarizer, "_build_client", side_effect=RuntimeError("no dep")):
            t = summarizer.summarize_title("x", project="p", region="r", sa_path="/sa")
        self.assertIsNone(t)

    def test_empty_model_output_none(self):
        client = mock.MagicMock()
        client.messages.create.return_value = _Resp("   ")
        with mock.patch.object(summarizer, "_build_client", return_value=client):
            t = summarizer.summarize_title("x", project="p", region="r", sa_path="/sa")
        self.assertIsNone(t)

    def test_async_callback_runs(self):
        client = mock.MagicMock()
        client.messages.create.return_value = _Resp("T")
        done = threading.Event()
        result = {}

        def cb(title):
            result["t"] = title
            done.set()

        with mock.patch.object(summarizer, "_build_client", return_value=client):
            summarizer.summarize_async(cb, "x", project="p", region="r", sa_path="/sa")
            done.wait(3)
        self.assertEqual(result.get("t"), "T")


if __name__ == "__main__":
    unittest.main()
