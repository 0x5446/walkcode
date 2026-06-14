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
    """Return the tmux session name of the current environment, or empty string."""
    if not os.environ.get("TMUX"):
        return ""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{session_name}"],
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
    comm = os.path.basename(parts[2]).lower() if len(parts) > 2 else ""
    return ppid, ctty, comm


def _pane_identity() -> tuple[str, str] | None:
    """Return ``(pane_tty, pane_pid)`` of the tmux pane this process belongs to
    (resolved via the inherited ``$TMUX_PANE``), or None if the probe fails.

    ``pane_pid`` is the pid tmux spawned for the pane — i.e. the pane's foreground
    agent itself (``tmux new-session "claude …"`` makes claude the pane process).
    Returned as a string; "" when tmux reports no pid.
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#{pane_tty}\t#{pane_pid}"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    if not out:
        return None
    pane_tty, _, pane_pid = out.partition("\t")
    pane_tty = pane_tty.strip()
    if not pane_tty:
        return None
    return pane_tty, pane_pid.strip()


# Process names treated as transparent when walking up to the firing agent: the
# hook runs under a shell (`sh -c "afplay … & walkcode hook …"`), so the agent is
# the shell's parent, not the shell.
_SHELLS = {"zsh", "bash", "sh", "fish", "dash", "csh", "tcsh", "ksh"}


def _ancestor_owns_pane(start_pid: int, pane_tty: str, pane_pid: str,
                        max_hops: int = 16) -> bool | None:
    """Decide pane ownership by walking up from ``start_pid`` (the hook process).

    The hook's *firing agent* is its nearest ancestor that is not a shell — the
    process that spawned the hook's ``sh -c``. That agent owns the pane iff it IS
    the pane's process (``pane_pid``) or its controlling terminal is the pane's
    (``pane_tty``). Anything else is a nested child (a sub-agent in its own/absent
    terminal) and must be dropped.

    Returns True (owner), False (foreign), or None (indeterminate → fail open).
    Anchoring on ``pane_pid`` is what makes this robust to agents that spawn hooks
    *detached*: claude (>=2.1.x) gives its hook processes no controlling terminal
    (ctty ``??``), so the hook's own ctty can no longer be compared — but the
    agent is still ``pane_pid``. A nested sub-agent, even one that likewise has no
    ctty, is a descendant of the pane agent and so is never ``pane_pid`` itself.
    """
    self_info = _proc_info(start_pid)
    if self_info is None:
        return None  # can't even read self → indeterminate
    pid = self_info[0]  # start at the hook's parent (the shell); skip self
    seen: set[int] = set()
    for _ in range(max_hops):
        if pid <= 1 or pid in seen:
            return None  # reached init / cycle without finding an agent
        seen.add(pid)
        info = _proc_info(pid)
        if info is None:
            return None  # ancestry broke (reparented mid-walk) → fail open
        ppid, ctty, comm = info
        if comm in _SHELLS:
            pid = ppid
            continue  # shells are transparent
        # First non-shell ancestor = the agent that fired this hook.
        if pane_pid and str(pid) == pane_pid:
            return True
        return _ctty_owns_pane(ctty, pane_tty)
    return None


def is_tmux_pane_owner() -> bool:
    """True if this hook was fired by the foreground agent of its tmux pane — the
    pane's real owner — not a nested child that merely inherited ``$TMUX`` (e.g. a
    deep-review sub-agent running in the parent agent's background terminal).

    Identity is decided from the pane tmux reports for the inherited
    ``$TMUX_PANE``: its ``pane_pid`` (the agent process itself) and ``pane_tty``.
    We walk up from the hook to its firing agent (the nearest non-shell ancestor)
    and confirm that agent is ``pane_pid``, or controls ``pane_tty``. See
    :func:`_ancestor_owns_pane` for why ``pane_pid`` — not the hook's own ctty — is
    the anchor: agents that spawn hooks detached leave them with no controlling
    terminal, so the earlier "hook ctty == pane_tty" test dropped every legitimate
    hook.

    Fail-open: if anything is indeterminate, return True so a real owner's hooks
    are never wrongly suppressed. Set ``WALKCODE_OWNER_CHECK=0`` to disable the
    gate entirely.
    """
    if os.environ.get("WALKCODE_OWNER_CHECK", "1") == "0":
        return True
    if not os.environ.get("TMUX"):
        return True  # not under tmux → the gate doesn't apply
    pane = _pane_identity()
    if pane is None:
        return True  # probe failed/empty → fail open
    pane_tty, pane_pid = pane
    verdict = _ancestor_owns_pane(os.getpid(), pane_tty, pane_pid)
    if verdict is None:
        return True  # indeterminate → fail open
    return verdict


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


_SHELLS = {"zsh", "bash", "sh", "fish", "dash", "csh", "tcsh", "ksh"}


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
    caller should inject anyway as a last resort and let delivery confirmation
    (the UserPromptSubmit hook) report the real outcome.
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
