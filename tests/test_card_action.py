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

        # Never touch ~/.claude/settings.json during the test.
        p2 = patch.object(server, "_add_permission_rule", lambda *a, **k: None)
        self.cards = []
        p3 = patch.object(server, "_reply_card",
                          lambda mid, card, reply_in_thread=False: self.cards.append(mid) or "cardmsg")
        self.reactions = []
        p4 = patch.object(server, "_add_reaction",
                          lambda mid, emoji: self.reactions.append(emoji))
        for p in (p2, p3, p4):
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

    # --- E: stale / invalidated clicks are gated, never injected ----------
    def test_invalidated_click_rejected(self):
        # PostToolUse invalidated the card (TUI already handled it) → a late click
        # writes no decision and is told it's expired.
        rid = self._register()
        server.registry.fill_request(rid, session_id="sess-x")
        server.registry.invalidate_session("sess-x")
        resp = _click({"rid": rid, "b": "allow"})
        self.assertIsNone(server.registry.get(rid).decision)
        self.assertEqual(resp.toast.type, "info")
        # PostToolUse invalidation = the tool ran (approved) → green resolved card
        self.assertEqual(resp.card.data["header"]["template"], "green")

    def test_stale_poll_click_rejected(self):
        # The hook stopped polling long ago (TUI deny/Esc killed it) → stale click,
        # no decision written, no fallback injection (there is none anymore).
        rid = self._register()
        server.registry.get(rid).created_at -= server._PERM_POLL_STALE + 10
        resp = _click({"rid": rid, "b": "allow"})
        self.assertIsNone(server.registry.get(rid).decision)
        self.assertEqual(resp.toast.type, "info")
        # TUI deny/Esc (NOT approval) → neutral grey; must never show green "allowed" (A)
        self.assertEqual(resp.card.data["header"]["template"], "grey")

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

    def test_askq_click_rejected_after_invalidate(self):
        # AskUserQuestion clicks are gated too (top-of-handler gate): after PostToolUse
        # invalidates the session, a late answer writes no decision.
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}])
        server.registry.fill_request(rid, session_id="s1")
        server.registry.invalidate_session("s1")
        resp = _click({"rid": rid, "action": "select", "answer": "A",
                       "question_index": 0, "total_questions": 1})
        self.assertIsNone(server.registry.get(rid).decision)
        self.assertEqual(resp.toast.type, "info")

    def test_single_select_finalizes(self):
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}])
        _click({"rid": rid, "action": "select", "answer": "A",
                "question_index": 0, "total_questions": 1})
        dec = server.registry.get(rid).decision
        self.assertEqual(dec["behavior"], "allow")
        self.assertEqual(dec["updatedInput"]["answers"], {"Pick": "A"})

    def test_finalize_card_shows_answer(self):
        # The Feishu finalize card is green and lists the chosen answer (not just
        # "all answered").
        rid = self._askq([{"question": "Pick", "header": "Choice",
                           "options": [{"label": "A"}, {"label": "B"}]}])
        resp = _click({"rid": rid, "action": "select", "answer": "A",
                       "question_index": 0, "total_questions": 1})
        card = resp.card.data
        self.assertEqual(card["header"]["template"], "green")
        text = card["elements"][0]["text"]["content"]
        self.assertIn("A", text)       # chosen label
        self.assertIn("Choice", text)  # question heading

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

    def test_askq_interactions_refresh_wait_timeout_timer(self):
        waiting = []
        refreshes = []
        qs = [
            {"question": "Q1", "options": [{"label": "A"}, {"label": "B"}]},
            {"question": "Q2", "options": [{"label": "C"}, {"label": "D"}]},
        ]

        with patch.object(server, "_mark_session_waiting",
                          lambda sid, reason: waiting.append((sid, reason))), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or False):
            rid_toggle = self._askq([{"question": "Pick many",
                                      "options": [{"label": "L1"}, {"label": "L2"}]}])
            server.registry.fill_request(rid_toggle, session_id="s-wait")
            _click({"rid": rid_toggle, "action": "toggle", "option_idx": 1, "question_index": 0})

            rid_other = self._askq([{"question": "Pick", "options": [{"label": "A"}]}])
            server.registry.fill_request(rid_other, session_id="s-wait")
            _click({"rid": rid_other, "action": "request_other", "question_index": 0})

            rid_next = self._askq(qs)
            server.registry.fill_request(rid_next, session_id="s-wait")
            _click({"rid": rid_next, "action": "select", "answer": "A",
                    "question_index": 0, "total_questions": 2})

        self.assertEqual(waiting, [
            ("s-wait", "ask_user_question"),
            ("s-wait", "ask_user_question"),
            ("s-wait", "ask_user_question"),
        ])
        self.assertEqual([sid for sid, _ in refreshes], ["s-wait", "s-wait", "s-wait"])

    def test_other_final_answer_marks_session_running(self):
        busy = []
        refreshes = []
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}]}])
        _click({"rid": rid, "action": "request_other", "question_index": 0})
        server.registry.fill_request(rid, session_id="s1")

        with patch.object(server, "_mark_session_busy", lambda sid: busy.append(sid)), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or False):
            server._consume_other_answer(rid, "my custom text", "msg1")

        self.assertEqual(busy, ["s1"])
        self.assertEqual([sid for sid, _ in refreshes], ["s1"])

    def test_finalize_loser_does_not_show_its_answer(self):
        # A later finalize that loses write-once (double-click / race with PostToolUse)
        # must NOT render its own answer as if it took effect; the first decision stands
        # and a neutral resolved card + info toast is shown (deep-review B).
        qs = [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}]
        rid = self._askq(qs)
        _click({"rid": rid, "action": "select", "answer": "A",
                "question_index": 0, "total_questions": 1})
        resp2 = server.P2CardActionTriggerResponse()
        server._finalize_askuser_answer(resp2, rid, qs, 0, 1, "B")  # losing late click
        self.assertEqual(server.registry.get(rid).decision["updatedInput"]["answers"],
                         {"Pick": "A"})  # first answer kept
        self.assertNotIn("B", str(resp2.card.data))  # loser's answer never shown
        self.assertEqual(resp2.card.data["header"]["template"], "grey")  # neutral, not green
        self.assertEqual(resp2.toast.type, "info")

    def test_other_wait_cleared_on_invalidation(self):
        # PostToolUse invalidation clears awaiting_other so a later thread reply isn't
        # consumed as the (now-settled) question's answer (deep-review ISSUE_3).
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}]}])
        _click({"rid": rid, "action": "request_other", "question_index": 0})
        server.registry.fill_request(rid, session_id="sx")
        server.registry.invalidate_session("sx")
        self.assertIsNone(server.registry.find_awaiting_other("root1"))
        server._consume_other_answer(rid, "late text", "msg1")
        self.assertIsNone(server.registry.get(rid).decision)  # late Other did not take effect

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

    def test_timeout_decision_makes_askq_click_stale_without_refreshing_wait(self):
        rid = self._askq([{"question": "Pick", "options": [{"label": "A"}]}])
        server.registry.fill_request(rid, session_id="s1")
        server.registry.timeout_session("s1")
        waiting = []
        refreshes = []

        with patch.object(server, "_mark_session_waiting",
                          lambda sid, reason: waiting.append((sid, reason))), \
             patch.object(server, "_refresh_health_card_for_event",
                          lambda sid, **kw: refreshes.append((sid, kw)) or False):
            resp = _click({"rid": rid, "action": "toggle",
                           "option_idx": 1, "question_index": 0})

        self.assertEqual(waiting, [])
        self.assertEqual(refreshes, [])
        self.assertEqual(resp.card.data["header"]["template"], "grey")
        self.assertEqual(resp.toast.type, "info")


if __name__ == "__main__":
    unittest.main()
