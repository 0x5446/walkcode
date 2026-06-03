"""Terminal injection via tmux send-keys."""

import logging
import os
import subprocess
import time

from .i18n import t

logger = logging.getLogger("walkcode")

# Single-key replies that should NOT have a newline appended
SINGLE_KEYS = {"y", "n", "a", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}

# Seconds to wait between delivering message text and pressing Enter. codex CLI
# >=0.136 detects paste bursts: if the text and the Enter arrive in one stdin
# read, the Enter is treated as a newline INSIDE the paste instead of a submit.
# Bracketed paste (below) already separates them on apps that enable the mode;
# this delay is the fallback for apps that don't.
_INJECT_ENTER_DELAY = 0.1
# Named tmux buffer for message delivery — avoids clobbering the user's default
# paste buffer; deleted right after the paste (`paste-buffer -d`).
_INJECT_BUFFER = "walkcode-inject"


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


def inject(session_name: str, text: str, enter: bool | None = None) -> bool:
    """Inject text into a tmux session via send-keys.

    Returns True on success. Raises RuntimeError on failure.
    """
    error = validate_target(session_name)
    if error:
        raise RuntimeError(error)

    stripped = text.strip()
    if enter is None:
        # For multi-character text, always send Enter
        # SINGLE_KEYS logic only applies to single-char replies (y/n/1-9/0)
        enter = len(stripped) > 1 or stripped.lower() not in SINGLE_KEYS

    # A single-key reply (y/n/1-9) is a MENU selection, not a message — it must
    # go in as a raw keystroke (a permission menu reads "1" as "pick option 1",
    # not as text). Everything else is a chat message: deliver it via bracketed
    # paste so codex >=0.136 sees an unambiguous paste boundary and treats the
    # trailing Enter as a submit rather than a newline inside a paste burst.
    is_menu_key = len(stripped) == 1 and stripped.lower() in SINGLE_KEYS

    if is_menu_key:
        # Use send-keys -l (literal) to avoid tmux key binding interpretation
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")
    else:
        # set-buffer into a named buffer, then paste-buffer -p (bracketed paste
        # when the app requested the mode) and -d (delete the buffer after).
        result = subprocess.run(
            ["tmux", "set-buffer", "-b", _INJECT_BUFFER, "--", text],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux set-buffer failed: {result.stderr.strip()}")
        result = subprocess.run(
            ["tmux", "paste-buffer", "-p", "-d", "-b", _INJECT_BUFFER, "-t", session_name],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux paste-buffer failed: {result.stderr.strip()}")

    if enter:
        if not is_menu_key:
            # Let codex flush the paste before the Enter arrives as its own key.
            time.sleep(_INJECT_ENTER_DELAY)
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys Enter failed: {result.stderr.strip()}")

    return True
