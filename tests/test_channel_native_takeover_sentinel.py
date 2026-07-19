"""ADR 0053: post-takeover TUI sentinel + pid identity hardening.

Root cause chain (2026-07-19 01:16 incident, fully evidenced):
- terminate_ref pid goes stale whenever hooks stop flowing (Ctrl+C'd TUI);
- killing a dead pid reported success ("already_exited" -> accepted);
- a live bare `claude` TUI (argv carries no session id) is invisible to every
  argv scan, survived the takeover, re-claimed the session and silently
  fenced out the fresh worker.

Deep-review 2026-07-19 hardening under test:
- three-state process probe (ok/gone/error): a probe error never fails open;
- kill-side identity check (command/lstart) so pid reuse never kills a stranger;
- session sweep is three-state (ok/error) and TUI-filtered (the freshly
  resumed `_bundled` SDK worker must be untouchable — revert 6c83ed9);
- ledger hygiene: a dead/reused terminate_ref is marked target_gone but stays
  armed, so a Ctrl+C'd TUI still takes the AUTOMATIC takeover path (cluster A);
- sentinel: while the orchestrator owns the writer lease, a FRESH activity
  hook from an external TUI reveals a takeover survivor -> verify identity,
  kill it, notify the channel; kill runs concurrently with a short timeout;
- claim flip requires fresh-or-live evidence AND must not predate the current
  owner (cluster B / concurrency#1);
- fresh_seconds + sentinel switch come from ChannelNativeConfig (cluster E).
"""

import asyncio
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from walkcode.channel_native import (
    ActorRef,
    ChannelBinding,
    ChannelNativeConfig,
    ControlResult,
    FakeAgentTransport,
    LocalProcessController,
    TransportCapabilities,
    _command_is_codex_app_server_process,
    _command_is_external_tui_process,
    _proc_identity_matches,
    _ProcProbe,
    _probe_process,
    _terminate_ref_session_id,
)
from walkcode import channel_native_runtime as runtime_module
from walkcode.channel_native_runtime import (
    ChannelNativeRuntime,
    _enrich_terminate_ref,
    _tui_hook_captured_age,
)

from tests.test_channel_native_runtime import _FakeTelegramApi, _transport_caps


SESSION_UUID = "98951f59-ef79-475e-a938-6bae92f14b28"


