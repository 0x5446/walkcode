"""Task #21 / ADR 0056: a closed worker's CLI process must actually be dead.

Closing the SDK client only closes pipes. A worker with lingering background
children survives a "successful" disconnect, keeps the Claude session file's
single-process lock (terminal `claude --resume` exits at startup) and stays a
latent double-writer — live incident 2026-07-20: one session accumulated
three such leftovers. The close path now verifies process exit and escalates
under ADR 0053 identity rules (never signal on probe error / pid reuse).
"""

import asyncio
import os
import signal
import subprocess
import time
import unittest

from walkcode.channel_native import (
    ClaudeHeadlessTransport,
    _probe_process,
)


def _spawn_detached_sleep(seconds: int = 60) -> int:
    """A real process that survives pipe/parent churn.

    start_new_session (setsid) is load-bearing: plain `sh -c "... &"`
    orphans get reaped in some harness environments (observed 2026-07-20),
    which made process-based tests flaky.
    """
    proc = subprocess.Popen(
        ["sleep", str(seconds)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


class _FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid


class _FakeInnerTransport:
    def __init__(self, pid: int):
        self._process = _FakeProcess(pid)


class _FakeClient:
    """Disconnects 'successfully' without killing the underlying process —
    exactly the leak shape observed in production."""

    def __init__(self, pid: int):
        self._transport = _FakeInnerTransport(pid)
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _transport() -> ClaudeHeadlessTransport:
    return ClaudeHeadlessTransport(client_factory=lambda spec: object())


class WorkerExitVerificationTests(unittest.TestCase):
    def _cleanup_pid(self, pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def test_leftover_process_is_terminated_after_clean_disconnect(self):
        pid = _spawn_detached_sleep()
        self.addCleanup(self._cleanup_pid, pid)
        probe = _probe_process(pid)
        self.assertEqual(probe.status, "ok")

        transport = _transport()
        client = _FakeClient(pid)
        transport._worker_procs["h1"] = (pid, probe.lstart, probe.command)

        started = time.monotonic()
        asyncio.run(transport._disconnect_client("h1", client))

        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(_probe_process(pid).status, "gone")
        self.assertNotIn("h1", transport._worker_procs)
        # Grace(1.5s) + one TERM round — not the full KILL ladder.
        self.assertLess(time.monotonic() - started, 8.0)

    def test_identity_mismatch_is_never_signalled(self):
        # The recorded identity belongs to a DIFFERENT process (pid reuse
        # scenario): the current occupant of the pid must not be touched.
        pid = _spawn_detached_sleep()
        self.addCleanup(self._cleanup_pid, pid)

        transport = _transport()
        transport._worker_procs["h1"] = (
            pid,
            "Mon Jan  6 00:00:00 2001",
            "some-other-command --flag",
        )
        asyncio.run(transport._disconnect_client("h1", _FakeClient(pid)))

        self.assertEqual(_probe_process(pid).status, "ok")  # untouched

    def test_prompt_exit_needs_no_signal(self):
        # A healthy worker that dies right after disconnect must not be
        # signalled (and the wait returns quickly).
        pid = _spawn_detached_sleep(seconds=1)
        self.addCleanup(self._cleanup_pid, pid)
        probe = _probe_process(pid)
        transport = _transport()
        transport._worker_procs["h1"] = (pid, probe.lstart, probe.command)

        asyncio.run(transport._disconnect_client("h1", _FakeClient(pid)))
        self.assertEqual(_probe_process(pid).status, "gone")

    def test_missing_record_is_a_noop(self):
        transport = _transport()
        started = time.monotonic()
        asyncio.run(transport._disconnect_client("h-unknown", _FakeClient(0)))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_capture_records_pid_and_identity(self):
        pid = _spawn_detached_sleep()
        self.addCleanup(self._cleanup_pid, pid)
        transport = _transport()
        transport._capture_worker_proc("h1", _FakeClient(pid))
        record = transport._worker_procs.get("h1")
        self.assertIsNotNone(record)
        self.assertEqual(record[0], pid)
        self.assertIn("sleep", record[2])

    def test_pid_extraction_rejects_garbage(self):
        self.assertEqual(ClaudeHeadlessTransport._client_worker_pid(object()), 0)

        class _WeirdClient:
            _transport = type("T", (), {"_process": type("P", (), {"pid": "not-a-pid"})()})()

        self.assertEqual(ClaudeHeadlessTransport._client_worker_pid(_WeirdClient()), 0)

        class _InitPidClient:
            _transport = type("T", (), {"_process": type("P", (), {"pid": 1})()})()

        self.assertEqual(ClaudeHeadlessTransport._client_worker_pid(_InitPidClient()), 0)


if __name__ == "__main__":
    unittest.main()
