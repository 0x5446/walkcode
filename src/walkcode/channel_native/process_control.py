"""Local process probing and control for external TUIs and headless workers."""

from __future__ import annotations

import asyncio
import calendar
import json
import os
import re
import shlex
import signal
import subprocess
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import ControlResult, _log_degrade


# Both real-world TUI attach forms carry the session id on argv:
#   claude --session-id <id>   (daemon-managed session worker)
#   claude --resume <id>       (interactive resume from the shell)
# A bare `claude` + in-TUI /resume carries NO id and can never be found by
# argv scanning — that gap is closed by the post-takeover hook sentinel in
# channel_native_runtime, not by widening this regex further.
_TERMINATE_SESSION_ID_RE = re.compile(r"--(?:session-id|resume)[ =]([0-9a-fA-F-]{8,64})")


def _terminate_ref_session_id(ref: dict[str, Any]) -> str:
    match = _TERMINATE_SESSION_ID_RE.search(str(ref.get("command", "") or ""))
    return match.group(1) if match else ""


def _command_executable_basename(command: str) -> str:
    try:
        parts = shlex.split(str(command or ""))
    except ValueError:
        parts = str(command or "").split()
    if not parts:
        return ""
    return Path(parts[0]).name


def _command_is_claude_headless_sdk_process(command: str) -> bool:
    value = str(command or "")
    if not value:
        return False
    if "claude_agent_sdk" in value and "_bundled/claude" in value:
        return True
    return "claude" in value and "--input-format stream-json" in value and "--output-format stream-json" in value


def _command_is_claude_tui_process(command: str) -> bool:
    value = str(command or "")
    if not value or _command_is_claude_headless_sdk_process(value):
        return False
    return _command_executable_basename(value) in {"claude", "claude-code"}


# terminate_ref controller kind for a TUI served by a shared codex app-server
# daemon: the daemon owns the thread, so takeover attaches instead of killing.
SHARED_APP_SERVER_CONTROLLER = "shared_app_server"


def _command_is_codex_app_server_process(command: str) -> bool:
    # Any `codex app-server ...` form is an app-server, never a user TUI: the
    # stdio client uses `--stdio`, the managed daemon uses `app-server daemon
    # [start]`. Since codex 0.157 the managed daemon also serves the user's TUI,
    # so this is NOT "WalkCode's own session" (hooks are attributed by turn id,
    # ADR 0064); it only guarantees an app-server is never killed as a TUI. Match on the PARSED subcommand, not a bare
    # substring — `codex "explain app-server"` is a real user TUI and a bare
    # substring test misclassified it as internal (round-2 review), hiding its
    # hooks. Matching only `--stdio` (the original) missed the daemon form and
    # let the sentinel SIGTERM walkcode's own Codex service (round-1 cluster F).
    value = str(command or "")
    if _command_executable_basename(value) != "codex":
        return False
    try:
        parts = shlex.split(value)
    except ValueError:
        parts = value.split()
    return len(parts) >= 2 and parts[1] == "app-server"


def _command_is_codex_tui_process(command: str) -> bool:
    value = str(command or "")
    if not value or _command_is_codex_app_server_process(value):
        return False
    return _command_executable_basename(value) == "codex"


def _command_is_external_tui_process(command: str) -> bool:
    return _command_is_claude_tui_process(command) or _command_is_codex_tui_process(command)


@dataclass(frozen=True)
class _ProcProbe:
    """Three-state result of probing a pid's identity via `ps`.

    Distinguishing "gone" from "error" is load-bearing (deep-review 2026-07-19
    cluster C): collapsing a `ps` timeout / permission error / parse failure
    into "process gone" is fail-open — it either kills a reused pid or disarms
    a still-valid ledger entry. Callers MUST branch on all three states.

    status:
      - "ok":    process exists; `lstart` + `command` carry its identity
      - "gone":  `ps` ran cleanly and the pid does not exist
      - "error": probe could not be completed (timeout / exec error / unparsable)
    """

    status: str  # "ok" | "gone" | "error"
    lstart: str = ""
    command: str = ""


def _c_locale_env() -> dict[str, str]:
    """Env for ps/pgrep children with a pinned C locale.

    `ps` renders lstart per LC_TIME: en_SG (and most European locales) puts the
    day before the month ("Sun 19 Jul ..."), which broke every lstart parse in
    v0.14.3 (hooks inherit the terminal's locale) — empty process trees, probe
    errors, revival refused, sentinel blind. Live incident 2026-07-19 evening.
    """
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    return env