def _spawn_detached_sleep() -> int:
    """A live pid reparented to init, so the internal-headless ancestry guard
    (which walks the pid's real parent chain) does not see a `_bundled/claude`
    ancestor when this suite itself runs inside a walkcode worker."""
    spawn = subprocess.run(
        ["/bin/sh", "-c", "sleep 60 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return int(spawn.stdout.strip())


class _FakeSentinelController:
    def __init__(self, *, accepted: bool = True, state: str = "terminated", reason: str = ""):
        self.accepted = accepted
        self.state = state
        self.reason = reason
        self.terminate_calls: list[dict] = []

    async def terminate(self, ref, reason):
        self.terminate_calls.append({"ref": dict(ref), "reason": reason})
        if not self.accepted:
            return ControlResult(False, reason=self.reason or "sentinel_failed")
        return ControlResult(True, state=self.state)


def _make_runtime(tmp: str, env_extra: dict | None = None):
    env = {
        "WALKCODE_CHANNEL": "telegram",
        "TELEGRAM_BOT_TOKEN": "fake",
        "WALKCODE_AGENT": "claude",
        "TELEGRAM_ALLOWED_CHAT_IDS": "123",
        "WALKCODE_STATE_PATH": str(Path(tmp) / "state.json"),
        "WALKCODE_CWD": tmp,
    }
    if env_extra:
        env.update(env_extra)
    cfg = ChannelNativeConfig.from_env(env)
    api = _FakeTelegramApi()
    runtime = ChannelNativeRuntime.from_config(
        cfg,
        telegram_api=api,
        transports={"claude_headless": FakeAgentTransport("claude_headless", _transport_caps())},
    )
    return runtime, api


def _orchestrator_owned_session(runtime, tmp: str):
    return runtime.state.sessions.create_structured_session(
        binding=ChannelBinding(
            channel_kind="telegram",
            account_id="bot",
            chat_id="123",
            root_message_id="3",
        ),
        transport_kind="claude_headless",
        transport_ref={"handle_id": "h1", "agent_session_id": "claude-session-1"},
        cwd=tmp,
        owner=ActorRef("telegram", "456", "Ada"),
    )


def _activity_hook_payload(*, pid: int, command: str, lstart: str = "", age_seconds: float = 1.0) -> dict:
    return {
        "session_id": "claude-session-1",
        "tool_name": "Bash",
        "_walkcode_hook_process_tree_entries": [
            {"pid": pid, "ppid": 1, "lstart": lstart, "command": command},
        ],
        "_walkcode_hook_captured_at": time.time() - age_seconds,
    }


def _sent_texts(api) -> list[str]:
    return [str(payload.get("text", "")) for method, payload in api.calls if method == "sendMessage"]


class ProbeTests(unittest.TestCase):
    def test_probe_live_process_is_ok(self):
        pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        probe = _probe_process(pid)
        self.assertEqual(probe.status, "ok")
        self.assertTrue(probe.lstart)
        self.assertIn("sleep", probe.command)

    def test_probe_dead_pid_is_gone_not_error(self):
        dead = subprocess.Popen(["sleep", "0.01"])
        dead_pid = dead.pid
        dead.wait(timeout=2.0)
        self.assertEqual(_probe_process(dead_pid).status, "gone")

    def test_probe_exception_is_error_not_gone(self):
        import walkcode.channel_native as cn

        def boom(*a, **k):
            raise TimeoutError("ps timed out")

        with patch.object(cn.subprocess, "run", side_effect=boom):
            self.assertEqual(_probe_process(4242).status, "error")

    def test_probe_survives_day_first_locale(self):
        # Live regression 2026-07-19: LANG=en_SG.UTF-8 renders ps lstart as
        # "Sun 19 Jul ..." (day first), which the v0.14.3 parser rejected —
        # empty hook trees, probe errors, revival refused. ps must run with a
        # pinned C locale regardless of the inherited environment.
        pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        with patch.dict("os.environ", {"LANG": "en_SG.UTF-8", "LC_TIME": "en_SG.UTF-8", "LC_ALL": ""}):
            probe = _probe_process(pid)
        self.assertEqual(probe.status, "ok")
        # C-locale shape: Www Mmm DD — month before day.
        self.assertRegex(probe.lstart, r"^\w{3}\s+\w{3}\s+\d{1,2}\s")

    def test_process_tree_entries_survive_day_first_locale(self):
        from walkcode.channel_native_runtime import _process_tree_entries

        pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        with patch.dict("os.environ", {"LANG": "en_SG.UTF-8", "LC_TIME": "en_SG.UTF-8", "LC_ALL": ""}):
            entries = _process_tree_entries(pid, max_depth=1)
        self.assertTrue(entries, "hook-side tree capture must not be locale-sensitive")
        self.assertEqual(entries[0]["pid"], pid)
        self.assertTrue(entries[0]["lstart"])

    def test_probe_zombie_is_gone(self):
        import walkcode.channel_native as cn

        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 0, stdout="Z+   Sun Jul 19 10:00:00 2026 claude --resume x\n", stderr=""
            )

        with patch.object(cn.subprocess, "run", side_effect=fake_run):
            self.assertEqual(_probe_process(4242).status, "gone")

    def test_probe_parses_stat_prefixed_output(self):
        import walkcode.channel_native as cn

        for stat in ("S+", "SN", "R", "S"):
            with patch.object(
                cn.subprocess,
                "run",
                side_effect=lambda cmd, _s=stat, **k: subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{_s}   Sun Jul 19 10:00:00 2026 claude --resume x\n", stderr=""
                ),
            ):
                probe = _probe_process(4242)
            self.assertEqual(probe.status, "ok")
            self.assertEqual(probe.lstart, "Sun Jul 19 10:00:00 2026")
            self.assertEqual(probe.command, "claude --resume x")

    def test_identity_matches_semantics(self):
        probe = _ProcProbe("ok", "Sun Jul 19 10:00:00 2026", "claude --resume x")
        self.assertTrue(_proc_identity_matches(probe, "Sun Jul 19 10:00:00 2026", "claude --resume x"))
        self.assertFalse(_proc_identity_matches(probe, "Sun Jul 19 09:00:00 2026", "claude --resume x"))
        self.assertFalse(_proc_identity_matches(probe, "", "vim"))
        self.assertTrue(_proc_identity_matches(probe, "", ""))  # nothing to compare


