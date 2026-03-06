"""Terminal injection via tmux send-keys."""

import os
import subprocess

# Single-key replies that should NOT have a newline appended
SINGLE_KEYS = {"y", "n", "a", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}


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
        return "No tmux session specified"
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return f"tmux session '{session_name}' not found (Claude exited?)"
    except FileNotFoundError:
        return "tmux is not installed"
    except Exception as e:
        return f"tmux check failed: {e}"
    return None


def inject(session_name: str, text: str, enter: bool | None = None) -> bool:
    """Inject text into a tmux session via send-keys.

    Returns True on success. Raises RuntimeError on failure.
    """
    error = validate_target(session_name)
    if error:
        raise RuntimeError(error)

    if enter is None:
        enter = text.strip().lower() not in SINGLE_KEYS

    # Use send-keys -l (literal) to avoid tmux key binding interpretation
    result = subprocess.run(
        ["tmux", "send-keys", "-t", session_name, "-l", text],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tmux send-keys failed: {result.stderr.strip()}")

    if enter:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"tmux send-keys Enter failed: {result.stderr.strip()}")

    return True