def _probe_process(pid: int) -> _ProcProbe:
    if pid <= 1:
        return _ProcProbe("gone")
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=,lstart=,command=", "-p", str(pid)],
            env=_c_locale_env(),
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return _ProcProbe("error")
    if result.returncode != 0:
        # macOS/BSD `ps -p <pid>` exits 1 when the pid is absent — that is a
        # clean "gone". Higher exit codes mean ps itself failed → "error".
        return _ProcProbe("gone") if result.returncode == 1 else _ProcProbe("error")
    line = result.stdout.strip("\n")
    if not line.strip():
        return _ProcProbe("gone")
    # stat lstart(Www Mmm dd HH:MM:SS yyyy) command
    match = re.match(
        r"^\s*(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+[\d:]{8}\s+\d{4})\s+(.*)$",
        line,
        flags=re.DOTALL,
    )
    if match is None:
        # Non-empty output we cannot parse is an error, not a "gone": failing
        # open here would let a reused pid pass the identity gate.
        return _ProcProbe("error")
    stat = match.group(1)
    # A zombie/defunct process is effectively gone: its pid lingers only until
    # the parent reaps it. Treating it as "ok" would make _wait_exited spin
    # until timeout on a process we just killed (it becomes a zombie first).
    if stat.startswith("Z"):
        return _ProcProbe("gone")
    return _ProcProbe("ok", match.group(2).strip(), match.group(3).strip())


def claude_tui_current_session(pid: int, lstart: str) -> str:
    """The session a live Claude TUI process is running right now; "" if unknown (ADR 0067).

    Claude Code keeps ``<config dir>/sessions/<pid>.json`` with the current
    ``sessionId`` and the process start time (``procStart``) — updated when
    ``/clear`` or ``/resume`` switches sessions inside the same process. The
    start time must match, so a reused pid never answers for another process.
    """
    wanted = _local_lstart_epochs(lstart)
    if pid <= 1 or not wanted:
        return ""
    home = Path.home()
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR", ""), str(home / ".claude")]
    try:
        dirs.extend(str(child) for child in sorted((home / ".claude-profiles").iterdir()))
    except OSError:
        pass  # no (readable) profiles dir: the other locations still answer
    for config_dir in dict.fromkeys(d for d in dirs if d):
        try:
            record = json.loads((Path(config_dir) / "sessions" / f"{pid}.json").read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        proc_start, session_id = record.get("procStart"), record.get("sessionId")
        if not isinstance(proc_start, str) or not isinstance(session_id, str) or not session_id.strip():
            continue  # a malformed record answers nothing
        # procStart is `ps lstart` rendered in UTC; our record is local time.
        started = _utc_lstart_epoch(proc_start)
        if started is not None and any(abs(started - epoch) < 1.0 for epoch in wanted):
            return session_id.strip()
    return ""


_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def _local_lstart_epochs(text: str) -> set[float]:
    """Epochs a C-locale ``ps -o lstart`` local-time string can mean (empty if unparsable).

    Usually one; two inside a DST fall-back hour, where the same wall time
    occurs twice — both are tried rather than letting mktime guess.
    """
    try:
        parsed = time.strptime(" ".join(str(text or "").split()), _LSTART_FORMAT)
    except ValueError:
        return set()
    epochs = set()
    for isdst in (0, 1):
        try:
            epoch = time.mktime((*parsed[:8], isdst))
        except (OverflowError, ValueError):
            continue
        # mktime "fixes up" a wrong isdst by shifting the hour; keep only
        # readings that really are this wall time.
        if time.localtime(epoch)[:6] == parsed[:6]:
            epochs.add(epoch)
    return epochs


def _utc_lstart_epoch(text: str) -> float | None:
    """Epoch of the same format read as UTC; None if unparsable."""
    try:
        return float(calendar.timegm(time.strptime(" ".join(str(text or "").split()), _LSTART_FORMAT)))
    except (ValueError, OverflowError):
        return None


def _claude_process_moved_to_another_session(pid: int, lstart: str, expected_session: str) -> bool:
    """Right before a signal: does this Claude process now run a session other than ``expected``?"""
    if not expected_session:
        return False
    current = claude_tui_current_session(pid, lstart)
    if current and current != expected_session:
        _log_degrade("terminate_skipped_switched_session", pid=pid, expected=expected_session, current=current)
        return True
    return False


def _probe_processes(pids: list[int]) -> dict[int, _ProcProbe] | None:
    """One ``ps`` for many pids: pid -> ok/gone probe; None when ps itself failed.

    macOS ``ps -p a,b,c`` prints one line per live pid and nothing for absent
    ones; it exits 0 if any pid exists and 1 if none does. Anything else —
    timeout, a higher exit code, a line we cannot parse — is a failed probe
    for the whole batch, never a "gone" (ADR 0066).
    """
    wanted = sorted({pid for pid in pids if pid > 1})
    if not wanted:
        return {}
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(str(pid) for pid in wanted)],
            env=_c_locale_env(),
            capture_output=True,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    if result.returncode not in (0, 1):
        return None
    probes = {pid: _ProcProbe("gone") for pid in wanted}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^\s*(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+[\d:]{8}\s+\d{4})\s*$", line)
        if match is None:
            return None
        pid = int(match.group(1))
        if pid in probes and not match.group(2).startswith("Z"):
            probes[pid] = _ProcProbe("ok", match.group(3).strip())
    return probes