class SentinelRemnantTerminationTests(unittest.TestCase):
    def _run_hook(self, runtime, payload, hook_type="PostToolUse"):
        return asyncio.run(
            runtime.process_tui_hook(hook_type=hook_type, agent="claude", payload=payload)
        )

    def test_fresh_activity_hook_kills_verified_remnant_and_notifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(pid=54321, command="claude", lstart="Sun Jul 19 10:20:02 2026")
            with (
                patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake),
                patch.object(
                    runtime_module,
                    "_probe_process",
                    lambda pid: _ProcProbe("ok", "Sun Jul 19 10:20:02 2026", "claude") if pid == 54321 else _ProcProbe("gone"),
                ),
            ):
                result = self._run_hook(runtime, payload)

            self.assertTrue(result.accepted)
            self.assertEqual(len(fake.terminate_calls), 1)
            ref = fake.terminate_calls[0]["ref"]
            self.assertEqual(ref["pid"], 54321)
            self.assertEqual(ref["command"], "claude")
            self.assertEqual(ref["lstart"], "Sun Jul 19 10:20:02 2026")
            self.assertTrue(ref["allow_terminate"])
            # Ownership must NOT flip: the orchestrator keeps the lease.
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "claude_headless")
            self.assertTrue(any("终端进程" in text for text in _sent_texts(api)))

    def test_stale_activity_hook_never_kills(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(pid=54321, command="claude", age_seconds=3600.0)
            with patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake):
                self._run_hook(runtime, payload)
            self.assertEqual(fake.terminate_calls, [])

    def test_sentinel_disabled_never_kills(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp, {"WALKCODE_TUI_SENTINEL_ENABLED": "0"})
            _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(pid=54321, command="claude")
            with patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake):
                self._run_hook(runtime, payload)
            self.assertEqual(fake.terminate_calls, [])

    def test_stopped_session_never_kills(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            session.status = "stopped"
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(pid=54321, command="claude")
            with patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake):
                self._run_hook(runtime, payload)
            self.assertEqual(fake.terminate_calls, [])

    def test_pid_identity_mismatch_via_controller_never_kills(self):
        # The captured command is `claude`; by kill time the pid runs vim.
        # The controller's _kill_one probes, sees the mismatch, and skips.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, api = _make_runtime(tmp)
            _orchestrator_owned_session(runtime, tmp)
            live_pid = _spawn_detached_sleep()
            self.addCleanup(lambda: subprocess.run(["kill", "-9", str(live_pid)], capture_output=True))
            payload = _activity_hook_payload(
                pid=live_pid, command="claude", lstart="Sun Jul 19 10:20:02 2026"
            )
            # Real controller with a real (mismatching) live process.
            self._run_hook(runtime, payload)
            # sleep process still alive: identity gate refused to kill it.
            self.assertEqual(_probe_process(live_pid).status, "ok")
            self.assertFalse(any("已清理" in t or "已终止" in t for t in _sent_texts(api)))

    def test_bundled_sdk_worker_is_never_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(
                pid=54321,
                command=f"/x/claude_agent_sdk/_bundled/claude --resume={SESSION_UUID}",
            )
            with patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake):
                self._run_hook(runtime, payload)
            self.assertEqual(fake.terminate_calls, [])

    def test_failed_kill_notifies_remnant_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, api = _make_runtime(tmp)
            _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController(accepted=False, reason="identity_probe_failed")
            payload = _activity_hook_payload(pid=54321, command="claude", lstart="Sun Jul 19 10:20:02 2026")
            with (
                patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake),
                patch.object(
                    runtime_module,
                    "_probe_process",
                    lambda pid: _ProcProbe("ok", "Sun Jul 19 10:20:02 2026", "claude") if pid == 54321 else _ProcProbe("gone"),
                ),
            ):
                self._run_hook(runtime, payload)
            self.assertEqual(len(fake.terminate_calls), 1)
            self.assertTrue(any("未能终止" in text for text in _sent_texts(api)))


