"""Regression tests for hook-delivery dedupe.

Background: codex CLI (>=0.135) fires each hook event TWICE — two identical
hook processes launched microseconds apart with the same payload (same
turn_id). Observed in Feishu as a duplicate "✅ Task complete" reply on every
codex turn (one carrying the @mention, one without — the first marked the
session subscribed). Claude never duplicates and carries no turn_id.

walkcode dedupes on the consumer side: a turn ends once → one notification.
The key (a tuple, never a string concat) is (session, type, "turn", turn_id)
when codex supplies a turn_id, else (session, type, "msg", message-hash). A key
is registered ONLY after a confirmed send, so a failed first delivery still lets
codex's duplicate retry (at-least-once preserved). turn_id keys use a long TTL;
the message-hash fallback uses a tiny window so two genuine identical replies are
never collapsed.

These tests pin the key function, the read-only check / mark split, the /hook
end-to-end behaviour (including first-send-failure retry), and cmd_hook's
turn_id forwarding.
"""

import argparse
import asyncio
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from walkcode import server
from walkcode import __main__ as m
from walkcode.config import Config
from walkcode.state import SessionStore


class HookDedupeKeyTests(unittest.TestCase):
    def setUp(self):
        with server._hook_dedupe_lock:
            server._recent_hook_keys.clear()

    def test_turn_id_key_shape(self):
        self.assertEqual(
            server._hook_dedupe_key("s1", "stop", "turn-A", "body"),
            ("s1", "stop", "turn", "turn-A"),
        )

    def test_no_turn_id_uses_message_hash(self):
        k = server._hook_dedupe_key("s1", "stop", "", "body")
        self.assertEqual(k[:3], ("s1", "stop", "msg"))

    def test_empty_session_returns_none(self):
        self.assertIsNone(server._hook_dedupe_key("", "stop", "turn-A", "body"))

    def test_mark_then_already_delivered(self):
        k = server._hook_dedupe_key("s1", "stop", "turn-A", "b")
        self.assertFalse(server._hook_already_delivered(k))
        server._hook_mark_delivered(k)
        self.assertTrue(server._hook_already_delivered(k))

    def test_distinct_turns_independent(self):
        ka = server._hook_dedupe_key("s1", "stop", "turn-A", "b")
        kb = server._hook_dedupe_key("s1", "stop", "turn-B", "b")
        server._hook_mark_delivered(ka)
        self.assertFalse(server._hook_already_delivered(kb))

    def test_no_turn_id_same_message_collapses(self):
        ka = server._hook_dedupe_key("s1", "stop", "", "same")
        kb = server._hook_dedupe_key("s1", "stop", "", "same")
        server._hook_mark_delivered(ka)
        self.assertTrue(server._hook_already_delivered(kb))

    def test_no_turn_id_different_message_independent(self):
        ka = server._hook_dedupe_key("s1", "stop", "", "one")
        kb = server._hook_dedupe_key("s1", "stop", "", "two")
        server._hook_mark_delivered(ka)
        self.assertFalse(server._hook_already_delivered(kb))

    def test_distinct_hook_types_do_not_collide(self):
        ka = server._hook_dedupe_key("s1", "stop", "turn-A", "b")
        kb = server._hook_dedupe_key("s1", "notification", "turn-A", "b")
        server._hook_mark_delivered(ka)
        self.assertFalse(server._hook_already_delivered(kb))

    def test_tuple_key_has_no_concat_collision(self):
        # F4: a string key like f"{sid}|{type}|t:{turn}" would let these collide.
        k1 = server._hook_dedupe_key("s", "stop", "X", "")
        k2 = server._hook_dedupe_key("s|stop|t:X", "stop", "", "")
        server._hook_mark_delivered(k1)
        self.assertFalse(server._hook_already_delivered(k2))

    def test_turn_key_ttl_is_long(self):
        k = server._hook_dedupe_key("s1", "stop", "turn-A", "b")
        with patch.object(server, "time") as mt:
            mt.time.return_value = 1000.0
            server._hook_mark_delivered(k)
            self.assertTrue(server._hook_already_delivered(k))
            mt.time.return_value = 1000.0 + server._HOOK_DEDUPE_TTL_TURN + 1
            self.assertFalse(server._hook_already_delivered(k))

    def test_hash_key_ttl_is_short(self):
        # F2: the message-hash fallback must expire fast so two genuine identical
        # replies seconds apart are NOT collapsed (Claude has no turn_id).
        k = server._hook_dedupe_key("s1", "stop", "", "msg")
        with patch.object(server, "time") as mt:
            mt.time.return_value = 1000.0
            server._hook_mark_delivered(k)
            self.assertTrue(server._hook_already_delivered(k))
            mt.time.return_value = 1000.0 + server._HOOK_DEDUPE_TTL_HASH + 0.5
            self.assertFalse(server._hook_already_delivered(k))


