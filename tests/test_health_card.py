"""Session health card: state machine, store mutators, and summary gating."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from walkcode import server
from walkcode.config import Config
from walkcode.state import SessionStore, Session
from walkcode.stats import SessionStats


def _stats(last_error=None, title="t"):
    return SessionStats(title=title, per_model=(), duration_minutes=1,
                        input_rounds=1, last_error=last_error, source="ok")


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class SessionHealthStateMachineTest(unittest.TestCase):
    """_session_health priority: timeout > waiting > running > error/done."""

    def setUp(self):
        server._session_last_ups.clear()
        server._session_last_stop.clear()
        self._reg = server.registry
        self._store = server.session_store
        server.registry = mock.MagicMock()
        server.registry.has_open_request.return_value = False
        server.session_store = mock.MagicMock()
        server.session_store.get.return_value = None
        self.addCleanup(lambda: setattr(server, "registry", self._reg))
        self.addCleanup(lambda: setattr(server, "session_store", self._store))

    def test_hitl_overrides_running(self):
        server.registry.has_open_request.return_value = True
        server._session_last_ups["s"] = 100.0  # also busy, but HITL wins
        self.assertEqual(server._session_health("s", _stats()), "hitl")

    def test_timeout_overrides_hitl(self):
        sess = Session(tty="t", cwd="/c", root_msg_id="r")
        sess.status = "stopped"
        sess.stop_reason = "interrupted"
        sess.interrupt_reason = "timeout"
        server.registry.has_open_request.return_value = True
        with mock.patch.object(server, "session_store") as store:
            store.get.return_value = sess
            self.assertEqual(server._session_health("s", _stats()), "timeout")

    def test_permission_waiting_is_hitl_without_registry_memory(self):
        sess = Session(tty="t", cwd="/c", root_msg_id="r")
        sess.status = "stopped"
        sess.stop_reason = "permission_request"
        sess.running_since = 123.0
        with mock.patch.object(server, "session_store") as store:
            store.get.return_value = sess
            self.assertEqual(server._session_health("s", _stats()), "hitl")

    def test_ask_user_question_waiting_is_hitl_without_registry_memory(self):
        sess = Session(tty="t", cwd="/c", root_msg_id="r")
        sess.status = "stopped"
        sess.stop_reason = "ask_user_question"
        sess.running_since = 123.0
        with mock.patch.object(server, "session_store") as store:
            store.get.return_value = sess
            self.assertEqual(server._session_health("s", _stats()), "hitl")

    def test_running_when_busy(self):
        server._session_last_ups["s"] = 200.0
        server._session_last_stop["s"] = 100.0  # ups > stop → busy
        self.assertEqual(server._session_health("s", _stats()), "running")

    def test_running_when_never_stopped(self):
        # no ups, no stop → starting up → running, NOT done [R1]
        self.assertEqual(server._session_health("s", _stats()), "running")

    def test_done_when_stopped_no_error(self):
        server._session_last_stop["s"] = 100.0  # stopped, not busy
        self.assertEqual(server._session_health("s", _stats()), "done")

    def test_error_when_stopped_with_error(self):
        server._session_last_stop["s"] = 100.0
        self.assertEqual(server._session_health("s", _stats(last_error="boom")), "error")

    def test_timeout_status_overrides_terminal_done(self):
        sess = Session(tty="t", cwd="/c", root_msg_id="r")
        sess.status = "stopped"
        sess.stop_reason = "interrupted"
        sess.interrupt_reason = "timeout"
        with mock.patch.object(server, "session_store") as store:
            store.get.return_value = sess
            server._session_last_stop["s"] = 100.0
            self.assertEqual(server._session_health("s", _stats()), "timeout")


class HealthCardStoreTest(unittest.TestCase):
    """set_* mutators must persist (locked + saved), survive reload [R3]."""

    def test_set_methods_persist_across_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            store = SessionStore(p)
            store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            store.set_health_card("s", "card1")
            store.set_title("s", "精炼标题", "summary")
            store.start_running("s", 123.0)
            store.set_stopped("s", "completed")
            store2 = SessionStore(p)
            store2.load()
            sess = store2.get("s")
            self.assertEqual(sess.health_card_id, "card1")
            self.assertEqual(sess.cached_title, "精炼标题")
            self.assertEqual(sess.title_source, "summary")
            self.assertEqual(sess.status, "stopped")
            self.assertEqual(sess.stop_reason, "completed")
            self.assertEqual(sess.interrupt_reason, "")
            self.assertEqual(sess.running_since, 0.0)

    def test_running_since_persists_across_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            store = SessionStore(p)
            store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            store.start_running("s", 123.0)

            store2 = SessionStore(p)
            store2.load()
            sess = store2.get("s")
            self.assertEqual(sess.status, "running")
            self.assertEqual(sess.stop_reason, "")
            self.assertEqual(sess.interrupt_reason, "")
            self.assertEqual(sess.running_since, 123.0)

    def test_legacy_last_status_migrates_to_status_model(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            p.write_text("""{
              "sessions": {
                "run": {"tty": "t1", "cwd": "/c", "last_status": "", "running_since": 11},
                "done": {"tty": "t2", "cwd": "/c", "last_status": "stopped"},
                "tout": {"tty": "t3", "cwd": "/c", "last_status": "timeout"}
              }
            }""")
            store = SessionStore(p)
            store.load()

            self.assertEqual(store.get("run").status, "running")
            self.assertEqual(store.get("run").running_since, 11.0)
            self.assertEqual(store.get("done").status, "stopped")
            self.assertEqual(store.get("done").stop_reason, "completed")
            self.assertEqual(store.get("tout").status, "stopped")
            self.assertEqual(store.get("tout").stop_reason, "interrupted")
            self.assertEqual(store.get("tout").interrupt_reason, "timeout")

    def test_set_methods_noop_on_unknown_session(self):
        with tempfile.TemporaryDirectory() as d:
            store = SessionStore(Path(d) / "state.json")
            store.set_health_card("nope", "x")  # no crash, no-op
            store.set_stopped("nope", "completed")
            self.assertIsNone(store.get("nope"))


class HealthCardBuildTest(unittest.TestCase):
    def test_card_includes_full_session_id(self):
        session_id = "229719b9-0a2c-4f55-aa31-ed8fc60a5dfe"
        card = server._build_health_card(_stats(), "running", "t", session_id=session_id)
        contents = [
            el.get("content", "")
            for el in card["elements"]
            if el.get("tag") == "markdown"
        ]
        self.assertTrue(any(session_id in c for c in contents))


class ReceiveSyncPendingHealthCardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cfg = server.config
        self._store = server.session_store
        self._adapter = server.agent_adapter
        server.config = Config(
            feishu_app_id="x", feishu_app_secret="y",
            feishu_receive_id="", feishu_receive_id_type="open_id",
            default_cwd="/default",
        )
        server.agent_adapter = mock.MagicMock(name="claude")
        server.agent_adapter.name = "claude"
        server.session_store = SessionStore(Path(self._tmp.name) / "state.json")
        self.addCleanup(self._restore)

    def _restore(self):
        self._tmp.cleanup()
        server.config = self._cfg
        server.session_store = self._store
        server.agent_adapter = self._adapter

    def test_session_start_binds_pending_root_and_refreshes_card(self):
        session_id = "229719b9-0a2c-4f55-aa31-ed8fc60a5dfe"
        server.session_store.add_pending(
            "tmux-pending", "root-card", reply_id="reply-start",
            cwd="/launch", health_card_id="root-card",
        )
        edits = []
        refreshes = []

        with mock.patch("walkcode.server._edit_message",
                        lambda mid, text: edits.append((mid, text)) or True), \
             mock.patch("walkcode.server._refresh_health_card_for_event",
                        lambda sid, **kw: refreshes.append((sid, kw)) or False):
            res = asyncio.run(server.receive_sync_hook(_Req({
                "tty": "tmux-pending",
                "session_id": session_id,
                "cwd": "/sync-cwd",
            })))

        self.assertEqual(res, {"ok": True})
        sess = server.session_store.get(session_id)
        self.assertEqual(sess.root_msg_id, "root-card")
        self.assertEqual(sess.health_card_id, "root-card")
        self.assertEqual(sess.cwd, "/launch")
        self.assertGreater(sess.running_since, 0.0)
        self.assertEqual(server.session_store.resolve(root_id="root-card"), session_id)
        self.assertIsNone(server.session_store.resolve_pending_tty("root-card"))
        self.assertEqual(refreshes, [(session_id, {})])
        self.assertEqual(edits[0][0], "reply-start")
        self.assertIn(session_id[:8], edits[0][1])

    def test_pending_without_cwd_uses_session_start_cwd(self):
        server.session_store.add_pending("tmux-pending", "root-card", health_card_id="root-card")

        with mock.patch("walkcode.server._refresh_health_card_for_event", return_value=False):
            asyncio.run(server.receive_sync_hook(_Req({
                "tty": "tmux-pending",
                "session_id": "sid-sync-cwd",
                "cwd": "/sync-launch",
            })))

        self.assertEqual(server.session_store.get("sid-sync-cwd").cwd, "/sync-launch")

    def test_session_start_sync_does_not_reopen_stopped_session(self):
        session_id = "sid-stopped-sync"
        server.session_store.upsert(
            session_id, tty="tmux-old", cwd="/old", root_msg_id="root-card",
        )
        server.session_store.set_stopped(session_id, "interrupted", interrupt_reason="timeout")

        res = asyncio.run(server.receive_sync_hook(_Req({
            "tty": "tmux-old",
            "session_id": session_id,
            "cwd": "/new",
        })))

        sess = server.session_store.get(session_id)
        self.assertEqual(res, {"ok": True})
        self.assertEqual(sess.tty, "tmux-old")
        self.assertEqual(sess.cwd, "/new")
        self.assertEqual(sess.status, "stopped")
        self.assertEqual(sess.stop_reason, "interrupted")
        self.assertEqual(sess.interrupt_reason, "timeout")
        self.assertEqual(sess.running_since, 0.0)


class MaybeSummarizeGateTest(unittest.TestCase):
    """_maybe_summarize is opt-in, codex-only, and event-triggered."""

    def setUp(self):
        self._cfg = server.config
        server._summarizing.clear()
        self.addCleanup(lambda: setattr(server, "config", self._cfg))
        self.addCleanup(server._summarizing.clear)

    def _session(self, title_source=""):
        s = Session(tty="t", cwd="/c")
        s.title_source = title_source
        return s

    def test_skip_when_summary_disabled(self):
        server.config = mock.MagicMock(summary_enabled=False, agent="codex")
        with mock.patch("walkcode.server.summarizer.summarize_async") as m:
            server._maybe_summarize("s", self._session(), _stats())
        m.assert_not_called()

    def test_skip_when_not_codex(self):
        server.config = mock.MagicMock(summary_enabled=True, agent="claude")
        with mock.patch("walkcode.server.summarizer.summarize_async") as m:
            server._maybe_summarize("s", self._session(), _stats())
        m.assert_not_called()

    def test_already_refined_can_update_on_later_stop(self):
        server.config = mock.MagicMock(summary_enabled=True, agent="codex")
        with mock.patch("walkcode.server.summarizer.summarize_async") as m:
            server._maybe_summarize("s", self._session(title_source="summary"), _stats())
        m.assert_called_once()

    def test_dispatches_once_for_codex(self):
        server.config = mock.MagicMock(
            summary_enabled=True, agent="codex",
            summary_vertex_project="p", summary_vertex_region="r", summary_sa_path="/sa",
            summary_model="claude-haiku-4-5", summary_timeout=8.0)
        with mock.patch("walkcode.server.summarizer.summarize_async") as m:
            server._maybe_summarize(
                "scodex", self._session(), _stats(title="首句"), "本轮完成了修复",
            )
            # second call deduped while first is in-flight
            server._maybe_summarize("scodex", self._session(), _stats(title="首句"))
        m.assert_called_once()
        self.assertEqual(m.call_args.kwargs["recent_turn"], "本轮完成了修复")


class HealthCardRefreshTest(unittest.TestCase):
    def setUp(self):
        self._cfg = server.config
        self._store = server.session_store
        self._reg = server.registry
        self._last_stop = dict(server._session_last_stop)
        self._last_ups = dict(server._session_last_ups)
        server._session_last_stop.clear()
        server._session_last_ups.clear()
        self.addCleanup(lambda: setattr(server, "config", self._cfg))
        self.addCleanup(lambda: setattr(server, "session_store", self._store))
        self.addCleanup(lambda: setattr(server, "registry", self._reg))
        self.addCleanup(lambda: (server._session_last_stop.clear(), server._session_last_stop.update(self._last_stop)))
        self.addCleanup(lambda: (server._session_last_ups.clear(), server._session_last_ups.update(self._last_ups)))

    def test_user_prompt_submit_refreshes_without_summarizing(self):
        with tempfile.TemporaryDirectory() as d:
            server.config = mock.MagicMock(health_card_enabled=True, agent="claude")
            server.session_store = SessionStore(Path(d) / "state.json")
            server.session_store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            server.session_store.set_health_card("s", "card1")
            server.session_store.set_stopped("s", "completed")
            server.registry = mock.MagicMock()
            server.registry.has_open_request.return_value = False
            server._session_last_stop["s"] = 100.0

            with mock.patch("walkcode.server.collect_stats", return_value=_stats()) as collect, \
                 mock.patch("walkcode.server._edit_card", return_value=True) as edit_card, \
                 mock.patch("walkcode.server._maybe_summarize") as summarize:
                res = asyncio.run(server.receive_prompt_hook(
                    _Req({"tty": "t", "session_id": "s", "prompt": "继续"}),
                ))

        self.assertEqual(res, {"ok": True})
        self.assertTrue(server._is_session_busy("s"))
        self.assertEqual(server.session_store.get("s").status, "running")
        collect.assert_called_once()
        edit_card.assert_called_once()
        summarize.assert_not_called()

    def test_event_refresh_summarizes_and_freezes_terminal_stop(self):
        with tempfile.TemporaryDirectory() as d:
            server.config = mock.MagicMock(health_card_enabled=True, agent="codex")
            server.session_store = SessionStore(Path(d) / "state.json")
            server.session_store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            server.session_store.set_health_card("s", "card1")
            server.registry = mock.MagicMock()
            server.registry.has_open_request.return_value = False
            server._session_last_stop["s"] = 100.0

            with mock.patch("walkcode.server.collect_stats", return_value=_stats()), \
                 mock.patch("walkcode.server._edit_card", return_value=True), \
                 mock.patch("walkcode.server._maybe_summarize") as summarize:
                server._refresh_health_card_for_event(
                    "s", summarize=True, freeze_if_terminal=True,
                    recent_turn="Stop result",
                )

            summarize.assert_called_once_with("s", mock.ANY, mock.ANY, "Stop result")
            sess = server.session_store.get("s")
            self.assertEqual(sess.status, "stopped")
            self.assertEqual(sess.stop_reason, "completed")


if __name__ == "__main__":
    unittest.main()