class ClaimFreshnessTests(unittest.TestCase):
    def _run_hook(self, runtime, payload, hook_type="SessionStart"):
        return asyncio.run(
            runtime.process_tui_hook(hook_type=hook_type, agent="claude", payload=payload)
        )

    def _claim_payload(self, *, live_pid: int | None, age_seconds: float | None) -> dict:
        pid = live_pid if live_pid is not None else 54321
        payload = {
            "session_id": "claude-session-1",
            "_walkcode_hook_process_tree_entries": [
                {"pid": pid, "ppid": 1, "lstart": "", "command": "/usr/local/bin/claude"},
            ],
            "_walkcode_hook_process_tree": ["/usr/local/bin/claude"],
        }
        if age_seconds is not None:
            payload["_walkcode_hook_captured_at"] = time.time() - age_seconds
        return payload

    def test_stale_claim_with_dead_pid_cannot_steal_session(self):
        # The 01:16 incident tail: a replayed claim describing a dead pid.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            result = self._run_hook(
                runtime, self._claim_payload(live_pid=None, age_seconds=3600.0)
            )
            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")
            self.assertEqual(updated.transport_kind, "claude_headless")

    def test_fresh_claim_hands_back_with_explicit_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            # A real post-takeover resume is captured AFTER the owner acquired
            # the lease; model that so the predates-owner fence does not fire.
            payload = self._claim_payload(live_pid=None, age_seconds=1.0)
            payload["_walkcode_hook_captured_at"] = float(session.writer_owner.acquired_at) + 1.0
            result = self._run_hook(runtime, payload)
            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "external_tui")
            self.assertTrue(any("接回" in text for text in _sent_texts(api)))

    def test_claim_predating_owner_refused_even_if_live(self):
        # round-3 ordering rule: a claim captured BEFORE the current owner
        # acquired the lease must NOT flip it back, even if the old terminal is
        # still alive — that survivor is the sentinel's job. (Previously a live
        # TUI wrongly overrode the predates-owner fence.)
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            live_pid = _spawn_detached_sleep()
            self.addCleanup(lambda: subprocess.run(["kill", "-9", str(live_pid)], capture_output=True))
            payload = {
                "session_id": "claude-session-1",
                "_walkcode_hook_process_tree_entries": [
                    {"pid": live_pid, "ppid": 1, "lstart": "Sun Jul 19 10:20:02 2026", "command": "claude"},
                ],
                # captured BEFORE the owner acquired -> predates.
                "_walkcode_hook_captured_at": float(session.writer_owner.acquired_at) - 100.0,
            }
            real_probe = runtime_module._probe_process
            def fake_probe(pid):
                real = real_probe(pid)
                if pid == live_pid and real.status == "ok":
                    return _ProcProbe("ok", "Sun Jul 19 10:20:02 2026", "claude")
                return real
            with patch.object(runtime_module, "_probe_process", side_effect=fake_probe):
                result = self._run_hook(runtime, payload)
            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")

    def test_live_backed_claim_after_acquire_hands_back(self):
        # A live TUI backing a claim captured AFTER acquisition (a genuine
        # post-takeover resume) hands back even without a fresh stamp.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            # Pin the owner acquisition well in the past so the claim can be
            # after-acquire (not predating) yet old (not fresh) yet not-future.
            session.writer_owner.acquired_at = time.time() - 10_000.0
            live_pid = _spawn_detached_sleep()
            self.addCleanup(lambda: subprocess.run(["kill", "-9", str(live_pid)], capture_output=True))
            captured_at = float(session.writer_owner.acquired_at) + 10.0  # after acquire, ~now-9990 (stale)
            payload = {
                "session_id": "claude-session-1",
                "_walkcode_hook_process_tree_entries": [
                    {"pid": live_pid, "ppid": 1, "lstart": "Sun Jul 19 10:20:02 2026", "command": "claude"},
                ],
                "_walkcode_hook_captured_at": captured_at,
            }
            real_probe = runtime_module._probe_process

            def fake_probe(pid):
                real = real_probe(pid)
                if pid == live_pid and real.status == "ok":
                    return _ProcProbe("ok", "Sun Jul 19 10:20:02 2026", "claude")
                return real

            with patch.object(runtime_module, "_probe_process", side_effect=fake_probe):
                result = self._run_hook(runtime, payload)
            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "external_tui")

    def test_claim_predating_owner_is_refused(self):
        # concurrency#1: a claim captured BEFORE the current owner acquired the
        # lease must not flip it, even if fresh.
        with tempfile.TemporaryDirectory() as tmp:
            runtime, _api = _make_runtime(tmp)
            session = _orchestrator_owned_session(runtime, tmp)
            acquired_at = float(session.writer_owner.acquired_at)
            payload = self._claim_payload(live_pid=None, age_seconds=1.0)
            payload["_walkcode_hook_captured_at"] = acquired_at - 5.0  # before takeover
            result = self._run_hook(runtime, payload)
            self.assertTrue(result.accepted)
            updated = runtime.state.sessions.get(session.session_id)
            self.assertEqual(updated.writer_owner.kind, "orchestrator")


