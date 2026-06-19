"""Unit tests for AskUserQuestion card rendering (_build_askuserquestion_card)
and lark_md escaping.

test_card_action.py only exercises clicks on synthetic payloads; these assert
the card STRUCTURE itself and that interaction callback fields survive both the
description layout (per-option div + button) and the no-description fallback
(one row of buttons).
"""

import unittest

from walkcode import server


class EscapeLarkMdTests(unittest.TestCase):
    def test_escapes_link_and_mention_chars(self):
        self.assertEqual(
            server._escape_lark_md("[x](http://e) <at id=1>"),
            "\\[x\\]\\(http://e\\) \\<at id=1\\>",
        )

    def test_empty_passthrough(self):
        self.assertEqual(server._escape_lark_md(""), "")


class CardNoDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.q = [{
            "question": "继续？",
            "multiSelect": False,
            "options": [{"label": "是"}, {"label": "否"}],
        }]

    def test_fallback_single_row(self):
        card = server._build_askuserquestion_card("rid", self.q, 0)
        tags = [e["tag"] for e in card["elements"]]
        # compact layout: one action row of option buttons, hr, control row
        self.assertEqual(tags, ["action", "hr", "action"])
        opt_btns = card["elements"][0]["actions"]
        self.assertEqual([b["text"]["content"] for b in opt_btns], ["是", "否"])

    def test_callback_fields_preserved(self):
        card = server._build_askuserquestion_card("rid", self.q, 0)
        b0 = card["elements"][0]["actions"][0]
        self.assertEqual(b0["value"], {
            "rid": "rid", "action": "select", "answer": "是",
            "option_idx": 1, "question_index": 0, "total_questions": 1,
        })
        self.assertEqual(b0["text"]["tag"], "plain_text")


class CardWithDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.q = [{
            "question": "选方案",
            "multiSelect": False,
            "options": [
                {"label": "A", "description": "方案A说明"},
                {"label": "B", "description": "方案B说明"},
            ],
        }]

    def test_per_option_div_plus_button(self):
        card = server._build_askuserquestion_card("rid", self.q, 0)
        els = card["elements"]
        self.assertEqual([e["tag"] for e in els],
                         ["div", "action", "div", "action", "hr", "action"])
        self.assertEqual(els[0]["text"]["tag"], "lark_md")
        self.assertIn("方案A说明", els[0]["text"]["content"])
        self.assertIn("**A**", els[0]["text"]["content"])

    def test_button_keeps_label_plain_text(self):
        card = server._build_askuserquestion_card("rid", self.q, 0)
        b0 = card["elements"][1]["actions"][0]
        self.assertEqual(b0["text"], {"tag": "plain_text", "content": "A"})
        self.assertEqual(b0["value"]["action"], "select")
        self.assertEqual(b0["value"]["option_idx"], 1)
        self.assertEqual(b0["value"]["answer"], "A")

    def test_description_is_escaped_but_answer_is_raw(self):
        q = [{"question": "q", "options": [
            {"label": "ok", "description": "点 [这里](http://evil) <at id=1>"}]}]
        card = server._build_askuserquestion_card("rid", q, 0)
        content = card["elements"][0]["text"]["content"]
        self.assertNotIn("[这里](http://evil)", content)
        self.assertIn("\\[这里\\]\\(http://evil\\)", content)
        # button still submits the raw, unescaped label
        self.assertEqual(card["elements"][1]["actions"][0]["value"]["answer"], "ok")

    def test_inline_format_chars_escaped(self):
        q = [{"question": "q", "options": [
            {"label": "ok", "description": "`code` **粗** _斜_ ~删~ # h"}]}]
        card = server._build_askuserquestion_card("rid", q, 0)
        content = card["elements"][0]["text"]["content"]
        self.assertNotIn("`code`", content)
        self.assertNotIn("**粗**", content)
        self.assertIn("\\`code\\`", content)
        self.assertIn("\\*\\*", content)

    def test_label_newline_collapsed_keeps_bold(self):
        q = [{"question": "q", "options": [
            {"label": "line1\nline2", "description": "d"}]}]
        card = server._build_askuserquestion_card("rid", q, 0)
        content = card["elements"][0]["text"]["content"]
        self.assertEqual(content.split("\n", 1)[0], "**line1 line2**")


class CardMultiSelectTests(unittest.TestCase):
    def setUp(self):
        self.q = [{
            "question": "多选",
            "multiSelect": True,
            "options": [
                {"label": "A", "description": "da"},
                {"label": "B", "description": "db"},
            ],
        }]

    def test_selected_marked_and_submit_present(self):
        card = server._build_askuserquestion_card("rid", self.q, 0, selected_indices=[2])
        # option B (idx 2) is selected: ✓ prefix + toggle action + primary
        b_b = card["elements"][3]["actions"][0]
        self.assertEqual(b_b["text"]["content"], "✓ B")
        self.assertEqual(b_b["type"], "primary")
        self.assertEqual(b_b["value"]["action"], "toggle")
        # control row carries submit + other
        ctrl = [b["text"]["content"] for b in card["elements"][-1]["actions"]]
        self.assertTrue(any("提交所选" in c for c in ctrl))
        self.assertTrue(any("其他" in c for c in ctrl))


if __name__ == "__main__":
    unittest.main()