class _Req:
    """Minimal stand-in for a Starlette Request carrying a JSON body."""

    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _stop_body(turn_id="turn-A", message="done", session_id="s1", tty="tmux1"):
    return {
        "type": "stop",
        "tty": tty,
        "cwd": "/tmp/proj",
        "session_id": session_id,
        "turn_id": turn_id,
        "message": message,
        "title": "",
        "matcher": "",
    }


class ReceiveHookDedupeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._orig_config = server.config
        self._orig_store = server.session_store
        # receive_id empty → no @mention branch, keeps assertions focused on count.
        server.config = Config(
            feishu_app_id="x", feishu_app_secret="y",
            feishu_receive_id="", feishu_receive_id_type="open_id",
        )
        server.session_store = SessionStore(Path(self._tmp.name) / "state.json")
        server.session_store.upsert("s1", tty="tmux1", cwd="/tmp/proj", root_msg_id="root1")

        def _restore():
            server.config = self._orig_config
            server.session_store = self._orig_store
        self.addCleanup(_restore)

        with server._hook_dedupe_lock:
            server._recent_hook_keys.clear()
        with server._pending_lock:
            server._session_last_stop.clear()

        self.replies = []          # (message_id, text)
        self._reply_results = []   # successive results: str → sent w/ that id;
                                   # None → transient failure; "__PERMANENT__" → permanent

        def _reply_status(mid, text, reply_in_thread=False):
            self.replies.append((mid, text))
            r = self._reply_results.pop(0) if self._reply_results else "sent-msg"
            if r == "__PERMANENT__":
                return ("permanent", None)
            return ("sent", r) if r else ("transient", None)

        # handler + _flush_redelivery both deliver via _reply_status now.
        p = patch.object(server, "_reply_status", _reply_status)
        p.start()
        self.addCleanup(p.stop)

    def _post(self, body):
        return asyncio.run(server.receive_hook(_Req(body)))

    def test_duplicate_stop_same_turn_sends_one_reply(self):
        r1 = self._post(_stop_body(turn_id="turn-A"))
        r2 = self._post(_stop_body(turn_id="turn-A"))
        self.assertEqual(len(self.replies), 1)
        self.assertFalse(r1.get("deduped", False))
        self.assertTrue(r2.get("deduped"))

    def test_distinct_turns_send_two_replies(self):
        self._post(_stop_body(turn_id="turn-A"))
        self._post(_stop_body(turn_id="turn-B"))
        self.assertEqual(len(self.replies), 2)

    def test_stop_refreshes_health_card_once_after_successful_delivery(self):
        calls = []
        with patch.object(
            server, "_refresh_health_card_for_event",
            lambda sid, **kw: calls.append((sid, kw)) or False,
        ):
            self._post(_stop_body(turn_id="turn-A"))
            self._post(_stop_body(turn_id="turn-A"))

        self.assertEqual(calls, [(
            "s1",
            {"summarize": True, "freeze_if_terminal": True},
        )])

    def test_claude_style_no_turn_id_dedupes_on_message(self):
        self._post(_stop_body(turn_id="", message="identical"))
        r2 = self._post(_stop_body(turn_id="", message="identical"))
        self.assertEqual(len(self.replies), 1)
        self.assertTrue(r2.get("deduped"))

    def test_first_send_failure_lets_duplicate_retry(self):
        # F1 (Critical): first _reply fails → key NOT registered → codex's
        # duplicate must NOT be deduped; it retries and succeeds.
        self._reply_results = [None, "sent-msg"]  # 1st fails, 2nd ok
        r1 = self._post(_stop_body(turn_id="turn-A"))
        r2 = self._post(_stop_body(turn_id="turn-A"))
        self.assertFalse(r1.get("ok"))               # first delivery failed
        self.assertFalse(r2.get("deduped", False))   # second NOT swallowed
        self.assertTrue(r2.get("ok"))                # second delivered
        self.assertEqual(len(self.replies), 2)       # both attempts hit _reply

    def test_no_turn_id_resend_after_window_is_allowed(self):
        # F2: same message beyond the short hash TTL is a genuine new reply.
        with patch.object(server, "time") as mt:
            mt.time.return_value = 5000.0
            self._post(_stop_body(turn_id="", message="done"))
            mt.time.return_value = 5000.0 + server._HOOK_DEDUPE_TTL_HASH + 1
            r2 = self._post(_stop_body(turn_id="", message="done"))
        self.assertFalse(r2.get("deduped", False))
        self.assertEqual(len(self.replies), 2)

    def test_null_message_does_not_crash(self):
        # F5: explicit JSON null for message must not raise.
        body = _stop_body(turn_id="turn-A")
        body["message"] = None
        r = self._post(body)
        self.assertTrue(r.get("ok"))

    def test_user_initiated_send_failure_lets_duplicate_retry(self):
        # ISSUE_1: the new-session (user-initiated) branch must also register
        # dedupe only on a confirmed send. First content reply fails → not deduped
        # → the duplicate retries via existing-session (the root was created).
        self._reply_results = [None, "ok-2"]  # 1st content reply fails, 2nd ok
        with patch.object(server, "_send", lambda text: "root-new"):
            r1 = self._post(_stop_body(turn_id="turn-A", session_id="s-new"))
            r2 = self._post(_stop_body(turn_id="turn-A", session_id="s-new"))
        self.assertFalse(r1.get("ok"))               # first delivery failed
        self.assertFalse(r2.get("deduped", False))   # second NOT swallowed
        self.assertEqual(len(self.replies), 2)

    def test_pending_reply_success_replies_into_existing_thread(self):
        # E happy path: a Feishu-initiated (pending) session replies into the
        # pre-created thread root and registers dedupe once.
        server.session_store.add_pending("tmux-pending", "pending-root")
        r = self._post(_stop_body(
            turn_id="turn-A", session_id="s-pending", tty="tmux-pending"))
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("msg_id"), "pending-root")
        self.assertEqual(self.replies[0][0], "pending-root")  # replied to the root
        # the session now owns the root for any duplicate to retry against
        self.assertEqual(server.session_store.get("s-pending").root_msg_id, "pending-root")

    def test_pending_reply_failure_does_not_fall_through_to_new_thread(self):
        # E (Critical): if the reply to the pending root fails, we must NOT fall
        # through and _send a brand-new thread — the first reply would land in
        # the wrong place. Return ok:False; the session keeps its root_msg_id so
        # codex's duplicate retries into the SAME thread via existing-session.
        server.session_store.add_pending("tmux-pending", "pending-root")
        self._reply_results = [None, "ok-2"]  # 1st (pending) fails, 2nd (existing) ok
        sends = []
        with patch.object(server, "_send",
                          lambda text: sends.append(text) or "should-not-be-used"):
            r1 = self._post(_stop_body(
                turn_id="turn-A", session_id="s-pending", tty="tmux-pending"))
            r2 = self._post(_stop_body(
                turn_id="turn-A", session_id="s-pending", tty="tmux-pending"))
        self.assertFalse(r1.get("ok"))               # pending reply failed
        self.assertEqual(sends, [])                  # never created a new thread
        self.assertFalse(r2.get("deduped", False))   # duplicate NOT swallowed
        self.assertTrue(r2.get("ok"))                # retried via existing-session
        self.assertEqual(r2.get("thread"), "pending-root")  # same thread, right place
        self.assertEqual(len(self.replies), 2)

    def test_pending_hook_establishes_launch_cwd_not_runtime(self):
        # Feishu-initiated: pending carries the launch cwd. The hook's runtime cwd
        # (_stop_body uses /tmp/proj, as if the agent had cd'd) must NOT become the
        # session's launch cwd — otherwise resume would later cd into the wrong dir.
        server.session_store.add_pending("tmux-pending", "pending-root", cwd="/launch")
        self._post(_stop_body(turn_id="t1", session_id="s-pending", tty="tmux-pending"))
        self.assertEqual(server.session_store.get("s-pending").cwd, "/launch")

    def test_pending_without_cwd_falls_back_to_default_not_runtime(self):
        # Old state.json upgraded in-flight: the pending record predates the cwd
        # field. Must fall back to config.default_cwd (the Feishu launch dir),
        # never this hook's runtime cwd.
        server.session_store.add_pending("tmux-pending", "pending-root")  # no cwd
        self._post(_stop_body(turn_id="t1", session_id="s-pending", tty="tmux-pending"))
        sess = server.session_store.get("s-pending")
        self.assertEqual(sess.cwd, server.config.default_cwd)
        self.assertNotEqual(sess.cwd, "/tmp/proj")  # not the runtime cwd

    def test_failed_stop_is_stashed_and_redelivered_next_hook(self):
        # The silent-drop bug: a Stop whose Feishu reply fails (network blip) must
        # be stashed, then redelivered ahead of the next turn's reply — never
        # dropped while the agent believes it answered.
        self._reply_results = [None, "redeliver", "current"]  # turn-A fails; next hook flush+current ok
        r1 = self._post(_stop_body(turn_id="A", message="ANSWER-A"))
        self.assertFalse(r1.get("ok"))
        self.assertEqual(len(server.session_store.get("s1").pending_redelivery), 1)

        r2 = self._post(_stop_body(turn_id="B", message="ANSWER-B"))
        self.assertTrue(r2.get("ok"))
        # 3 sends total: the failed turn-A, its redelivery, then turn-B.
        self.assertEqual(len(self.replies), 3)
        self.assertIn("ANSWER-A", self.replies[1][1])   # redelivered first
        self.assertIn("ANSWER-B", self.replies[2][1])   # then the new turn
        self.assertEqual(server.session_store.get("s1").pending_redelivery, [])

    def test_codex_duplicate_failure_stashed_once_not_twice(self):
        # codex fires Stop twice per turn. If both fail during an outage, the same
        # turn must be stashed once, so redelivery sends it a single time.
        key = ("s1", "stop", "turn", "A")
        server.session_store.add_redelivery("s1", "ANSWER-A", key)
        server.session_store.add_redelivery("s1", "ANSWER-A", key)  # codex's duplicate
        self.assertEqual(len(server.session_store.get("s1").pending_redelivery), 1)

    def test_redelivered_turn_not_resent_when_duplicate_arrives(self):
        # turn-A fails (stashed). codex's duplicate turn-A then arrives: the next
        # hook redelivers turn-A AND the duplicate must NOT re-send it (dedupe via
        # the redelivered key), so Feishu sees turn-A exactly once.
        self._reply_results = [None, "redeliver"]  # turn-A fails; duplicate's flush ok
        self._post(_stop_body(turn_id="A", message="ANSWER-A"))       # fails, stashed
        r2 = self._post(_stop_body(turn_id="A", message="ANSWER-A"))  # duplicate
        self.assertTrue(r2.get("redelivered"))
        self.assertFalse(r2.get("deduped", False))
        self.assertEqual(len(self.replies), 2)  # failed send + one redelivery, no third

    def test_user_initiated_reply_failure_is_stashed(self):
        # B2: a brand-new session (codex started outside Feishu, no pending root).
        # _send creates the root but the content reply fails → must stash, because
        # Claude has no duplicate Stop to retry it.
        self._reply_results = [None]  # content reply transient-fails
        with patch.object(server, "_send", lambda text: "root-ui"):
            r = self._post(_stop_body(turn_id="A", session_id="s-ui", tty="tmux-ui"))
        self.assertFalse(r.get("ok"))
        self.assertEqual(len(server.session_store.get("s-ui").pending_redelivery), 1)

    def test_permanent_failure_is_not_stashed(self):
        # A permanent send error (bad payload) must NOT be stashed — retrying the
        # same payload is futile and would wedge the queue.
        self._reply_results = ["__PERMANENT__"]
        r = self._post(_stop_body(turn_id="A", message="X"))
        self.assertFalse(r.get("ok"))
        self.assertEqual(server.session_store.get("s1").pending_redelivery, [])

    def test_blocked_backlog_holds_current_for_order(self):
        # If the backlog can't drain (still failing), the current turn must queue
        # BEHIND it, not jump ahead — preserving order.
        server.session_store.add_redelivery("s1", "OLD", ("s1", "stop", "turn", "Z"))
        self._reply_results = [None]  # flush of OLD transient-fails
        r = self._post(_stop_body(turn_id="NOW", message="NEW"))
        self.assertFalse(r.get("ok"))
        texts = [e["text"] for e in server.session_store.get("s1").pending_redelivery]
        self.assertEqual(texts[0], "OLD")            # old reply still first
        self.assertIn("NEW", texts[-1])              # new turn queued behind it
        self.assertEqual(len(self.replies), 1)       # only the failed flush; current NOT sent

    def test_flush_drops_permanent_and_keeps_draining(self):
        # A poison stashed reply (permanent) is dropped, not re-stashed, so it can't
        # block the rest of the queue; the good one and the current turn still send.
        server.session_store.add_redelivery("s1", "POISON", ("s1", "stop", "turn", "P"))
        server.session_store.add_redelivery("s1", "GOOD", ("s1", "stop", "turn", "G"))
        self._reply_results = ["__PERMANENT__", "ok-good", "ok-cur"]
        r = self._post(_stop_body(turn_id="NOW", message="NEW"))
        self.assertTrue(r.get("ok"))
        self.assertEqual(server.session_store.get("s1").pending_redelivery, [])  # fully drained
        texts = [t for _, t in self.replies]
        self.assertTrue(any("GOOD" in t for t in texts))
        self.assertTrue(any("NEW" in t for t in texts))

    def test_duplicate_current_returned_even_if_backlog_blocked(self):
        # Backlog [A(sent), B(transient-fail)] and the current hook IS A's duplicate.
        # Must return redelivered (A already sent), NOT re-queue A behind B — which
        # would duplicate A and reorder it after B.
        server.session_store.add_redelivery("s1", "A", ("s1", "stop", "turn", "A"))
        server.session_store.add_redelivery("s1", "B", ("s1", "stop", "turn", "B"))
        self._reply_results = ["okA", None]  # A flush sent, B flush transient-fails
        r = self._post(_stop_body(turn_id="A", message="A"))  # current = A's duplicate
        self.assertTrue(r.get("redelivered"))
        pend = [e["text"] for e in server.session_store.get("s1").pending_redelivery]
        self.assertEqual(pend, ["B"])  # only B re-queued; A not re-added


