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
        self._reply_results = []   # successive return values; default "sent-msg"

        def _reply(mid, text, reply_in_thread=False):
            self.replies.append((mid, text))
            if self._reply_results:
                return self._reply_results.pop(0)
            return "sent-msg"

        p = patch.object(server, "_reply", _reply)
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


if __name__ == "__main__":
    unittest.main()