class TerminateIdentityTests(unittest.TestCase):
    def test_terminate_skips_live_pid_whose_command_changed(self):
        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=2.0)))
        controller = LocalProcessController(timeout=0.5)
        result = asyncio.run(
            controller.terminate(
                {
                    "pid": live.pid,
                    "command": f"claude --resume {SESSION_UUID}",
                    "lstart": "Sun Jul 19 10:20:02 2026",
                    "allow_terminate": True,
                },
                "test",
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, "already_exited")
        self.assertIsNone(live.poll())  # the stranger is untouched

    def test_terminate_refuses_on_probe_error(self):
        import walkcode.channel_native as cn

        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=2.0)))
        controller = LocalProcessController(timeout=0.5)
        with patch.object(cn, "_probe_process", return_value=_ProcProbe("error")):
            result = asyncio.run(
                controller.terminate(
                    {"pid": live.pid, "command": "claude", "allow_terminate": True},
                    "test",
                )
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "identity_probe_failed")
        self.assertIsNone(live.poll())  # not killed on probe error

    def test_terminate_kills_when_identity_matches(self):
        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=2.0)))
        probe = _probe_process(live.pid)
        self.assertEqual(probe.status, "ok")
        controller = LocalProcessController(timeout=2.0)
        result = asyncio.run(
            controller.terminate(
                {
                    "pid": live.pid,
                    "command": probe.command,
                    "lstart": probe.lstart,
                    "allow_terminate": True,
                },
                "test",
            )
        )
        self.assertTrue(result.accepted)
        self.assertIn(result.state, {"terminated", "killed"})
        live.wait(timeout=2.0)
        self.assertIsNotNone(live.poll())

    def test_terminate_ref_session_id_matches_resume_and_session_id(self):
        self.assertEqual(
            _terminate_ref_session_id({"command": f"claude --resume {SESSION_UUID}"}),
            SESSION_UUID,
        )
        self.assertEqual(
            _terminate_ref_session_id({"command": f"claude --resume={SESSION_UUID}"}),
            SESSION_UUID,
        )
        self.assertEqual(
            _terminate_ref_session_id({"command": f"claude --session-id {SESSION_UUID}"}),
            SESSION_UUID,
        )
        self.assertEqual(_terminate_ref_session_id({"command": "claude"}), "")

    def test_pids_for_session_ok_filters_out_sdk_workers(self):
        import walkcode.channel_native as cn

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, 0, stdout="101\n102\n", stderr="")
            return real_run(cmd, **kwargs)

        def fake_probe(pid):
            if pid == 101:
                return _ProcProbe("ok", "L1", f"/x/claude_agent_sdk/_bundled/claude --resume={SESSION_UUID}")
            if pid == 102:
                return _ProcProbe("ok", "L2", f"claude --resume {SESSION_UUID}")
            return _ProcProbe("gone")

        with (
            patch.object(cn.subprocess, "run", side_effect=fake_run),
            patch.object(cn, "_probe_process", side_effect=fake_probe),
        ):
            status, triples = LocalProcessController._pids_for_session(SESSION_UUID)
        self.assertEqual(status, "ok")
        self.assertEqual(triples, [(102, "L2", f"claude --resume {SESSION_UUID}")])

    def test_pids_for_session_reports_scan_error(self):
        import walkcode.channel_native as cn

        def boom(cmd, **kwargs):
            if cmd and cmd[0] == "pgrep":
                raise TimeoutError("pgrep timed out")
            return subprocess.run(cmd, **kwargs)

        with patch.object(cn.subprocess, "run", side_effect=boom):
            status, triples = LocalProcessController._pids_for_session(SESSION_UUID)
        self.assertEqual(status, "error")
        self.assertEqual(triples, [])

    def test_terminate_surfaces_scan_failure(self):
        import walkcode.channel_native as cn

        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=2.0)))
        # recorded pid probes gone (already dead), but the session sweep errors
        # -> must NOT report false success.
        def fake_probe(pid):
            return _ProcProbe("gone")

        with (
            patch.object(cn, "_probe_process", side_effect=fake_probe),
            patch.object(
                LocalProcessController,
                "_pids_for_session",
                staticmethod(lambda sid: ("error", [])),
            ),
        ):
            controller = LocalProcessController(timeout=0.5)
            result = asyncio.run(
                controller.terminate(
                    {
                        "pid": live.pid,
                        "command": f"claude --resume {SESSION_UUID}",
                        "allow_terminate": True,
                    },
                    "test",
                )
            )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "session_scan_failed")