class RetrySendTests(unittest.TestCase):
    """_send_with_status: retry transient network errors, classify permanent
    business errors, re-raise programming errors."""

    class _Resp:
        def __init__(self, ok, mid=None, code=0, msg=""):
            self._ok = ok
            self.code = code
            self.msg = msg

            class _D:
                message_id = mid

            self.data = _D

        def success(self):
            return self._ok

    def test_retries_network_exception_then_succeeds(self):
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("dns blip")
            return self._Resp(True, mid="mid")

        with patch.object(server, "time"):  # no real sleep
            self.assertEqual(server._send_with_status(_flaky, "T"), ("sent", "mid"))
        self.assertEqual(calls["n"], 3)

    def test_does_not_retry_business_error(self):
        calls = {"n": 0}

        def _f():
            calls["n"] += 1
            return self._Resp(False, code=230001, msg="bad param")

        with patch.object(server, "time"):
            self.assertEqual(server._send_with_status(_f, "T"), ("permanent", None))
        self.assertEqual(calls["n"], 1)  # permanent error (230001 whitelisted): no retry

    def test_retries_non_whitelisted_api_code_as_transient(self):
        # A non-whitelisted API failure (rate limit / 5xx / unknown) is transient:
        # retried, then reported transient (→ redelivered), NOT dropped as permanent.
        calls = {"n": 0}

        def _rate_limited():
            calls["n"] += 1
            return self._Resp(False, code=99991400, msg="rate limited")

        with patch.object(server, "time"):
            self.assertEqual(server._send_with_status(_rate_limited, "T"), ("transient", None))
        self.assertEqual(calls["n"], server._SEND_RETRY_ATTEMPTS)

    def test_gives_up_after_attempts(self):
        calls = {"n": 0}

        def _always_raise():
            calls["n"] += 1
            raise ConnectionError("down")

        with patch.object(server, "time"):
            self.assertEqual(server._send_with_status(_always_raise, "T"), ("transient", None))
        self.assertEqual(calls["n"], server._SEND_RETRY_ATTEMPTS)

    def test_programming_error_is_reraised(self):
        # A bug in request construction (TypeError/AttributeError/…) must surface,
        # not be swallowed as a transient network failure.
        def _bug():
            raise TypeError("our bug")
        with patch.object(server, "time"), self.assertRaises(TypeError):
            server._send_with_status(_bug, "T")


