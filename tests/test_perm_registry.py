"""Unit + concurrency tests for PermissionRegistry (the permission state machine).

The permission link can't be exercised end-to-end (codex bypass mode skips
permission requests), so these tests — including threading.Barrier races — are
the safety net for the single-lock state machine:

* set_decision_once: first writer wins, the rest are refused (A, no allow→deny tearing)
* try_consume ↔ claim_fallback: mutually exclusive (B, never both poll AND inject)
* gc: grace reap, TTL backstop, None-key (AskUserQuestion) exemption, active-poller survival
* card_failed releases the dedupe key so a duplicate re-registers (D)
"""

import threading
import unittest

from walkcode.permreg import CardStatus, PermissionRegistry


class _Clock:
    """A mutable, callable clock for deterministic time control."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class RegisterTests(unittest.TestCase):
    def test_none_key_always_new(self):
        reg = PermissionRegistry(now=_Clock())
        a, na = reg.register_or_get(None)
        b, nb = reg.register_or_get(None)
        self.assertTrue(na and nb)
        self.assertNotEqual(a.rid, b.rid)

    def test_same_key_dedupes(self):
        reg = PermissionRegistry(now=_Clock())
        a, na = reg.register_or_get(("s", "t1"))
        reg.card_sent(a.rid)
        b, nb = reg.register_or_get(("s", "t1"))
        self.assertTrue(na)
        self.assertFalse(nb)
        self.assertEqual(a.rid, b.rid)

    def test_failed_key_reregisters(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(("s", "t1"))
        reg.card_failed(a.rid)
        b, nb = reg.register_or_get(("s", "t1"))
        self.assertTrue(nb)
        self.assertNotEqual(a.rid, b.rid)

    def test_fill_request_sets_fields(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(("s", "t1"))
        reg.fill_request(a.rid, tool_name="Bash", tty="tmux1")
        req = reg.get(a.rid)
        self.assertEqual(req.tool_name, "Bash")
        self.assertEqual(req.tty, "tmux1")


class DecisionTests(unittest.TestCase):
    def test_set_decision_once_first_wins(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(("s", "t1"))
        self.assertTrue(reg.set_decision_once(a.rid, {"behavior": "allow"}))
        self.assertFalse(reg.set_decision_once(a.rid, {"behavior": "deny"}))
        self.assertEqual(reg.get(a.rid).decision, {"behavior": "allow"})
        self.assertTrue(reg.get(a.rid).decided.is_set())

    def test_set_decision_once_single_winner_under_barrier(self):
        # codex double-fire + double card click: many threads race to decide; the
        # state machine must let exactly one through (no allow→deny tearing).
        for trial in range(60):
            reg = PermissionRegistry(now=_Clock())
            a, _ = reg.register_or_get(("s", f"t{trial}"))
            n = 8
            barrier = threading.Barrier(n)
            results = []
            lk = threading.Lock()

            def worker(i):
                barrier.wait()
                won = reg.set_decision_once(a.rid, {"behavior": "allow" if i % 2 else "deny", "i": i})
                with lk:
                    results.append(won)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(sum(results), 1, f"trial {trial}: not exactly one winner")
            self.assertIsNotNone(reg.get(a.rid).decision)


class ConsumeFallbackTests(unittest.TestCase):
    def _decided(self, clock):
        reg = PermissionRegistry(now=clock, quiesce=5.0)
        a, _ = reg.register_or_get(("s", "t1"))
        reg.set_decision_once(a.rid, {"behavior": "allow"})
        return reg, a.rid

    def test_try_consume_returns_and_marks(self):
        clock = _Clock(1000.0)
        reg, rid = self._decided(clock)
        self.assertEqual(reg.try_consume(rid), {"behavior": "allow"})
        # read-not-pop: a second poller within grace still gets it
        self.assertEqual(reg.try_consume(rid), {"behavior": "allow"})
        self.assertIsNotNone(reg.get(rid).consumed_at)

    def test_consumed_blocks_fallback(self):
        clock = _Clock(1000.0)
        reg, rid = self._decided(clock)
        reg.try_consume(rid)
        clock.t = 1000.0 + 100  # even far past quiesce
        self.assertFalse(reg.claim_fallback(rid))

    def test_fallback_requires_quiesce(self):
        clock = _Clock(1000.0)
        reg, rid = self._decided(clock)
        # right after creation/decision → too soon to claim
        self.assertFalse(reg.claim_fallback(rid))
        clock.t = 1000.0 + 6  # past quiesce, no poll
        self.assertTrue(reg.claim_fallback(rid))
        # once claimed, a poller can no longer consume
        self.assertIsNone(reg.try_consume(rid))

    def test_active_poll_blocks_fallback(self):
        clock = _Clock(1000.0)
        reg, rid = self._decided(clock)
        clock.t = 1000.0 + 10
        reg.mark_poll(rid)              # a hook is still polling
        clock.t = 1000.0 + 12          # only 2s since last poll
        self.assertFalse(reg.claim_fallback(rid))

    def test_consume_vs_fallback_mutually_exclusive_under_barrier(self):
        for trial in range(100):
            clock = _Clock(1000.0)
            reg = PermissionRegistry(now=clock, quiesce=5.0)
            a, _ = reg.register_or_get(("s", f"t{trial}"))
            reg.set_decision_once(a.rid, {"behavior": "allow"})
            clock.t = 1000.0 + 10  # past quiesce so the fallback is eligible
            rid = a.rid
            barrier = threading.Barrier(2)
            out = {}

            def consumer():
                barrier.wait()
                out["consumed"] = reg.try_consume(rid) is not None

            def claimer():
                barrier.wait()
                out["claimed"] = reg.claim_fallback(rid)

            t1 = threading.Thread(target=consumer)
            t2 = threading.Thread(target=claimer)
            t1.start(); t2.start(); t1.join(); t2.join()
            self.assertFalse(out["consumed"] and out["claimed"], f"trial {trial}: both won")
            self.assertTrue(out["consumed"] or out["claimed"], f"trial {trial}: neither won")


class GcTests(unittest.TestCase):
    def test_grace_reaps_consumed(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, grace=5.0)
        a, _ = reg.register_or_get(("s", "t1"))
        reg.set_decision_once(a.rid, {"behavior": "allow"})
        reg.try_consume(a.rid)  # consumed_at = 1000
        self.assertIsNotNone(reg.get(a.rid))
        clock.t = 1000.0 + 6
        reg.gc()
        self.assertIsNone(reg.get(a.rid))

    def test_ttl_reaps_undrained_codex_request(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        a, _ = reg.register_or_get(("s", "t1"))
        clock.t = 1000.0 + 91
        reg.gc()
        self.assertIsNone(reg.get(a.rid))

    def test_none_key_active_poll_survives_ttl(self):
        # An AskUserQuestion (None-key) whose hook keeps polling survives the TTL —
        # liveness is keyed on last_poll activity, not on dedupe_key.
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        a, _ = reg.register_or_get(None)
        clock.t = 1000.0 + 89
        reg.mark_poll(a.rid)  # hook still polling
        clock.t = 1000.0 + 95
        reg.gc()
        self.assertIsNotNone(reg.get(a.rid))

    def test_none_key_reaped_when_poll_stale(self):
        # Regression for the None-key leak: the old code exempted None-key requests
        # from the TTL forever. Now a dead-hook AskUserQuestion (no poll past TTL) is
        # reaped like any other.
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        a, _ = reg.register_or_get(None)
        clock.t = 1000.0 + 91
        reg.gc()
        self.assertIsNone(reg.get(a.rid))

    def test_none_key_registration_triggers_gc(self):
        # A pure-None-key (claude) workload only triggers GC on registration; a dead
        # None-key request must be reaped by a later None-key registration.
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        dead, _ = reg.register_or_get(None)
        clock.t = 1000.0 + 91
        reg.register_or_get(None)  # later None-key registration runs GC
        self.assertIsNone(reg.get(dead.rid))

    def test_active_poller_survives_ttl(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        a, _ = reg.register_or_get(("s", "t1"))
        clock.t = 1000.0 + 89
        reg.mark_poll(a.rid)
        clock.t = 1000.0 + 95
        reg.gc()
        self.assertIsNotNone(reg.get(a.rid))

    def test_dedupe_key_cleaned_on_reap(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, ttl=90.0)
        a, _ = reg.register_or_get(("s", "t1"))
        clock.t = 1000.0 + 91
        reg.gc()
        # the key is free again → a later request with the same key is new
        b, nb = reg.register_or_get(("s", "t1"))
        self.assertTrue(nb)


class InvalidationTests(unittest.TestCase):
    def test_invalidate_marks_and_wakes(self):
        reg = PermissionRegistry(now=_Clock(1000.0))
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1", card_msg_id="c1")
        snaps = reg.invalidate_session("s1")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["card_msg_id"], "c1")
        self.assertTrue(reg.is_invalidated(a.rid))
        self.assertTrue(a.decided.is_set())  # a parked poller is woken

    def test_set_decision_rejected_after_invalidate(self):
        # Closes the check-then-act race: once invalidated, set_decision_once must
        # refuse even if a late click slips past the lock-free gate.
        reg = PermissionRegistry(now=_Clock(1000.0))
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1")
        reg.invalidate_session("s1")
        self.assertFalse(reg.set_decision_once(a.rid, {"behavior": "allow"}))
        self.assertIsNone(reg.get(a.rid).decision)

    def test_invalidate_write_once(self):
        reg = PermissionRegistry(now=_Clock(1000.0))
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1")
        self.assertEqual(len(reg.invalidate_session("s1")), 1)
        self.assertEqual(len(reg.invalidate_session("s1")), 0)  # idempotent

    def test_invalidate_skips_decided(self):
        reg = PermissionRegistry(now=_Clock(1000.0))
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1")
        reg.set_decision_once(a.rid, {"behavior": "allow"})
        self.assertEqual(len(reg.invalidate_session("s1")), 0)  # already decided
        self.assertFalse(reg.is_invalidated(a.rid))

    def test_invalidate_empty_session_noop(self):
        reg = PermissionRegistry(now=_Clock(1000.0))
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1")
        self.assertEqual(len(reg.invalidate_session("")), 0)  # no session_id → no-op
        self.assertFalse(reg.is_invalidated(a.rid))

    def test_invalidated_reaped_after_grace(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock, grace=5.0)
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, session_id="s1")
        reg.invalidate_session("s1")
        clock.t = 1000.0 + 6
        reg.gc()
        self.assertIsNone(reg.get(a.rid))

    def test_poll_age(self):
        clock = _Clock(1000.0)
        reg = PermissionRegistry(now=clock)
        a, _ = reg.register_or_get(None)
        clock.t = 1000.0 + 10
        self.assertAlmostEqual(reg.poll_age(a.rid), 10.0)  # never polled → since created
        reg.mark_poll(a.rid)
        clock.t = 1000.0 + 13
        self.assertAlmostEqual(reg.poll_age(a.rid), 3.0)
        self.assertIsNone(reg.poll_age("nonexistent"))


class CardDeliveryTests(unittest.TestCase):
    def test_await_ready(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(("s", "t1"))
        reg.card_sent(a.rid)
        self.assertEqual(reg.await_send_result(a.rid, timeout=0.2), CardStatus.READY)

    def test_await_failed(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(("s", "t1"))
        reg.card_failed(a.rid)
        self.assertEqual(reg.await_send_result(a.rid, timeout=0.2), CardStatus.FAILED)

    def test_await_unknown_is_failed(self):
        reg = PermissionRegistry(now=_Clock())
        self.assertEqual(reg.await_send_result("nope", timeout=0.2), CardStatus.FAILED)


class AskUserTests(unittest.TestCase):
    def _ask(self):
        reg = PermissionRegistry(now=_Clock())
        a, _ = reg.register_or_get(None)
        reg.fill_request(a.rid, tool_name="AskUserQuestion", feishu_root_msg_id="root1")
        return reg, a.rid

    def test_toggle_accumulates(self):
        reg, rid = self._ask()
        self.assertEqual(reg.askuser_toggle(rid, 0, 1), [1])
        self.assertEqual(reg.askuser_toggle(rid, 0, 2), [1, 2])
        self.assertEqual(reg.askuser_toggle(rid, 0, 1), [2])  # toggle off
        self.assertEqual(reg.askuser_get_selected(rid, 0), [2])

    def test_record_answer_extends(self):
        reg, rid = self._ask()
        self.assertEqual(reg.askuser_record_answer(rid, 0, "A"), ["A"])
        self.assertEqual(reg.askuser_record_answer(rid, 2, "C"), ["A", None, "C"])

    def test_record_answer_clears_awaiting_other(self):
        reg, rid = self._ask()
        reg.askuser_set_awaiting_other(rid, 0, "root1")
        self.assertEqual(reg.find_awaiting_other("root1"), rid)
        reg.askuser_record_answer(rid, 0, "typed")
        self.assertIsNone(reg.find_awaiting_other("root1"))

    def test_find_awaiting_other_matches_root_and_tool(self):
        reg, rid = self._ask()
        self.assertIsNone(reg.find_awaiting_other("root1"))  # not awaiting yet
        reg.askuser_set_awaiting_other(rid, 0, "root1")
        self.assertEqual(reg.find_awaiting_other("root1"), rid)
        self.assertIsNone(reg.find_awaiting_other("other-root"))
        self.assertIsNone(reg.find_awaiting_other(None))


if __name__ == "__main__":
    unittest.main()