class EnrichTerminateRefTests(unittest.TestCase):
    def _ref(self, *, pid: int, command: str = "") -> dict:
        process_ref = {"pid": pid, "allow_terminate": True}
        if command:
            process_ref["command"] = command
        return {"controller_kind": "process", "process_ref": process_ref}

    def test_dead_pid_marked_target_gone_but_stays_armed(self):
        # Cluster A: a Ctrl+C'd TUI must still take the AUTOMATIC takeover path,
        # not be downgraded to manual_only. So allow_terminate stays True.
        dead = subprocess.Popen(["sleep", "0.01"])
        dead_pid = dead.pid
        dead.wait(timeout=2.0)
        enriched = _enrich_terminate_ref(self._ref(pid=dead_pid, command="claude"))
        process_ref = enriched["process_ref"]
        self.assertTrue(process_ref["allow_terminate"])
        self.assertTrue(process_ref["target_gone"])
        self.assertEqual(process_ref["probe_state"], "gone")
        self.assertIn("recorded_at", process_ref)

    def test_live_matching_pid_gets_lstart(self):
        pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        probe = _probe_process(pid)
        enriched = _enrich_terminate_ref(self._ref(pid=pid, command=probe.command))
        process_ref = enriched["process_ref"]
        self.assertTrue(process_ref["allow_terminate"])
        self.assertEqual(process_ref.get("lstart"), probe.lstart)
        self.assertIn("recorded_at", process_ref)

    def test_probe_error_does_not_disarm(self):
        # Cluster C: a transient probe error must not mutate authorization.
        import walkcode.channel_native as cn

        # _enrich_terminate_ref lives in the runtime module and calls the
        # _probe_process imported into that namespace — patch it there.
        with patch.object(runtime_module, "_probe_process", return_value=_ProcProbe("error")):
            enriched = _enrich_terminate_ref(self._ref(pid=4242, command="claude"))
        process_ref = enriched["process_ref"]
        self.assertTrue(process_ref["allow_terminate"])
        self.assertNotIn("target_gone", process_ref)
        self.assertEqual(process_ref["probe_state"], "error")

    def test_live_pid_with_reused_command_marked_target_gone(self):
        pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(pid)], capture_output=True))
        enriched = _enrich_terminate_ref(
            self._ref(pid=pid, command=f"claude --resume {SESSION_UUID}")
        )
        process_ref = enriched["process_ref"]
        self.assertTrue(process_ref["target_gone"])
        self.assertTrue(process_ref["allow_terminate"])

    def test_none_passthrough(self):
        self.assertIsNone(_enrich_terminate_ref(None))


