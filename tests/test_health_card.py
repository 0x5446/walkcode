"""Session health card: state machine, store mutators, and summary gating."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from walkcode import server
from walkcode.state import SessionStore, Session
from walkcode.stats import SessionStats


def _stats(last_error=None, title="t"):
    return SessionStats(title=title, per_model=(), duration_minutes=1,
                        input_rounds=1, last_error=last_error, source="ok")


class SessionHealthStateMachineTest(unittest.TestCase):
    """_session_health priority: HITL > running > (never stopped→running) > error/done."""

    def setUp(self):
        server._session_last_ups.clear()
        server._session_last_stop.clear()
        self._reg = server.registry
        server.registry = mock.MagicMock()
        server.registry.has_open_request.return_value = False
        self.addCleanup(lambda: setattr(server, "registry", self._reg))

    def test_hitl_overrides_running(self):
        server.registry.has_open_request.return_value = True
        server._session_last_ups["s"] = 100.0  # also busy, but HITL wins
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


class HealthCardStoreTest(unittest.TestCase):
    """set_* mutators must persist (locked + saved), survive reload [R3]."""

    def test_set_methods_persist_across_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state.json"
            store = SessionStore(p)
            store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            store.set_health_card("s", "card1")
            store.set_title("s", "精炼标题", "summary")
            store.set_status("s", "stopped")
            store2 = SessionStore(p)
            store2.load()
            sess = store2.get("s")
            self.assertEqual(sess.health_card_id, "card1")
            self.assertEqual(sess.cached_title, "精炼标题")
            self.assertEqual(sess.title_source, "summary")
            self.assertEqual(sess.last_status, "stopped")

    def test_set_methods_noop_on_unknown_session(self):
        with tempfile.TemporaryDirectory() as d:
            store = SessionStore(Path(d) / "state.json")
            store.set_health_card("nope", "x")  # no crash, no-op
            store.set_status("nope", "stopped")
            self.assertIsNone(store.get("nope"))


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
            server._maybe_summarize("scodex", self._session(), _stats(title="首句"))
            # second call deduped while first is in-flight
            server._maybe_summarize("scodex", self._session(), _stats(title="首句"))
        m.assert_called_once()


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

    def test_poller_refresh_does_not_summarize(self):
        with tempfile.TemporaryDirectory() as d:
            server.config = mock.MagicMock(health_card_enabled=True, agent="codex")
            server.session_store = SessionStore(Path(d) / "state.json")
            server.session_store.upsert("s", tty="t", cwd="/c", root_msg_id="r")
            server.session_store.set_health_card("s", "card1")
            server.registry = mock.MagicMock()
            server.registry.has_open_request.return_value = False

            with mock.patch("walkcode.server.collect_stats", return_value=_stats()), \
                 mock.patch("walkcode.server._edit_card", return_value=True), \
                 mock.patch("walkcode.server._maybe_summarize") as summarize:
                server._refresh_all_health_cards()

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
                )

            summarize.assert_called_once()
            self.assertEqual(server.session_store.get("s").last_status, "stopped")


if __name__ == "__main__":
    unittest.main()
