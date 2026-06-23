"""Read-only session statistics collection for the health card.

Pure functions, no network, no mutation — each ``collect_*`` returns a frozen
``SessionStats`` snapshot built from an agent's on-disk session files. Everything
here parses **undocumented internal formats** (Claude transcript JSONL, Codex
rollout JSONL, Codex ``state_*.sqlite``), so every field access is guarded and any
failure degrades to ``source="unavailable"`` rather than raising — the poller that
calls this must never crash on a malformed or half-written file.

Token accounting is normalized so Claude and Codex read the same way:
``input`` = fresh (non-cached) input tokens, ``cache`` = cached/reused input
tokens, ``output`` = generated tokens (incl. reasoning). Claude is genuinely
multi-model per session (model can switch mid-session) so tokens are grouped by
``message.model``; Codex is single-model per session.
"""

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("walkcode.stats")

# A session that has parsed a transcript/rollout larger than this is reported as
# source="unavailable" rather than burning the poller on a multi-MB full parse
# (rollouts have been seen at 8MB+). Tunable; the card then shows "stats
# unavailable" instead of blocking the 60s loop.
_MAX_PARSE_BYTES = 12 * 1024 * 1024

# A codex session id is a plain token; reject glob metacharacters before it
# reaches rglob so a crafted value can't widen the match to another rollout.
_CODEX_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ModelTokens:
    model: str
    input: int = 0
    output: int = 0
    cache: int = 0


@dataclass(frozen=True)
class SessionStats:
    """One snapshot of a session's health metrics. ``source`` is ok|partial|
    unavailable — partial means some fields were derivable but others (e.g. the
    token split) were missing."""
    title: str | None = None
    per_model: tuple[ModelTokens, ...] = ()
    duration_minutes: int = 0
    input_rounds: int = 0
    last_error: str | None = None
    source: str = "unavailable"


def _unavailable() -> SessionStats:
    return SessionStats(source="unavailable")


# --- shared helpers (moved here from __main__; re-exported there) -------------