class TerminateTargetGoneAndWaitTests(unittest.TestCase):
    def test_target_gone_ref_never_signals_primary_pid(self):
        # round-2 Critical: target_gone must skip the recorded pid entirely so a
        # reused pid is never signalled; only the session sweep proceeds.
        live = subprocess.Popen(["sleep", "60"])
        self.addCleanup(lambda: (live.kill(), live.wait(timeout=2.0)))
        controller = LocalProcessController(timeout=0.5)
        result = asyncio.run(
            controller.terminate(
                {
                    "pid": live.pid,
                    "command": "claude",
                    "allow_terminate": True,
                    "target_gone": True,
                },
                "test",
            )
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, "already_exited")
        self.assertIsNone(live.poll())  # never signalled

    def test_target_gone_skips_primary_but_sweeps_workers(self):
        # round-3: target_gone must skip the primary pid AND still run the
        # session sweep (a reused primary pid must not abort the whole op).
        import walkcode.channel_native as cn

        killed: list[int] = []
        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, 0, stdout="7001\n", stderr="")
            return real_run(cmd, **kwargs)

        def fake_probe(pid):
            if pid == 7001:
                return _ProcProbe("ok", "L", f"claude --session-id {SESSION_UUID}")
            return _ProcProbe("gone")

        def fake_kill_one(self, pid, expected_lstart="", expected_command=""):
            killed.append(pid)
            return ControlResult(True, state="terminated")

        with (
            patch.object(cn.subprocess, "run", side_effect=fake_run),
            patch.object(cn, "_probe_process", side_effect=fake_probe),
            patch.object(LocalProcessController, "_kill_one", fake_kill_one),
        ):
            controller = LocalProcessController(timeout=0.5)
            result = asyncio.run(
                controller.terminate(
                    {
                        "pid": 6000,  # the dead/reused primary
                        "command": f"claude --resume {SESSION_UUID}",
                        "allow_terminate": True,
                        "target_gone": True,
                    },
                    "test",
                )
            )
        self.assertTrue(result.accepted)
        self.assertEqual(killed, [7001])  # only the swept worker, never 6000

    def test_wait_exited_error_is_not_exited(self):
        import walkcode.channel_native as cn

        controller = LocalProcessController(timeout=0.2, poll_interval=0.02)
        with patch.object(cn, "_probe_process", return_value=_ProcProbe("error")):
            self.assertFalse(controller._wait_exited(4242, "L", "claude"))

    def test_wait_exited_identity_change_counts_as_exited(self):
        import walkcode.channel_native as cn

        controller = LocalProcessController(timeout=0.5)
        with patch.object(
            cn, "_probe_process", return_value=_ProcProbe("ok", "NEW", "vim")
        ):
            # expected identity differs -> original target gone, pid reused.
            self.assertTrue(controller._wait_exited(4242, "OLD", "claude"))

    def test_pids_for_session_negative_returncode_is_error(self):
        import walkcode.channel_native as cn

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, -9, stdout="", stderr="")
            return real_run(cmd, **kwargs)

        with patch.object(cn.subprocess, "run", side_effect=fake_run):
            status, triples = LocalProcessController._pids_for_session(SESSION_UUID)
        self.assertEqual(status, "error")

    def test_pids_for_session_drops_other_session_pid(self):
        # round-2 concurrency#2: a pid reused by ANOTHER session's claude
        # between pgrep and probe must be excluded.
        import walkcode.channel_native as cn

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            if cmd and cmd[0] == "pgrep":
                return subprocess.CompletedProcess(cmd, 0, stdout="202\n", stderr="")
            return real_run(cmd, **kwargs)

        with (
            patch.object(cn.subprocess, "run", side_effect=fake_run),
            patch.object(
                cn,
                "_probe_process",
                lambda pid: _ProcProbe("ok", "L", "claude --resume 00000000-1111-2222-3333-444444444444"),
            ),
        ):
            status, triples = LocalProcessController._pids_for_session(SESSION_UUID)
        self.assertEqual(status, "ok")
        self.assertEqual(triples, [])  # different session id -> dropped


class LiveTuiIdentityTests(unittest.TestCase):
    def test_reused_pid_is_not_a_live_tui(self):
        # round-2 Critical: pid reuse must not read as a live TUI. The captured
        # command was claude; the pid now runs vim -> not live.
        from walkcode.channel_native_runtime import _tui_hook_has_live_tui_process

        live_pid = _spawn_detached_sleep()
        self.addCleanup(lambda: subprocess.run(["kill", "-9", str(live_pid)], capture_output=True))
        payload = {
            "_walkcode_hook_process_tree_entries": [
                {"pid": live_pid, "ppid": 1, "lstart": "Sun Jul 19 10:20:02 2026", "command": "claude"},
            ],
        }
        real_probe = runtime_module._probe_process

        def fake_probe(pid):
            real = real_probe(pid)
            if pid == live_pid and real.status == "ok":
                return _ProcProbe("ok", "Sun Jul 19 11:00:00 2026", "vim notes.txt")
            return real

        with patch.object(runtime_module, "_probe_process", side_effect=fake_probe):
            self.assertFalse(_tui_hook_has_live_tui_process("claude_headless", payload))


