"""Terminal injection via tmux send-keys."""

import itertools
import logging
import os
import subprocess
import threading
import time

from .i18n import t

logger = logging.getLogger("walkcode")

# Seconds to wait between delivering message text and pressing Enter. codex CLI
# >=0.136 detects paste bursts: if the text and the Enter arrive in one stdin
# read, the Enter is treated as a newline INSIDE the paste instead of a submit.
# Bracketed paste (below) already separates them on apps that enable the mode;
# this delay is the fallback for apps that don't.
_INJECT_ENTER_DELAY = 0.1
# Prefix for the per-call tmux paste buffer. tmux buffers live on the (shared)
# tmux server, so a single fixed name is a global mutable slot: two concurrent
# injects — different worker threads, or the separate claude/codex bot processes
# driving the same user's tmux server — would race on set-buffer/paste-buffer
# and could paste one session's text into another (cross-session mixup) or drop
# a message. Each inject gets a unique buffer name instead. Avoids clobbering the
# user's default paste buffer; deleted right after the paste (`paste-buffer -d`).
_INJECT_BUFFER = "walkcode-inject"
_inject_seq = itertools.count()


def _unique_inject_buffer() -> str:
    """A buffer name unique per call across processes and threads."""
    return f"{_INJECT_BUFFER}-{os.getpid()}-{threading.get_ident()}-{next(_inject_seq)}"


