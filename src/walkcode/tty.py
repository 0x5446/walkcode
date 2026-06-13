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