class SentinelDisabledTests(unittest.TestCase):
    def test_disabled_sentinel_notifies_but_does_not_kill(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime, api = _make_runtime(tmp, {"WALKCODE_TUI_SENTINEL_ENABLED": "0"})
            _orchestrator_owned_session(runtime, tmp)
            fake = _FakeSentinelController()
            payload = _activity_hook_payload(pid=54321, command="claude", lstart="Sun Jul 19 10:20:02 2026")
            with patch.object(ChannelNativeRuntime, "_sentinel_process_controller", return_value=fake):
                asyncio.run(
                    runtime.process_tui_hook(hook_type="PostToolUse", agent="claude", payload=payload)
                )
            self.assertEqual(fake.terminate_calls, [])
            self.assertTrue(any("哨兵已关闭" in t for t in _sent_texts(api)))


class ConfigValidationTests(unittest.TestCase):
    def _cfg(self, value):
        return ChannelNativeConfig.from_env(
            {
                "WALKCODE_CHANNEL": "telegram",
                "TELEGRAM_BOT_TOKEN": "fake",
                "WALKCODE_AGENT": "claude",
                "TELEGRAM_ALLOWED_CHAT_IDS": "123",
                "WALKCODE_STATE_PATH": "/tmp/x.json",
                "WALKCODE_CWD": "/tmp",
                "WALKCODE_TUI_HOOK_FRESH_SECONDS": value,
            }
        )

    def test_inf_fresh_seconds_rejected(self):
        from walkcode.channel_native import ChannelConfigError

        with self.assertRaises(ChannelConfigError):
            self._cfg("inf")

    def test_negative_fresh_seconds_rejected(self):
        from walkcode.channel_native import ChannelConfigError

        with self.assertRaises(ChannelConfigError):
            self._cfg("-5")

    def test_valid_fresh_seconds(self):
        self.assertEqual(self._cfg("30").tui_hook_fresh_seconds, 30.0)


class CapturedAgeTests(unittest.TestCase):
    def test_future_stamp_is_unknown_not_fresh(self):
        self.assertIsNone(_tui_hook_captured_age({"_walkcode_hook_captured_at": time.time() + 3600}))

    def test_nan_stamp_is_unknown(self):
        self.assertIsNone(_tui_hook_captured_age({"_walkcode_hook_captured_at": float("nan")}))

    def test_recent_stamp_is_small_age(self):
        age = _tui_hook_captured_age({"_walkcode_hook_captured_at": time.time() - 2.0})
        self.assertIsNotNone(age)
        self.assertLess(age, 10.0)


class CommandClassifierTests(unittest.TestCase):
    def test_bare_claude_is_external_tui(self):
        self.assertTrue(_command_is_external_tui_process("claude"))
        self.assertTrue(_command_is_external_tui_process(f"claude --resume {SESSION_UUID}"))

    def test_bundled_sdk_worker_is_not_external_tui(self):
        self.assertFalse(
            _command_is_external_tui_process(
                f"/x/claude_agent_sdk/_bundled/claude --resume={SESSION_UUID}"
            )
        )

    def test_codex_app_server_daemon_is_internal(self):
        # Cluster F: the managed daemon form must classify as internal so the
        # sentinel never SIGTERMs walkcode's own Codex service.
        self.assertTrue(_command_is_codex_app_server_process("codex app-server daemon"))
        self.assertTrue(_command_is_codex_app_server_process("codex app-server daemon start"))
        self.assertTrue(_command_is_codex_app_server_process("codex app-server --stdio"))
        self.assertTrue(_command_is_codex_app_server_process("/opt/homebrew/bin/codex app-server daemon"))
        self.assertFalse(_command_is_external_tui_process("codex app-server daemon"))

    def test_codex_tui_with_app_server_in_args_is_not_internal(self):
        # round-2: a bare substring test misclassified a real Codex TUI whose
        # prompt merely mentions app-server. It must remain an external TUI.
        self.assertFalse(_command_is_codex_app_server_process('codex "explain app-server startup"'))
        self.assertTrue(_command_is_external_tui_process('codex "explain app-server startup"'))


if __name__ == "__main__":
    unittest.main()
