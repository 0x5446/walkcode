"""TTY input injection for macOS Terminal.app via AppleScript."""

import os
import stat
import subprocess
from datetime import datetime

# Single-key replies that should NOT have a newline appended
SINGLE_KEYS = {"y", "n", "a", "1", "2", "3", "4", "5", "6", "7", "8", "9", "0"}


def _ps_field(pid: int | str, field: str) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _normalize_lstart(value: str | None) -> str | None:
    if not value:
        return None
    compact = " ".join(value.split())
    for fmt in ("%a %b %d %H:%M:%S %Y", "%a %d %b %H:%M:%S %Y"):
        try:
            return datetime.strptime(compact, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    return compact


def _friendly_applescript_error(stderr: str) -> str:
    if "not allowed to send keystrokes" in stderr or "(1002)" in stderr:
        return (
            "macOS 拒绝键盘注入：请在 系统设置 > 隐私与安全性 > 辅助功能 中，"
            "给启动 cbuddy serve 的宿主应用授权（例如 Terminal.app、iTerm、Codex），"
            "然后重启该应用和 cbuddy serve"
        )
    return f"AppleScript failed: {stderr.strip()}"


def _title_escape_sequence(title: str) -> str:
    safe = title.replace("\x1b", "").replace("\x07", "")
    return f"\033]0;{safe}\007\033]1;{safe}\007\033]2;{safe}\007"


def detect_terminal_binding(start_pid: int | None = None, max_depth: int = 5) -> tuple[str, int | None, str | None]:
    """Walk up the process tree and keep the oldest ancestor bound to a real TTY."""
    pid = str(start_pid or os.getppid())
    candidate = ("", None, None)
    for _ in range(max_depth):
        tty = _ps_field(pid, "tty")
        if tty and tty != "??":
            started_at = _normalize_lstart(_ps_field(pid, "lstart"))
            candidate = (f"/dev/{tty}", int(pid), started_at)
        pid = _ps_field(pid, "ppid")
        if not pid:
            break
    return candidate


def inspect_tty_owner(pid: int | None, started_at: str | None) -> tuple[str, str | None]:
    """Validate a stored TTY owner fingerprint and return its live TTY.

    Returns a status string and the current TTY when available.
    Statuses:
      - "ok": pid/start fingerprint still matches and TTY is live
      - "missing_fingerprint": pid/start not available
      - "process_missing": pid no longer exists
      - "process_reused": pid exists but start time changed
      - "tty_missing": process exists but no real TTY is attached
    """
    if not pid or not started_at:
        return "missing_fingerprint", None

    current_started_at = _normalize_lstart(_ps_field(pid, "lstart"))
    if not current_started_at:
        return "process_missing", None
    if current_started_at != _normalize_lstart(started_at):
        return "process_reused", None

    current_tty = _ps_field(pid, "tty")
    if not current_tty or current_tty == "??":
        return "tty_missing", None
    return "ok", f"/dev/{current_tty}"


def validate_tty(tty_path: str) -> str | None:
    """Check TTY exists and is owned by current user. Returns error message or None."""
    if not os.path.exists(tty_path):
        return f"TTY {tty_path} does not exist (terminal closed?)"
    try:
        st = os.stat(tty_path)
    except OSError as e:
        return f"Cannot stat {tty_path}: {e}"
    if not stat.S_ISCHR(st.st_mode):
        return f"{tty_path} is not a character device"
    if st.st_uid != os.getuid():
        return f"{tty_path} is not owned by current user"
    return None


def set_terminal_title(tty_path: str, title: str):
    """Write OSC title sequences directly to the target TTY."""
    error = validate_tty(tty_path)
    if error:
        raise RuntimeError(error)

    with open(tty_path, "w", encoding="utf-8", errors="ignore") as tty_device:
        tty_device.write(_title_escape_sequence(title))
        tty_device.flush()


def _escape_for_applescript(text: str) -> str:
    """Escape text for use in AppleScript string literals."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def inject(tty_path: str, text: str, enter: bool | None = None) -> bool:
    """Inject text into a terminal via clipboard paste + Cmd-V.

    Returns True if the terminal content changed after injection (verified),
    False if injection was attempted but could not be verified.

    Raises RuntimeError if the TTY is invalid or tab not found.
    """
    error = validate_tty(tty_path)
    if error:
        raise RuntimeError(error)

    if enter is None:
        enter = text.strip().lower() not in SINGLE_KEYS

    escaped_text = _escape_for_applescript(text)
    escaped_tty = _escape_for_applescript(tty_path)

    enter_line = 'keystroke return' if enter else ''

    # Clipboard paste (Cmd+V) is more reliable than keystroke which sends
    # char-by-char UI events that can be lost during window focus transitions.
    # After injection, compare terminal contents to verify delivery.
    script = f'''
tell application "Terminal"
    activate
    repeat with w in windows
        repeat with t in tabs of w
            if tty of t is "{escaped_tty}" then
                set index of w to 1
                set selected tab of w to t
                delay 0.5

                -- snapshot before injection
                set contentBefore to contents of t

                tell application "System Events"
                    set prevClip to ""
                    try
                        set prevClip to the clipboard as text
                    end try
                    set the clipboard to "{escaped_text}"
                    tell process "Terminal"
                        keystroke "v" using command down
                        delay 0.15
                        {enter_line}
                    end tell
                    delay 0.1
                    try
                        set the clipboard to prevClip
                    end try
                end tell

                -- verify: wait then check if content changed
                delay 0.8
                set contentAfter to contents of t
                if contentAfter is not equal to contentBefore then
                    return "verified"
                else
                    return "unverified"
                end if
            end if
        end repeat
    end repeat
    return "tty_not_found"
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=20,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        raise RuntimeError(_friendly_applescript_error(result.stderr))
    if output == "tty_not_found":
        raise RuntimeError(
            f"No Terminal.app tab found with TTY {tty_path}"
        )
    return output == "verified"
