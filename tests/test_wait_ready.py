"""Tests for tty.wait_until_input_ready — the resume-inject readiness gate.

Background: resuming a session replays/re-renders its whole history (a
100%-context session takes a minute-plus) before the input prompt appears.
Injecting during that window lands the paste in a not-yet-ready TUI and the Enter
is dropped, so the message is never submitted — and a freshly resumed session
reported "not delivered". The old code guessed a fixed `sleep(3)`, which lost the
race on big sessions. wait_until_input_ready replaces the guess: it blocks until
the agent process is up AND the pane has stopped repainting (render/compaction
finished, prompt rendered), with a timeout backstop.

These tests pin that contract with a fake clock (no real waiting).
"""

import unittest
from unittest.mock import patch

from walkcode import tty


class _Clock:
    """Deterministic clock: sleep() advances virtual time, time() reads it."""

    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


def _scripted(frames):
    """capture_pane stub: yield each frame once, then repeat the last forever."""
    seq = list(frames)

    def _cap(session_name, lines=40):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return _cap


class WaitReadyTests(unittest.TestCase):
    def _run(self, *, alive, frames, timeout=120.0, stable_for=2.0, poll=0.5):
        clock = _Clock()
        with patch.object(tty.time, "time", clock.time), \
             patch.object(tty.time, "sleep", clock.sleep), \
             patch.object(tty, "is_agent_alive", alive), \
             patch.object(tty, "capture_pane", frames):
            result = tty.wait_until_input_ready(
                "sess", timeout=timeout, stable_for=stable_for, poll=poll
            )
        return result, clock.now

    def test_stable_pane_is_ready(self):
        # Process up, screen byte-stable → ready once it holds for stable_for.
        ready, elapsed = self._run(
            alive=lambda s: True,
            frames=lambda s, lines=40: "❯ idle prompt",
        )
        self.assertTrue(ready)
        # Needs prev capture + stable_for to elapse; well under the timeout.
        self.assertLess(elapsed, 10.0)

    def test_changing_pane_times_out(self):
        # Screen keeps repainting (history replay / spinner) → never ready.
        counter = {"n": 0}

        def churning(s, lines=40):
            counter["n"] += 1
            return f"frame {counter['n']}"

        ready, elapsed = self._run(alive=lambda s: True, frames=churning)
        self.assertFalse(ready)
        self.assertGreaterEqual(elapsed, 120.0)

    def test_waits_for_process_then_settles(self):
        # Agent not up for the first few polls, then comes up and stabilizes.
        state = {"polls": 0}

        def alive(s):
            state["polls"] += 1
            return state["polls"] > 3

        ready, _ = self._run(
            alive=alive,
            frames=lambda s, lines=40: "ready prompt",
        )
        self.assertTrue(ready)

    def test_stability_resets_on_change_then_becomes_ready(self):
        # Replay paints A, B, C, then lands on a steady prompt → ready only after
        # it stops changing (not on the first static-looking frame mid-replay).
        ready, _ = self._run(
            alive=lambda s: True,
            frames=_scripted(["A", "B", "C", "DONE"]),
        )
        self.assertTrue(ready)

    def test_blank_pane_never_counts_as_ready(self):
        # An empty capture must not be mistaken for a settled prompt.
        ready, elapsed = self._run(
            alive=lambda s: True,
            frames=lambda s, lines=40: "   \n  ",
        )
        self.assertFalse(ready)
        self.assertGreaterEqual(elapsed, 120.0)


if __name__ == "__main__":
    unittest.main()
