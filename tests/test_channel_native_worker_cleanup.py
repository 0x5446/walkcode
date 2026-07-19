"""Task #21 / ADR 0056: a closed worker's CLI process must actually be dead.

Closing the SDK client only closes pipes. A worker with lingering background
children survives a "successful" disconnect, keeps the Claude session file's
single-process lock (terminal `claude --resume` exits at startup) and stays a
latent double-writer — live incident 2026-07-20: one session accumulated
three such leftovers. The close path now verifies process exit as a
per-handle singleton task (cancellation-safe, shielded) and escalates under
ADR 0053 identity rules; a resume waits for the session's last known worker
to reach a terminal state before spawning the next one.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from walkcode.channel_native import (
    ClaudeHeadlessTransport,
    ResumeSpec,
    _probe_process,
)


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

    async def connect(self, prompt=None) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def _transport() -> ClaudeHeadlessTransport:
    return ClaudeHeadlessTransport(client_factory=lambda spec: object())


class _WorkerCleanupBase(unittest.TestCase):
    def _spawn(self, args=None, seconds: int = 60) -> subprocess.Popen:
        """A real process that survives pipe/parent churn.

        start_new_session (setsid) is load-bearing: plain `sh -c "... &"`
        orphans get reaped in some harness environments (observed
        2026-07-20), which made process-based tests flaky. Keeping the Popen
        and wait()ing in cleanup avoids test-process zombies.
        """
        proc = subprocess.Popen(
            args or ["sleep", str(seconds)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def _cleanup() -> None:
            try:
                proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

        self.addCleanup(_cleanup)
        return proc

    def _record(self, transport: ClaudeHeadlessTransport, handle_id: str, pid: int, session_id: str = "sess-1") -> None:
        asyncio.run(transport._capture_worker_proc(handle_id, session_id, _FakeClient(pid)))
        self.assertIn(handle_id, transport._worker_procs)


class WorkerExitVerificationTests(_WorkerCleanupBase):
    def test_leftover_process_is_terminated_after_clean_disconnect(self):
        proc = self._spawn()
        transport = _transport()
        client = _FakeClient(proc.pid)
        self._record(transport, "h1", proc.pid)

        started = time.monotonic()
        asyncio.run(transport._disconnect_client("h1", client))

        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(_probe_process(proc.pid).status, "gone")
        # Terminal outcome retires ALL tracking records.
        self.assertNotIn("h1", transport._worker_procs)
        self.assertNotIn("sess-1", transport._session_last_worker)
        self.assertLess(time.monotonic() - started, 8.0)

    def test_sigterm_immune_worker_is_sigkilled(self):
        proc = self._spawn(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
            ]
        )
        time.sleep(0.5)  # let the handler install
        transport = _transport()
        self._record(transport, "h1", proc.pid)

        asyncio.run(transport._disconnect_client("h1", _FakeClient(proc.pid)))
        self.assertEqual(_probe_process(proc.pid).status, "gone")

    def test_identity_mismatch_is_never_signalled(self):
        # The recorded identity belongs to a DIFFERENT process (pid reuse
        # scenario): the current occupant of the pid must not be touched.
        proc = self._spawn()
        transport = _transport()
        transport._worker_procs["h1"] = (
            proc.pid,
            "Mon Jan  6 00:00:00 2001",
            "some-other-command --flag",
        )
        asyncio.run(transport._disconnect_client("h1", _FakeClient(proc.pid)))

        self.assertEqual(_probe_process(proc.pid).status, "ok")  # untouched
        self.assertNotIn("h1", transport._worker_procs)  # reused == worker gone

    def test_cancelled_close_still_kills_the_worker(self):
        # The verify is a shielded singleton task: cancelling the CALLER must
        # not disarm the cleanup (review P1: a cancelled EOF drain used to
        # leave the leftover alive forever).
        proc = self._spawn()
        transport = _transport()
        self._record(transport, "h1", proc.pid)

        async def _scenario() -> None:
            closer = asyncio.create_task(transport._disconnect_client("h1", _FakeClient(proc.pid)))
            await asyncio.sleep(0.3)  # inside the grace window
            closer.cancel()
            try:
                await closer
            except asyncio.CancelledError:
                pass
            # The background singleton keeps going to a terminal state.
            for _ in range(40):
                if _probe_process(proc.pid).status == "gone":
                    return
                await asyncio.sleep(0.25)

        asyncio.run(_scenario())
        self.assertEqual(_probe_process(proc.pid).status, "gone")

    def test_probe_error_keeps_record_for_retry(self):
        # One transient ps failure must not permanently disarm the cleanup.
        transport = _transport()
        transport._worker_procs["h1"] = (4242, "Sun Jul 19 00:00:00 2026", "sleep 60")
        transport._session_last_worker["sess-1"] = "h1"

        broken = mock.Mock(return_value=mock.Mock(status="error", lstart="", command=""))
        with mock.patch("walkcode.channel_native._probe_process", broken):
            asyncio.run(transport._disconnect_client("h1", None))

        self.assertIn("h1", transport._worker_procs)  # kept for a later retry
        self.assertIn("sess-1", transport._session_last_worker)

    def test_prompt_exit_needs_no_signal(self):
        proc = self._spawn(seconds=1)
        transport = _transport()
        self._record(transport, "h1", proc.pid)

        with mock.patch("os.kill", wraps=os.kill) as spy:
            asyncio.run(transport._disconnect_client("h1", _FakeClient(proc.pid)))
            signalled = [c for c in spy.call_args_list if c.args[0] == proc.pid]
        self.assertEqual(signalled, [])  # died in the grace window, untouched
        self.assertEqual(_probe_process(proc.pid).status, "gone")

    def test_missing_record_is_a_noop(self):
        transport = _transport()
        started = time.monotonic()
        asyncio.run(transport._disconnect_client("h-unknown", _FakeClient(0)))
        self.assertLess(time.monotonic() - started, 1.0)


class ResumeBarrierTests(_WorkerCleanupBase):
    def test_resume_waits_for_lingering_worker_of_same_session(self):
        # EOF/settle already unregistered _session_handles while the old
        # worker still lives: resume must reach a terminal state for it
        # BEFORE spawning the replacement (review P1: the new worker would
        # otherwise hit the session lock / double-write next to it).
        proc = self._spawn()
        transport = _transport()
        self._record(transport, "h-old", proc.pid, session_id="sess-1")
        # _session_handles intentionally empty == already unregistered.

        new_client = _FakeClient(0)

        async def _fake_connect(client, prompt=None):
            return None

        with (
            mock.patch.object(ClaudeHeadlessTransport, "_available", return_value=True),
            mock.patch.object(
                ClaudeHeadlessTransport, "_create_client", return_value=(new_client, None)
            ),
            mock.patch.object(ClaudeHeadlessTransport, "_connect_client", _fake_connect),
        ):
            handle = asyncio.run(
                transport.resume(
                    ResumeSpec(
                        cwd="/tmp",
                        session_id="sess-1",
                        resume_ref={"agent_session_id": "agent-1"},
                    )
                )
            )

        self.assertTrue(handle.handle_id)
        self.assertEqual(_probe_process(proc.pid).status, "gone")  # barrier held
        self.assertNotIn("h-old", transport._worker_procs)


class WorkerPidExtractionTests(unittest.TestCase):
    def test_capture_records_pid_and_identity(self):
        base = _WorkerCleanupBase()
        # reuse spawn hygiene via a scratch TestCase instance
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            transport = _transport()
            asyncio.run(transport._capture_worker_proc("h1", "sess-9", _FakeClient(proc.pid)))
            record = transport._worker_procs.get("h1")
            self.assertIsNotNone(record)
            self.assertEqual(record[0], proc.pid)
            self.assertIn("sleep", record[2])
            self.assertEqual(transport._session_last_worker.get("sess-9"), "h1")
        finally:
            proc.kill()
            proc.wait(timeout=5)
        del base

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