def detect_tmux_session() -> str:
    """Return the tmux session name of the current environment, or empty string.

    Targets the inherited ``$TMUX_PANE`` when present so the reported session
    matches the pane the owner gate decides on (a bare ``display-message`` would
    resolve to the active pane, which can differ from this hook's pane).
    """
    if not os.environ.get("TMUX"):
        return ""
    cmd = ["tmux", "display-message", "-p", "#{session_name}"]
    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        cmd = ["tmux", "display-message", "-t", tmux_pane, "-p", "#{session_name}"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _ctty_owns_pane(my_ctty: str, pane_tty: str) -> bool:
    """Pure predicate: does controlling-tty ``my_ctty`` (as ``ps -o tty=`` prints
    it, e.g. ``ttys047`` or ``??``) belong to tmux ``pane_tty`` (``/dev/ttys047``)?
    """
    if not my_ctty or my_ctty in ("?", "??"):
        return False
    return bool(pane_tty) and pane_tty.endswith("/" + my_ctty)


def _proc_info(pid: int) -> tuple[int, str, str] | None:
    """Return ``(ppid, ctty, comm)`` for ``pid`` via ``ps``, or None on failure.

    ``ctty`` is the controlling terminal as ``ps -o tty=`` prints it (``ttys053``
    or ``??``). ``comm`` is the executable basename, lower-cased (``ps -o comm=``
    prints either a bare name like ``claude`` or a full path like ``/bin/zsh``).
    """
    try:
        r = subprocess.run(
            ["ps", "-o", "ppid=,tty=,comm=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    line = r.stdout.strip()
    if not line:
        return None
    # ppid<sp>tty<sp>comm — comm (the path) may itself contain spaces, so keep it
    # as the remainder after the first two whitespace-separated fields.
    parts = line.split(None, 2)
    if len(parts) < 2:
        return None
    try:
        ppid = int(parts[0])
    except ValueError:
        return None
    ctty = parts[1]
    # ps prints a login shell as "-zsh"/"-bash"; strip the leading dash so it
    # still matches _SHELLS (else the pane's login shell looks like an agent and
    # a main hook launched from it would be wrongly dropped).
    comm = os.path.basename(parts[2]).lower().lstrip("-") if len(parts) > 2 else ""
    return ppid, ctty, comm


def _pane_identity() -> tuple[str, str] | None:
    """Return ``(pane_tty, pane_pid)`` of the tmux pane this process belongs to,
    or None if the probe is unusable (caller fails open).

    The pane is targeted explicitly by the inherited ``$TMUX_PANE`` so we read the
    hook's OWN pane, not whatever pane happens to be active in the client (a bare
    ``display-message`` resolves to the active pane, which drifts as the user
    switches focus). ``pane_pid`` is the pid tmux spawned for the pane — the pane's
    foreground process (``tmux new-session "claude …"`` makes claude the pane
    process). A missing or non-numeric ``pane_pid`` is treated as an unusable probe
    (None → fail open) rather than silently falling back to a weaker test: the
    pane_pid anchor is what lets a detached main hook (no ctty) be recognised.
    """
    tmux_pane = os.environ.get("TMUX_PANE")
    if not tmux_pane:
        return None  # can't identify our own pane → caller fails open
    cmd = ["tmux", "display-message", "-t", tmux_pane, "-p", "#{pane_tty}\t#{pane_pid}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return None
    pane_tty, _, pane_pid = out.partition("\t")
    pane_tty = pane_tty.strip()
    pane_pid = pane_pid.strip()
    if not pane_tty or not pane_pid.isdigit():
        return None
    return pane_tty, pane_pid


# Process names treated as transparent when walking up to the firing agent: the
# hook runs under a shell (`sh -c "afplay … & walkcode hook …"`), so the agent is
# the shell's parent, not the shell. Shared with is_agent_alive below.
_SHELLS = {"zsh", "bash", "sh", "fish", "dash", "csh", "tcsh", "ksh"}


def _first_agent_ancestor(start_pid: int, max_hops: int) -> tuple[int, str, int] | None:
    """Walk up from ``start_pid``'s parent, skipping shells; return
    ``(pid, ctty, ppid)`` of the first non-shell ancestor — the process that
    spawned the hook's shell, i.e. the agent that fired the hook. None if the
    chain breaks / cycles / hits init / exceeds ``max_hops`` (indeterminate)."""
    self_info = _proc_info(start_pid)
    if self_info is None:
        return None
    pid = self_info[0]  # the hook's parent (its shell); self is skipped
    seen: set[int] = set()
    for _ in range(max_hops):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        info = _proc_info(pid)
        if info is None:
            return None
        ppid, ctty, comm = info
        if comm in _SHELLS:
            pid = ppid
            continue
        return pid, ctty, ppid
    return None


def _ancestor_owns_pane(start_pid: int, pane_tty: str, pane_pid: str,
                        max_hops: int = 16) -> bool | None:
    """Decide pane ownership by walking up from ``start_pid`` (the hook process).

    Returns True (owner), False (foreign / nested), or None (indeterminate → the
    caller fails open). ``pane_pid`` is the authoritative anchor — tmux's pane
    process — which is what makes this robust to agents that spawn hooks *detached*
    (claude >=2.1.x gives its hooks no controlling terminal, ctty ``??``, so the
    hook's own ctty can't be compared).

    Decision:
      1. Find the firing agent = first non-shell ancestor of the hook.
      2. agent IS ``pane_pid``  → owner (the pane runs the agent directly; covers
         the detached-hook case where ctty is useless).
      3. agent is NOT ``pane_pid`` → it depends on what ``pane_pid`` is:
         - ``pane_pid`` is itself an agent (non-shell): the firing agent is a
           DIFFERENT non-shell process under it → a nested sub-agent → NOT owner.
           (This closes the same-terminal hijack: a sub-agent that inherits the
           pane's ctty must not pass just because ctty matches.)
         - ``pane_pid`` is a shell: the pane runs a shell that launched the agent.
           The legitimate foreground agent is the FIRST non-shell under that shell.
           Owner iff the firing agent reaches ``pane_pid`` through only shells AND
           shares the pane terminal; another agent in between → nested → NOT owner.
    """
    agent = _first_agent_ancestor(start_pid, max_hops)
    if agent is None:
        return None  # indeterminate → fail open
    agent_pid, agent_ctty, agent_ppid = agent
    if str(agent_pid) == pane_pid:
        return True
    # The firing agent is not the pane process. Classify by pane_pid's nature.
    if not pane_pid.isdigit():
        return None  # no usable anchor → fail open
    pane_info = _proc_info(int(pane_pid))
    if pane_info is None:
        return None  # pane process gone/unreadable → fail open
    if pane_info[2] not in _SHELLS:
        # pane_pid is itself an agent; a different non-shell descendant is nested.
        return False
    # pane_pid is a shell. The legitimate foreground agent shares the pane
    # terminal, so a firing agent whose ctty is NOT the pane's is foreign — decide
    # that NOW, before walking ancestry. This closes the orphan bypass: a nested
    # sub-agent that detaches (double-fork → reparented to init) would otherwise
    # break the ancestry walk into an indeterminate (None → fail-open) result.
    if not _ctty_owns_pane(agent_ctty, pane_tty):
        return False
    # ctty matches; confirm the firing agent is the pane shell's direct foreground
    # agent (only shells between them) — another agent in between → nested.
    pid = agent_ppid
    seen: set[int] = {agent_pid}
    for _ in range(max_hops):
        if str(pid) == pane_pid:
            return True  # reached the pane shell through shells only; ctty already ok
        if pid <= 1:
            # ctty matched but the chain to the pane shell is severed: the firing
            # agent was reparented to init, i.e. it detached. A real foreground
            # agent is never orphaned from its own pane process, so this is a
            # detached nested sub-agent → foreign. (Only genuine probe errors below
            # fail open; a structural orphan does not.)
            return False
        if pid in seen:
            return None  # cycle (corrupt read) → fail open
        seen.add(pid)
        info = _proc_info(pid)
        if info is None:
            return None  # transient ps failure → fail open (don't drop a real hook)
        ppid, _ctty, comm = info
        if comm not in _SHELLS:
            return False  # another agent between candidate and pane shell → nested
        pid = ppid
    return None  # exceeded max_hops → fail open


def owner_check() -> tuple[bool, str]:
    """Decide pane ownership and return ``(is_owner, reason)``.

    ``reason`` is a short stable tag for diagnostics (logged on drops and
    fail-opens): ``disabled`` / ``not_tmux`` / ``owner`` / ``non_owner`` /
    ``failopen:pane_probe`` / ``failopen:indeterminate``. Identity comes from the
    pane tmux reports for the inherited ``$TMUX_PANE`` — its ``pane_pid`` (the pane
    process) and ``pane_tty`` — anchored on ``pane_pid`` rather than the hook's own
    controlling terminal, since agents that spawn hooks detached leave them with no
    ctty (the bug the earlier "hook ctty == pane_tty" test caused).

    Fail-open: any indeterminate result returns ``True`` so a real owner's hooks
    are never wrongly suppressed. Set ``WALKCODE_OWNER_CHECK=0`` to disable the
    gate entirely.
    """
    if os.environ.get("WALKCODE_OWNER_CHECK", "1") == "0":
        return True, "disabled"
    if not os.environ.get("TMUX"):
        return True, "not_tmux"  # not under tmux → the gate doesn't apply
    pane = _pane_identity()
    if pane is None:
        return True, "failopen:pane_probe"  # can't identify our pane → fail open
    pane_tty, pane_pid = pane
    verdict = _ancestor_owns_pane(os.getpid(), pane_tty, pane_pid)
    if verdict is True:
        return True, "owner"
    if verdict is False:
        return False, "non_owner"
    return True, "failopen:indeterminate"  # ancestry probe inconclusive → fail open


def is_tmux_pane_owner() -> bool:
    """True if this hook was fired by the foreground agent of its tmux pane — the
    pane's real owner — not a nested child that merely inherited ``$TMUX`` (e.g. a
    deep-review sub-agent running in the parent agent's background terminal). Thin
    boolean wrapper over :func:`owner_check`."""
    return owner_check()[0]


def validate_target(session_name: str) -> str | None:
    """Check if tmux session exists. Returns error message or None."""
    if not session_name:
        return t("tty.no_session")
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return t("tty.not_found", name=session_name)
    except FileNotFoundError:
        return t("tty.not_installed")
    except Exception as e:
        return t("tty.check_failed", error=e)
    return None


def get_session_activity(session_name: str) -> float | None:
    """Return the epoch timestamp of last activity in a tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p", "#{window_activity}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def kill_session(session_name: str) -> bool:
    """Kill a tmux session. Returns True on success."""
    try:
        result = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_agent_alive(session_name: str) -> bool:
    """Check if an agent process (not a shell) is running in the tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            cmd = result.stdout.strip().split("\n")[0]
            return cmd not in _SHELLS and cmd != ""
    except Exception:
        pass
    return False


def capture_pane(session_name: str, lines: int = 30) -> str:
    """Capture last N lines of tmux pane output. Returns empty string on failure."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


# Resume readiness: how long to wait for a (re)launched agent's TUI to settle
# before injecting, how long the pane must stay unchanged to count as "ready",
# and how often to poll. See wait_until_input_ready for the rationale.
_READY_TIMEOUT = 120.0   # absolute cap (huge sessions take a while to re-render)
_READY_STABLE = 2.0      # pane unchanged this long ⇒ render/compaction finished
_READY_POLL = 0.5        # s between pane captures


def wait_until_input_ready(
    session_name: str,
    timeout: float = _READY_TIMEOUT,
    stable_for: float = _READY_STABLE,
    poll: float = _READY_POLL,
) -> bool:
    """Block until a (re)launched agent's TUI can accept a submitted prompt.

    Resuming a session replays/re-renders its whole history (and may auto-compact
    at 100% context) before the input prompt appears. Injecting during that
    window lands text in a not-yet-ready TUI: the paste may stick in the box but
    the Enter is dropped, so nothing is submitted — the bug that made a freshly
    resumed session report "not delivered". A fixed sleep can't cover this: small
    sessions are ready in ~1s, a maxed-out one takes a minute-plus.

    We detect readiness structurally rather than guessing a delay: the agent
    process is running AND the pane has stopped changing. While history replays
    or a spinner runs, the screen keeps repainting; once it lands on the idle
    input prompt it goes static (verified: idle Claude/Codex panes are byte-stable
    across captures, no footer animation).

    Returns True once readiness is observed, False on timeout — in which case the
    caller should inject anyway as a last resort.
    """
    deadline = time.time() + timeout
    prev: str | None = None
    stable_since: float | None = None
    while time.time() < deadline:
        if not is_agent_alive(session_name):
            # Process not up yet (or already gone): reset and keep waiting.
            prev, stable_since = None, None
            time.sleep(poll)
            continue
        snap = capture_pane(session_name, lines=40)
        if snap.strip() and snap == prev:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= stable_for:
                return True
        else:
            prev, stable_since = snap, None
        time.sleep(poll)
    return False


def inject(session_name: str, text: str, enter: bool | None = None,
           menu_key: bool = False) -> bool:
    """Inject text into a tmux session via send-keys.

    ``menu_key`` declares the caller's intent, which inject used to guess from
    content (and got wrong for chat messages that happen to be a single char):

      * ``menu_key=False`` (default) — ``text`` is a chat message. It is
        delivered via bracketed paste and always submitted with Enter, even when
        it is a single character like "2" or "y". Sniffing single chars as menu
        keys is exactly what left a Feishu reply of "2" sitting unsubmitted in
        the input box.
      * ``menu_key=True`` — ``text`` is a single-keystroke menu/permission
        selection (e.g. "2" picks option 2). It goes in as a raw ``send-keys -l``
        keystroke so the TUI menu reads it as a choice, not as typed text. The
        only such caller is the permission hook-timeout fallback.

    Returns True on success. Raises RuntimeError on failure.
    """
    error = validate_target(session_name)
    if error:
        raise RuntimeError(error)

    if enter is None:
        # Chat messages are always submitted; the menu fallback passes enter
        # explicitly when it needs it.
        enter = not menu_key

    if menu_key:
        # Use send-keys -l (literal) to avoid tmux key binding interpretation
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")
    else:
        # A chat message is delivered via bracketed paste so codex >=0.136 sees
        # an unambiguous paste boundary and treats the trailing Enter as a submit
        # rather than a newline inside a paste burst.
        # set-buffer into a per-call buffer, then paste-buffer -p (bracketed paste
        # when the app requested the mode) and -d (delete the buffer after).
        buf = _unique_inject_buffer()
        result = subprocess.run(
            ["tmux", "set-buffer", "-b", buf, "--", text],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux set-buffer failed: {result.stderr.strip()}")
        try:
            result = subprocess.run(
                ["tmux", "paste-buffer", "-p", "-d", "-b", buf, "-t", session_name],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                raise RuntimeError(f"tmux paste-buffer failed: {result.stderr.strip()}")
        except BaseException:
            # paste-buffer -d deletes the buffer on success; if we got here it may
            # not have, so don't leak the per-call buffer on the tmux server.
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buf],
                capture_output=True, text=True, timeout=5,
            )
            raise

    if enter:
        if not menu_key:
            # Let codex flush the paste before the Enter arrives as its own key.
            time.sleep(_INJECT_ENTER_DELAY)
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys Enter failed: {result.stderr.strip()}")

    return True