class CmdHookTurnIdForwardingTests(unittest.TestCase):
    """cmd_hook must forward turn_id into the POST body (tests ISSUE_2): the
    server-side tests inject turn_id directly, so a broken cmd_hook would slip
    through them."""

    def _run(self, hook_type, hook_data):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())

            class _Resp:
                def read(self_inner):
                    return b"{}"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _Resp()

        with patch.object(m.sys, "stdin", io.StringIO(json.dumps(hook_data))), \
             patch.object(m, "detect_tmux_session", lambda: "tmux1"), \
             patch.object(m, "owner_check", lambda: (True, "owner")), \
             patch.object(m.urllib.request, "urlopen", fake_urlopen), \
             patch.dict(os.environ, {"WALKCODE_PORT": "3999"}, clear=False):
            m.cmd_hook(argparse.Namespace(hook_type=hook_type))
        return captured

    def test_stop_forwards_turn_id(self):
        cap = self._run("stop", {
            "session_id": "s1", "turn_id": "turn-XYZ",
            "last_assistant_message": "done", "cwd": "/tmp",
        })
        self.assertEqual(cap["body"]["turn_id"], "turn-XYZ")

    def test_claude_stop_has_empty_turn_id(self):
        cap = self._run("stop", {
            "session_id": "s1", "last_assistant_message": "done", "cwd": "/tmp",
        })
        self.assertEqual(cap["body"]["turn_id"], "")