def _is_user_turn_start(rec: dict) -> bool:
    """True if a Claude transcript ``user`` record is a real prompt (turn
    boundary), not a tool_result echo. Tool results are also ``user`` records but
    carry a ``tool_result`` block; a genuine prompt is a plain string or a
    text/image array."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _find_codex_rollout(session_id: str) -> str:
    """Locate a Codex rollout JSONL by session id.

    Named ``rollout-<ts>-<session_id>.jsonl`` under ``~/.codex/sessions/<Y>/<M>/
    <D>/``. ``session_id`` is validated to a plain token first (no glob meta) and
    matched on the exact ``-<session_id>.jsonl`` suffix. Returns newest match or "".
    """
    if not session_id or not _CODEX_SESSION_ID_RE.fullmatch(session_id):
        return ""
    base = Path.home() / ".codex" / "sessions"
    try:
        matches = list(base.rglob(f"rollout-*-{session_id}.jsonl"))
    except OSError:
        return ""
    if not matches:
        return ""
    try:
        return str(max(matches, key=lambda p: p.stat().st_mtime))
    except OSError:
        return str(matches[0])


def _find_claude_transcript(session_id: str) -> str:
    """Locate a Claude transcript JSONL by session id.

    Stored at ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``. The
    encoded-cwd dir is lossy/ambiguous, so DON'T reverse the cwd — glob by the
    globally-unique session_id across all project dirs and take the newest mtime.
    """
    if not session_id or not _CODEX_SESSION_ID_RE.fullmatch(session_id):
        return ""
    base = Path.home() / ".claude" / "projects"
    try:
        matches = list(base.glob(f"*/{session_id}.jsonl"))
    except OSError:
        return ""
    if not matches:
        return ""
    try:
        return str(max(matches, key=lambda p: p.stat().st_mtime))
    except OSError:
        return str(matches[0])


def _too_big(path: Path) -> bool:
    try:
        return path.stat().st_size > _MAX_PARSE_BYTES
    except OSError:
        return False


def _parse_ts(value) -> float | None:
    """Parse a transcript/rollout timestamp (ISO8601 string or epoch number) to
    epoch seconds. Returns None if unparseable."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        v = value.strip()
        # ISO8601, tolerate trailing Z
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def _duration_minutes(first_ts: float | None, last_ts: float | None) -> int:
    if first_ts is None or last_ts is None or last_ts < first_ts:
        return 0
    return int((last_ts - first_ts) // 60)


# --- Claude ------------------------------------------------------------------

def collect_claude_stats(session_id: str, cwd: str = "") -> SessionStats:
    """Full parse of a Claude transcript → SessionStats. cwd is accepted for
    signature symmetry but not used to locate the file (glob by session_id)."""
    path = _find_claude_transcript(session_id)
    if not path:
        return _unavailable()
    p = Path(path)
    if _too_big(p):
        logger.info("claude transcript too big for stats: %s", path)
        return _unavailable()
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return _unavailable()

    by_model: dict[str, dict] = {}
    title: str | None = None
    input_rounds = 0
    first_ts: float | None = None
    last_ts: float | None = None
    last_error: str | None = None
    first_user_msg = ""

    for line in lines:
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        rtype = rec.get("type")

        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        if rtype == "ai-title":
            t = rec.get("aiTitle")
            if isinstance(t, str) and t.strip():
                title = t.strip()  # keep last
            continue

        if rtype == "user":
            if not rec.get("isMeta") and not rec.get("isSidechain") and _is_user_turn_start(rec):
                input_rounds += 1
                if not first_user_msg:
                    c = rec.get("message", {}).get("content")
                    if isinstance(c, str):
                        first_user_msg = c
                    elif isinstance(c, list):
                        first_user_msg = " ".join(
                            b.get("text", "") for b in c
                            if isinstance(b, dict) and b.get("type") == "text")
            continue

        if rtype != "assistant":
            continue
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        # A trailing API error surfaces as an assistant record flagged
        # isApiErrorMessage — the one conservative ERROR signal we trust.
        if rec.get("isApiErrorMessage"):
            content = msg.get("content")
            if isinstance(content, list):
                txt = " ".join(b.get("text", "") for b in content
                               if isinstance(b, dict) and b.get("type") == "text")
                last_error = (txt or "API error").strip()[:200]
            else:
                last_error = "API error"
        else:
            last_error = None  # a later non-error assistant turn clears it
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        model = msg.get("model") or "unknown"
        agg = by_model.setdefault(model, {"input": 0, "output": 0, "cache": 0})
        agg["input"] += int(usage.get("input_tokens") or 0)
        agg["output"] += int(usage.get("output_tokens") or 0)
        agg["cache"] += (int(usage.get("cache_creation_input_tokens") or 0)
                         + int(usage.get("cache_read_input_tokens") or 0))

    per_model = tuple(
        ModelTokens(model=m, input=v["input"], output=v["output"], cache=v["cache"])
        for m, v in by_model.items()
    )
    # Prefer Claude's auto ai-title; fall back to the first user message (truncated)
    # so the card header always shows what the task is about.
    final_title = title or (first_user_msg.strip()[:40] or None)
    return SessionStats(
        title=final_title,
        per_model=per_model,
        duration_minutes=_duration_minutes(first_ts, last_ts),
        input_rounds=input_rounds,
        last_error=last_error,
        source="ok" if per_model else "partial",
    )


# --- Codex -------------------------------------------------------------------

def _open_codex_db() -> sqlite3.Connection | None:
    """Open the newest ~/.codex/state_*.sqlite read-only (mode=ro, so codex's
    live writer is never blocked). Returns None if absent/unreadable."""
    base = Path.home() / ".codex"
    try:
        dbs = sorted(base.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for db in dbs:
        try:
            return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            continue
    return None


def _codex_thread_row(session_id: str) -> dict:
    """Fetch the threads-table row for session_id (title/model/rollout_path/
    timestamps). Returns {} on any failure — caller falls back to the rollout."""
    conn = _open_codex_db()
    if conn is None:
        return {}
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT title, first_user_message, model, rollout_path, created_at, updated_at "
            "FROM threads WHERE id = ? LIMIT 1",
            (session_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    except sqlite3.Error:
        return {}
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def collect_codex_stats(session_id: str) -> SessionStats:
    """Codex stats from rollout token_count (in/out/cache split) + state sqlite
    (title/model/timestamps). Codex is single-model per session."""
    row = _codex_thread_row(session_id)
    rollout = row.get("rollout_path") or _find_codex_rollout(session_id)

    title = None
    t = row.get("title") or row.get("first_user_message")
    if isinstance(t, str) and t.strip():
        title = t.strip().splitlines()[0][:80]  # first line, capped
    model = row.get("model") if isinstance(row.get("model"), str) else None

    tok = None
    input_rounds = 0
    first_ts = _parse_ts(row.get("created_at"))
    last_ts = _parse_ts(row.get("updated_at"))
    last_error: str | None = None

    if rollout:
        rp = Path(rollout)
        if _too_big(rp):
            logger.info("codex rollout too big for stats: %s", rollout)
        else:
            try:
                lines = rp.read_text().splitlines()
            except OSError:
                lines = []
            for line in lines:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                ts = _parse_ts(rec.get("timestamp"))
                if ts is not None:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                # turn_context is a top-level rec type; its model lives in the
                # payload (the payload has no "type" field of its own).
                if rec.get("type") == "turn_context":
                    if not model and isinstance(payload.get("model"), str):
                        model = payload.get("model")
                    continue
                if rec.get("type") != "event_msg":
                    continue
                ptype = payload.get("type")
                if ptype == "user_message":
                    input_rounds += 1
                elif ptype == "token_count":
                    info = payload.get("info")
                    if isinstance(info, dict):
                        usage = info.get("total_token_usage")
                        if isinstance(usage, dict):
                            tok = usage  # keep last (cumulative)
                elif ptype == "error":
                    m = payload.get("message")
                    last_error = (m if isinstance(m, str) else "error").strip()[:200]
                elif ptype == "task_complete":
                    last_error = None  # a clean turn after an error clears it
                # turn_aborted (user interrupt) is NOT an error → leave as-is

    per_model: tuple[ModelTokens, ...] = ()
    if isinstance(tok, dict):
        total_in = int(tok.get("input_tokens") or 0)
        cached = int(tok.get("cached_input_tokens") or 0)
        out = int(tok.get("output_tokens") or 0) + int(tok.get("reasoning_output_tokens") or 0)
        per_model = (ModelTokens(
            model=model or "unknown",
            input=max(total_in - cached, 0),  # fresh input only, mirror Claude
            output=out,
            cache=cached,
        ),)

    if not rollout and not row:
        return _unavailable()
    return SessionStats(
        title=title,
        per_model=per_model,
        duration_minutes=_duration_minutes(first_ts, last_ts),
        input_rounds=input_rounds,
        last_error=last_error,
        source="ok" if per_model else "partial",
    )


# --- dispatch ----------------------------------------------------------------

def collect_stats(agent: str, session_id: str, cwd: str = "") -> SessionStats:
    """Dispatch to the per-agent collector; catch everything so the poller never
    throws. Unknown agent or any failure → source="unavailable"."""
    if not session_id:
        return _unavailable()
    try:
        if agent == "codex":
            return collect_codex_stats(session_id)
        if agent == "claude":
            return collect_claude_stats(session_id, cwd)
        return _unavailable()
    except Exception as e:  # defensive: internal formats drift across versions
        logger.warning("collect_stats failed (agent=%s): %s", agent, e)
        return _unavailable()
