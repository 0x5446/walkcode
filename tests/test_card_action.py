"""Integration tests for _on_card_action / _consume_other_answer over the registry.

These exercise the Feishu card-click handler end-to-end (not just the registry
primitives in test_perm_registry.py):

* A (decision tearing): the first permission click wins; a second click (even a
  different button) does NOT overwrite it and only echoes the decided verdict.
* AskUserQuestion: single-select finalize, multi-question advance, multiSelect
  toggle+submit, and the "Other" thread-reply path — including a stale card click
  arriving AFTER an Other answer already finalized (must be idempotent).
"""

import unittest
from unittest.mock import patch

from walkcode import server
from walkcode.permreg import PermissionRegistry


class _Action:
    def __init__(self, value):
        self.value = value


class _Event:
    def __init__(self, value):
        self.action = _Action(value)


class _Data:
    def __init__(self, value):
        self.event = _Event(value)


def _click(value):
    return server._on_card_action(_Data(value))


class CardActionTests(unittest.TestCase):
    def setUp(self):
        self._orig_registry = server.registry
        server.registry = PermissionRegistry()
        self.addCleanup(lambda: setattr(server, "registry", self._orig_registry))

        # Never spawn the real tmux-fallback thread or touch ~/.claude/settings.json.
        p1 = patch.object(server, "_schedule_tmux_fallback", lambda *a, **k: None)
        p2 = patch.object(server, "_add_permission_rule", lambda *a, **k: None)
        self.cards = []
        p3 = patch.object(server, "_reply_card",
                          lambda mid, card, reply_in_thread=False: self.cards.append(mid) or "cardmsg")
        self.reactions = []
        p4 = patch.object(server, "_add_reaction",
                          lambda mid, emoji: self.reactions.append(emoji))
        for p in (p1, p2, p3, p4):
            p.start()
            self.addCleanup(p.stop)

    def _register(self, tool_name="Bash", tool_input=None, key=("s1", "tu-1"), root="root1"):
        req, _ = server.registry.register_or_get(key)
        server.registry.fill_request(
            req.rid, tool_name=tool_name, tool_input=tool_input or {"command": "ls"},
            tty="tmux1", permission_suggestions=[], feishu_root_msg_id=root,
        )
        return req.rid

    # --- A: permission decision is write-once ----------------------------
    def test_first_click_wins_second_does_not_overwrite(self):
        rid = self._register()
        _click({"rid": rid, "b": "allow"})
        self.assertEqual(server.registry.get(rid).decision["behavior"], "allow")

        resp2 = _click({"rid": rid, "b": "deny"})  # a contradicting second click
        self.assertEqual(server.registry.get(rid).decision["behavior"], "allow")  # unchanged
        self.assertEqual(resp2.toast.type, "info")  # already-decided notice, not success

    def test_both_pollers_read_the_won_decision(self):
        import asyncio
        rid = self._register()
        _click({"rid": rid, "b": "deny"})
        d1 = asyncio.run(server.get_permission_decision(rid))
        d2 = asyncio.run(server.get_permission_decision(rid))
        self.assertEqual(d1["decision"]["behavior"], "deny")
        self.assertEqual(d1["decision"], d2["decision"])

    # --- AskUserQuestion --------------------------------------------------
    def _askq(self, questions):
        return self._register(tool_name="AskUserQuestion",
                              tool_input={"questions": questions}, key=None)

    def test_single_select_finalizes(self):
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}])
        _click({"rid": rid, "action": "select", "answer": "A",
                "question_index": 0, "total_questions": 1})
        dec = server.registry.get(rid).decision
        self.assertEqual(dec["behavior"], "allow")
        self.assertEqual(dec["updatedInput"]["answers"], {"Pick": "A"})

    def test_multi_question_advances_then_finalizes(self):
        qs = [
            {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
        ]
        rid = self._askq(qs)
        _click({"rid": rid, "action": "select", "answer": "A",
                "question_index": 0, "total_questions": 2})
        self.assertIsNone(server.registry.get(rid).decision)  # not done yet
        _click({"rid": rid, "action": "select", "answer": "D",
                "question_index": 1, "total_questions": 2})
        dec = server.registry.get(rid).decision
        self.assertEqual(dec["updatedInput"]["answers"], {"Q1": "A", "Q2": "D"})

    def test_multiselect_toggle_then_submit(self):
        qs = [{"question": "Pick many",
               "options": [{"label": "L1"}, {"label": "L2"}, {"label": "L3"}]}]
        rid = self._askq(qs)
        _click({"rid": rid, "action": "toggle", "option_idx": 1, "question_index": 0})
        _click({"rid": rid, "action": "toggle", "option_idx": 2, "question_index": 0})
        _click({"rid": rid, "action": "submit_multi", "question_index": 0, "total_questions": 1})
        dec = server.registry.get(rid).decision
        # multiSelect answer joins labels with comma in updatedInput
        self.assertEqual(dec["updatedInput"]["answers"], {"Pick many": "L1,L2"})

    def test_other_thread_reply_then_stale_click_is_idempotent(self):
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}]}])
        resp = _click({"rid": rid, "action": "request_other", "question_index": 0})
        self.assertEqual(resp.toast.type, "info")
        self.assertEqual(server.registry.find_awaiting_other("root1"), rid)

        server._consume_other_answer(rid, "my custom text", "msg1")
        dec = server.registry.get(rid).decision
        self.assertEqual(dec["updatedInput"]["answers"], {"Pick": "my custom text"})

        # a stale card click arriving AFTER the Other answer finalized must not
        # change the decided answer (set_decision_once already won).
        _click({"rid": rid, "action": "select", "answer": "A",
                "question_index": 0, "total_questions": 1})
        self.assertEqual(server.registry.get(rid).decision["updatedInput"]["answers"],
                         {"Pick": "my custom text"})


if __name__ == "__main__":
    unittest.main()