class CmdHookStopMessageTests(unittest.TestCase):
    """cmd_hook's Stop branch must forward the WHOLE turn (not just the last
    segment), and degrade safely. The helper-function unit tests don't prove the
    wiring — a broken branch (wrong reader, dropped transcript_path, codex env not
    honored, fallback missing) would slip past them."""

    def _run(self, hook_data, env=None):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())

            class _Resp:
                def read(self_inner):
                    return b"{}"

                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *a):
                    return False

            return _Resp()

        envd = {"WALKCODE_PORT": "3999"}
        envd.update(env or {})
        with patch.object(m.sys, "stdin", io.StringIO(json.dumps(hook_data))), \
             patch.object(m, "detect_tmux_session", lambda: "tmux1"), \
             patch.object(m, "owner_check", lambda: (True, "owner")), \
             patch.object(m.urllib.request, "urlopen", fake_urlopen), \
             patch.dict(os.environ, envd, clear=False):
            m.cmd_hook(argparse.Namespace(hook_type="stop"))
        return captured

    def _transcript(self, d, records):
        p = Path(d) / "t.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return str(p)

    def test_claude_forwards_whole_turn(self):
        with TemporaryDirectory() as d:
            tp = self._transcript(d, [
                {"type": "user", "message": {"content": "do it"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "step one"},
                    {"type": "tool_use", "name": "Bash", "input": {}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "final answer"}]}},
            ])
            cap = self._run({
                "session_id": "s1", "cwd": "/tmp", "transcript_path": tp,
                "last_assistant_message": "final answer",
            })
            self.assertEqual(cap["body"]["message"], "step one\n\nfinal answer")

    def test_claude_falls_back_to_last_assistant_message(self):
        # No transcript_path → reader returns "" → fall back to the single segment.
        cap = self._run({
            "session_id": "s1", "cwd": "/tmp",
            "last_assistant_message": "only this",
        })
        self.assertEqual(cap["body"]["message"], "only this")

    def test_appends_final_segment_when_transcript_lags(self):
        # Transcript missing the final segment (e.g. still-flushing) → the
        # authoritative last_assistant_message is appended so the conclusion lands.
        with TemporaryDirectory() as d:
            tp = self._transcript(d, [
                {"type": "user", "message": {"content": "q"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "partial so far"}]}},
            ])
            cap = self._run({
                "session_id": "s1", "cwd": "/tmp", "transcript_path": tp,
                "last_assistant_message": "the real conclusion",
            })
            self.assertEqual(
                cap["body"]["message"], "partial so far\n\nthe real conclusion")

    def test_no_duplicate_when_transcript_already_has_final(self):
        with TemporaryDirectory() as d:
            tp = self._transcript(d, [
                {"type": "user", "message": {"content": "q"}},
                {"type": "assistant", "message": {"content": [
                    {"type": "text", "text": "the answer"}]}},
            ])
            cap = self._run({
                "session_id": "s1", "cwd": "/tmp", "transcript_path": tp,
                "last_assistant_message": "the answer",
            })
            self.assertEqual(cap["body"]["message"], "the answer")

    def test_codex_branch_uses_rollout_reader(self):
        with patch.object(m, "_read_codex_turn_messages",
                          return_value="codex seg 1\n\ncodex seg 2") as reader:
            cap = self._run(
                {"session_id": "019ed-sid", "cwd": "/tmp",
                 "turn_id": "t1", "last_assistant_message": "codex seg 2"},
                env={"WALKCODE_AGENT": "codex"},
            )
        reader.assert_called_once()
        self.assertEqual(cap["body"]["message"], "codex seg 1\n\ncodex seg 2")


if __name__ == "__main__":
    unittest.main()