def _proc_identity_matches(probe: _ProcProbe, expected_lstart: str, expected_command: str) -> bool:
    """True if a live probe matches the recorded identity.

    Empty expected fields are treated as "nothing to compare on that axis".
    With no recorded identity at all, returns True (liveness-only; the caller
    has already established the process is live).
    """
    expected_lstart = str(expected_lstart or "").strip()
    expected_command = str(expected_command or "").strip()
    if not expected_lstart and not expected_command:
        return True
    if expected_lstart and expected_lstart != probe.lstart:
        return False
    if expected_command and expected_command != probe.command:
        return False
    return True


class LocalProcessController:
    kind = "process"

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        poll_interval: float = 0.05,
        kill_after_timeout: bool = True,
    ):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.kill_after_timeout = kill_after_timeout

    async def terminate(self, ref: dict[str, Any], reason: str) -> ControlResult:
        return await asyncio.to_thread(self._terminate_sync, ref, reason)

    def _terminate_sync(self, ref: dict[str, Any], _reason: str) -> ControlResult:
        try:
            pid = int(ref.get("pid", 0) or 0)
        except (TypeError, ValueError):
            return ControlResult(False, "invalid_pid")
        if not bool(ref.get("allow_terminate")):
            return ControlResult(False, "termination_not_authorized")
        target_gone = bool(ref.get("target_gone"))
        # Validate the primary pid up front on the non-target_gone path — before
        # the scan — so a malformed ref still returns invalid_pid rather than a
        # scan/auth error (round-3 verify: error-priority contract). target_gone
        # deliberately skips this: its pid is known-dead and only the sweep runs.
        if not target_gone and (pid <= 1 or pid == os.getpid()):
            return ControlResult(False, "invalid_pid")
        # Claude Code >= 2.1.2xx runs TUI sessions as daemon-managed workers:
        # the hook-recorded pid is often just the pty host, and the session
        # keeps running in a `--session-id <id>` worker. Unless every process
        # of the session dies, headless resume is refused with "currently
        # running as a background agent". Scan FIRST: a scan failure must abort
        # before any signal, or we kill the terminal then report failure and
        # the takeover rolls back its fresh worker — leaving the user with no
        # writer at all (round-2 cluster: Rollback).
        session_id = _terminate_ref_session_id(ref)
        if session_id:
            status, triples = self._pids_for_session(session_id)
            if status == "error":
                return ControlResult(False, "session_scan_failed")
        else:
            triples = []
        # Each target is (pid, expected_lstart, expected_command). Identity
        # travels all the way to the signal so pid reuse never kills a stranger
        # (cluster D). The recorded ref supplies its own capture-time identity;
        # the sweep supplies scan-time identity per pid.
        targets: list[tuple[int, str, str]] = []
        if not target_gone:
            # Primary pid already validated above. target_gone deliberately
            # contributes no primary target — its pid is known dead/reused, so
            # only the session sweep proceeds (round-3 EdgeState: a dead pid
            # reused by the runtime itself must not abort the sweep).
            targets.append((pid, str(ref.get("lstart", "") or ""), str(ref.get("command", "") or "")))
        # Exclude the recorded (gone/reused) primary pid from the sweep too, so
        # target_gone never routes a signal to it by another path.
        triples = [t for t in triples if not (target_gone and t[0] == pid)]
        targets.extend(triples)
        seen: set[int] = set()
        deduped: list[tuple[int, str, str]] = []
        for tpid, ls, cmd in targets:
            if tpid <= 1 or tpid == os.getpid() or tpid in seen:
                continue
            seen.add(tpid)
            deduped.append((tpid, ls, cmd))
        final_state = "already_exited"
        expected_session = str(ref.get("expected_claude_session", "") or "")
        for tpid, ls, cmd in deduped:
            result = self._kill_one(tpid, ls, cmd, expected_session=expected_session)
            if not result.accepted:
                return result
            if result.state != "already_exited":
                final_state = result.state
        return ControlResult(True, state=final_state)

    def _kill_one(
        self, pid: int, expected_lstart: str = "", expected_command: str = "", *, expected_session: str = ""
    ) -> ControlResult:
        probe = _probe_process(pid)
        if probe.status == "gone":
            return ControlResult(True, state="already_exited")
        if probe.status == "error":
            # Probe failure is not proof of death: refuse to signal rather than
            # fail open onto a possibly-reused pid (deep-review cluster C).
            _log_degrade("terminate_identity_probe_failed", pid=pid, phase="pre_sigterm")
            return ControlResult(False, "identity_probe_failed")
        if not _proc_identity_matches(probe, expected_lstart, expected_command):
            # Live pid, but no longer the process we recorded → the original
            # target already exited and the pid was reused. Do not kill it.
            _log_degrade(
                "terminate_stale_pid_skipped",
                pid=pid,
                recorded_command=expected_command,
                current_command=probe.command,
            )
            return ControlResult(True, state="already_exited")
        if _claude_process_moved_to_another_session(pid, expected_lstart, expected_session):
            return ControlResult(True, state="switched_away")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return ControlResult(True, state="already_exited")
        except PermissionError:
            return ControlResult(False, "permission_denied")
        except OSError as exc:
            return ControlResult(False, str(exc))
        if self._wait_exited(pid, expected_lstart, expected_command):
            return ControlResult(True, state="terminated")
        if not self.kill_after_timeout:
            return ControlResult(False, "process_still_running")
        # Re-verify identity before the harder SIGKILL: the wait window is
        # exactly when the pid could have been reused by an unrelated process.
        probe2 = _probe_process(pid)
        if probe2.status == "gone":
            return ControlResult(True, state="terminated")
        if probe2.status == "error":
            _log_degrade("terminate_identity_probe_failed", pid=pid, phase="pre_sigkill")
            return ControlResult(False, "process_still_running")
        if not _proc_identity_matches(probe2, expected_lstart, expected_command):
            # Our target died during the wait and the pid was reused; the
            # original is gone, which is what we wanted.
            return ControlResult(True, state="terminated")
        if _claude_process_moved_to_another_session(pid, expected_lstart, expected_session):
            return ControlResult(True, state="switched_away")
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return ControlResult(True, state="terminated")
        except PermissionError:
            return ControlResult(False, "permission_denied")
        except OSError as exc:
            return ControlResult(False, str(exc))
        if self._wait_exited(pid, expected_lstart, expected_command):
            return ControlResult(True, state="killed")
        return ControlResult(False, "process_still_running")

    @staticmethod
    def _pids_for_session(session_id: str) -> tuple[str, list[tuple[int, str, str]]]:
        """Return ("ok"|"error", [(pid, lstart, command), ...]).

        "error" means the pgrep scan itself failed (timeout / exec error) and
        the caller must NOT treat the empty result as "no survivors". A clean
        scan with zero matches is ("ok", []).
        """
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"(session-id|resume)[= ]{session_id}"],
                env=_c_locale_env(),
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return "error", []
        # pgrep exit: 0 = matches, 1 = no matches. Anything else — including a
        # NEGATIVE code when pgrep was signal-killed — is a scan error, not
        # "no survivors" (round-2: negative rc fell through `> 1` as ok).
        if result.returncode not in (0, 1):
            return "error", []
        pids: list[int] = []
        for token in result.stdout.split():
            try:
                pids.append(int(token))
            except ValueError:
                continue
        # Keep only genuine external TUI processes still bound to THIS session.
        # The pgrep pattern also matches walkcode's own SDK workers
        # (`_bundled/claude --resume=<id>`) — including the worker the takeover
        # flow just resumed (resume runs BEFORE terminate by design; killing it
        # was the v0.14.2 regression that forced revert 6c83ed9). And between
        # pgrep and probe a pid can be reused by ANOTHER session's claude, so we
        # re-extract the session id from the live command and require it match
        # (round-2 concurrency#2).
        safe: list[tuple[int, str, str]] = []
        scan_error = False
        for candidate in pids:
            if candidate <= 1 or candidate == os.getpid():
                continue
            probe = _probe_process(candidate)
            if probe.status == "error":
                # Could not classify this pid — do not silently drop it, or a
                # survivor we failed to probe reads as "no survivors".
                scan_error = True
                continue
            if probe.status != "ok":
                continue
            if not _command_is_external_tui_process(probe.command):
                continue
            if _terminate_ref_session_id({"command": probe.command}) != session_id:
                continue
            safe.append((candidate, probe.lstart, probe.command))
        return ("error" if scan_error else "ok"), safe

    def _wait_exited(self, pid: int, expected_lstart: str = "", expected_command: str = "") -> bool:
        """True once the target is provably gone.

        Three-state (round-2 cluster: false-success): a probe ERROR is NOT
        "exited" — that would report a still-live terminal as terminated. Only
        a clean "gone", or a live pid whose identity no longer matches (the
        target died and the pid was reused), counts as exited.
        """
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            probe = _probe_process(pid)
            if probe.status == "gone":
                return True
            if probe.status == "ok" and not _proc_identity_matches(
                probe, expected_lstart, expected_command
            ):
                return True
            time.sleep(self.poll_interval)
        return False
