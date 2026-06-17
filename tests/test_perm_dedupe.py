"""Regression tests for permission-request dedupe (codex 0.135 double-fire).

codex 0.135 double-fires PreToolUse: two processes POST /hook/permission for ONE
tool call → two cards, two long-polls. walkcode dedupes by tool_use_id (NOT
turn_id — a turn holds dozens of tool calls): the duplicate reuses the first
request_id (one card), and both pollers read the SAME decision (read-not-pop +
lazy GC). AskUserQuestion is Claude-only (no tool_use_id) → key None → never
deduped, so its multi-step / Other flow is untouched.

State now lives in a PermissionRegistry (see test_perm_registry.py for the
state-machine unit + concurrency tests); these tests pin the /hook/permission and
/decision endpoints end-to-end: single-card dedupe, no over-merging by tool, the
no-turn degradation, two-poller decision sharing, lazy GC (grace + TTL backstop +
AskUserQuestion exemption), tmux-fallback gating, card-send-failure retry, and
cmd-side tool_use_id/turn_id forwarding.
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
from walkcode.permreg import CardStatus, PermissionRegistry
from walkcode.state import SessionStore


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class PermDedupeKeyTests(unittest.TestCase):
    def test_key_shape(self):
        self.assertEqual(server._perm_dedupe_key("s1", "tu-1"), ("s1", "tu-1"))

    def test_missing_tool_use_id_returns_none(self):
        self.assertIsNone(server._perm_dedupe_key("s1", ""))

    def test_missing_session_returns_none(self):
        self.assertIsNone(server._perm_dedupe_key("", "tu-1"))


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def _perm_body(tool_use_id="tu-1", session_id="s1", tty="tmux1", tool_name="Bash"):
    return {
        "tty": tty,
        "cwd": "/tmp/proj",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": {"command": "ls"},
        "tool_use_id": tool_use_id,
        "hook_data_full": {"tool_use_id": tool_use_id, "permission_mode": "default"},
    }


class ReceivePermDedupeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_config = server.config
        self._orig_store = server.session_store
        self._orig_registry = server.registry
        server.config = Config(
            feishu_app_id="x", feishu_app_secret="y",
            feishu_receive_id="", feishu_receive_id_type="open_id",
        )
        server.session_store = SessionStore(Path(self._tmp.name) / "state.json")
        # existing thread → receive replies card via _reply_card
        server.session_store.upsert("s1", tty="tmux1", cwd="/tmp/proj", root_msg_id="root1")

        # Fresh registry on a controllable clock so GC timing is deterministic.
        self.clock = _Clock(1000.0)
        server.registry = PermissionRegistry(now=self.clock)

        def _restore():
            server.config = self._orig_config
            server.session_store = self._orig_store
            server.registry = self._orig_registry
        self.addCleanup(_restore)

        self.cards = []
        p1 = patch.object(server, "_reply_card",
                          lambda mid, card, reply_in_thread=False: self.cards.append(("reply", mid)) or "cardmsg")
        p2 = patch.object(server, "_send_card",
                          lambda card: self.cards.append(("send", None)) or "cardmsg")
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def _post(self, body):
        return asyncio.run(server.receive_permission_hook(_Req(body)))

    def test_duplicate_reuses_rid_one_card(self):
        r1 = self._post(_perm_body(tool_use_id="tu-1"))
        r2 = self._post(_perm_body(tool_use_id="tu-1"))
        self.assertEqual(r1["request_id"], r2["request_id"])
        self.assertFalse(r1.get("deduped", False))
        self.assertTrue(r2.get("deduped"))
        self.assertEqual(len(self.cards), 1)  # only ONE card sent

    def test_distinct_tool_use_ids_two_cards(self):
        self._post(_perm_body(tool_use_id="tu-1"))
        self._post(_perm_body(tool_use_id="tu-2"))
        self.assertEqual(len(self.cards), 2)

    def test_missing_tool_use_id_degrades_to_no_dedupe(self):
        # Claude / AskUserQuestion path: no tool_use_id → two independent cards
        self._post(_perm_body(tool_use_id=""))
        self._post(_perm_body(tool_use_id=""))
        self.assertEqual(len(self.cards), 2)

    def test_two_pollers_get_same_decision(self):
        rid = self._post(_perm_body(tool_use_id="tu-1"))["request_id"]
        server.registry.set_decision_once(rid, {"behavior": "allow"})
        d1 = asyncio.run(server.get_permission_decision(rid))
        d2 = asyncio.run(server.get_permission_decision(rid))
        self.assertEqual(d1["status"], "decided")
        self.assertEqual(d2["status"], "decided")
        self.assertEqual(d1["decision"], d2["decision"])  # read-not-pop

    def test_decision_gc_after_grace(self):
        rid = self._post(_perm_body(tool_use_id="tu-1"))["request_id"]
        server.registry.set_decision_once(rid, {"behavior": "allow"})
        server.registry.try_consume(rid)  # consumed_at = 1000
        self.assertIsNotNone(server.registry.get(rid))
        self.clock.t = 1000.0 + server.registry._grace + 1
        server.registry.gc()
        self.assertIsNone(server.registry.get(rid))

    def test_unconsumed_codex_req_gc_after_ttl(self):
        rid = self._post(_perm_body(tool_use_id="tu-1"))["request_id"]
        self.clock.t = 1000.0 + server.registry._ttl + 1
        server.registry.gc()
        self.assertIsNone(server.registry.get(rid))
        # dedupe key freed too → a later same-key request is new (sends a card)
        r2, is_new = server.registry.register_or_get(("s1", "tu-1"))
        self.assertTrue(is_new)

    def test_none_key_request_reaped_when_poll_stale(self):
        # A None-key request (no tool_use_id, e.g. AskUserQuestion) whose hook never
        # polls / has died is reaped past the TTL. Liveness is now keyed on last_poll
        # activity, not dedupe_key — this fixes the old None-key leak (a slow user
        # whose hook keeps polling stays protected, see test_active_poller_*).
        rid = self._post(_perm_body(tool_use_id=""))["request_id"]
        self.clock.t = 1000.0 + server.registry._ttl + 100
        server.registry.gc()
        self.assertIsNone(server.registry.get(rid))

    def test_active_poller_survives_ttl(self):
        # Slow user: past the TTL, but a hook is still long-polling (fresh
        # last_poll) → the valid card must NOT be reaped out from under them.
        rid = self._post(_perm_body(tool_use_id="tu-1"))["request_id"]
        self.clock.t = 1000.0 + server.registry._ttl + 49
        server.registry.mark_poll(rid)
        self.clock.t = 1000.0 + server.registry._ttl + 50
        server.registry.gc()
        self.assertIsNotNone(server.registry.get(rid))

    def test_post_tool_invalidates_session_cards(self):
        # End-to-end: an open permission card, then PostToolUse fires for the session
        # → the card is invalidated (edited grey) and the decision long-poll returns
        # "invalidated" so the hook fails open instead of waiting to 1800s.
        edits = []
        with patch.object(server, "_edit_card", lambda mid, card: edits.append(mid)):
            rid = self._post(_perm_body(tool_use_id="tu-1"))["request_id"]
            res = asyncio.run(server.receive_post_tool_hook(_Req({"session_id": "s1"})))
        self.assertEqual(res["invalidated"], 1)
        self.assertEqual(edits, ["cardmsg"])  # the card_msg_id was edited to the grey card
        self.assertTrue(server.registry.is_invalidated(rid))
        dec = asyncio.run(server.get_permission_decision(rid))
        self.assertEqual(dec["status"], "invalidated")

    def test_post_tool_missing_session_noop(self):
        res = asyncio.run(server.receive_post_tool_hook(_Req({})))
        self.assertFalse(res["ok"])

    def test_card_send_failure_releases_slot(self):
        # If the card never reaches Feishu the dedupe slot is released so codex's
        # duplicate comes through as is_new and re-sends (not stranded).
        with patch.object(server, "_reply_card",
                          lambda mid, card, reply_in_thread=False: None):
            r1 = self._post(_perm_body(tool_use_id="tu-1"))
        self.assertFalse(r1.get("ok"))
        # retry with a working card (setUp's mock) → is_new, sends exactly one card
        r2 = self._post(_perm_body(tool_use_id="tu-1"))
        self.assertFalse(r2.get("deduped", False))
        self.assertTrue(r2.get("ok"))
        self.assertEqual(len(self.cards), 1)


class PermNoThreadRootTests(unittest.TestCase):
    """A HITL (permission / AskUserQuestion) that is a session's FIRST outbound event
    has no Feishu thread root yet (the root is created by Stop/Notification hooks via
    /hook). The card must NOT leak into the group's main conversation: walkcode
    creates the thread root now and replies the card into that thread, and persists
    the root so the session's later hooks reuse it.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = (server.config, server.session_store, server.registry)
        server.config = Config(
            feishu_app_id="x", feishu_app_secret="y",
            feishu_receive_id="rid", feishu_receive_id_type="open_id",
        )
        server.session_store = SessionStore(Path(self._tmp.name) / "state.json")
        server.registry = PermissionRegistry(now=_Clock(1000.0))

        def _restore():
            server.config, server.session_store, server.registry = self._orig
        self.addCleanup(_restore)

        self.cards = []   # ("reply", root) or ("send", None)
        self.sent = []    # thread-root titles created via _send
        ps = patch.object(server, "_send", lambda text: self.sent.append(text) or "newroot")
        pr = patch.object(server, "_reply_card",
                          lambda mid, card, reply_in_thread=False: self.cards.append(("reply", mid)) or "cardmsg")
        psc = patch.object(server, "_send_card",
                           lambda card: self.cards.append(("send", None)) or "cardmsg")
        # no Feishu-initiated pending root for this tty
        ppop = patch.object(server.session_store, "pop_pending", lambda tty: (None, None, None))
        for p in (ps, pr, psc, ppop):
            p.start(); self.addCleanup(p.stop)

    def _post(self, body):
        return asyncio.run(server.receive_permission_hook(_Req(body)))

    def test_first_event_hitl_creates_thread_root_and_replies_in_thread(self):
        # session is known (SessionStart synced tty/cwd) but has NO thread root yet
        server.session_store.upsert("s2", tty="tmux2", cwd="/tmp/proj")
        r = self._post(_perm_body(tool_use_id="", session_id="s2", tty="tmux2"))
        self.assertTrue(r.get("ok"))
        self.assertEqual(len(self.sent), 1, "must create a thread root")
        # card lands in the new thread, NOT the group main conversation
        self.assertEqual(self.cards, [("reply", "newroot")])
        # root persisted so the session's later hooks reuse the same thread
        self.assertEqual(server.session_store.get("s2").root_msg_id, "newroot")

    def test_thread_root_creation_failure_falls_back_to_group(self):
        # if root creation itself fails (_send returns None), still surface the HITL
        # rather than dropping it
        server.session_store.upsert("s3", tty="tmux3", cwd="/tmp/proj")
        with patch.object(server, "_send", lambda text: None):
            r = self._post(_perm_body(tool_use_id="", session_id="s3", tty="tmux3"))
        self.assertTrue(r.get("ok"))
        self.assertEqual(self.cards, [("send", None)], "fall back to group when root can't be created")


class HandlePermissionForwardingTests(unittest.TestCase):
    """_handle_permission_request must forward tool_use_id + turn_id in the POST."""

    def _resp(self, payload):
        class _R:
            def __init__(self, p):
                self._p = json.dumps(p).encode()

            def read(self):
                return self._p

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return _R(payload)

    def test_forwards_tool_use_id_and_turn_id(self):
        captured = {}
        calls = [0]

        def fake_urlopen(req, timeout=None):
            calls[0] += 1
            if calls[0] == 1:  # POST /hook/permission
                captured["body"] = json.loads(req.data.decode())
                return self._resp({"request_id": "rid-1"})
            return self._resp({"status": "decided", "decision": {"behavior": "allow"}})

        hook_data = {
            "tool_name": "Bash", "tool_input": {"command": "ls"},
            "tool_use_id": "tu-9", "turn_id": "turn-9", "permission_mode": "default",
        }
        with patch.object(m.urllib.request, "urlopen", fake_urlopen), \
             patch.dict(os.environ, {"WALKCODE_AGENT": "claude"}, clear=False), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit):
                m._handle_permission_request(hook_data, 3999, "tmux1", "/tmp/proj", "s1")

        self.assertEqual(captured["body"]["tool_use_id"], "tu-9")
        self.assertEqual(captured["body"]["turn_id"], "turn-9")

    def test_bypass_permissions_non_askuser_short_circuits(self):
        hook_data = {
            "tool_name": "Bash", "tool_input": {"command": "ls"},
            "permission_mode": "bypassPermissions",
        }
        with patch.object(m.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(SystemExit) as cm:
                m._handle_permission_request(hook_data, 3999, "tmux1", "/tmp/proj", "s1")

        self.assertEqual(cm.exception.code, 0)
        urlopen.assert_not_called()

    def test_bypass_permissions_askuserquestion_still_posts(self):
        captured = {}
        calls = [0]

        def fake_urlopen(req, timeout=None):
            calls[0] += 1
            if calls[0] == 1:  # POST /hook/permission
                captured["body"] = json.loads(req.data.decode())
                return self._resp({"request_id": "rid-ask"})
            return self._resp({
                "status": "decided",
                "decision": {
                    "behavior": "allow",
                    "updatedInput": {"answers": ["范围 A"]},
                },
            })

        hook_data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "迁移范围选哪个？"}]},
            "permission_mode": "bypassPermissions",
        }
        with patch.object(m.urllib.request, "urlopen", fake_urlopen), \
             patch.dict(os.environ, {"WALKCODE_AGENT": "claude"}, clear=False), \
             patch("sys.stdout", io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                m._handle_permission_request(hook_data, 3999, "tmux1", "/tmp/proj", "s1")

        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(captured["body"]["tool_name"], "AskUserQuestion")
        self.assertEqual(captured["body"]["tool_input"], hook_data["tool_input"])


if __name__ == "__main__":
    unittest.main()
