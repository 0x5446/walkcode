"""Task-title summarizer — short title via Claude Haiku on Vertex.

This is walkcode's OWN summary feature, isolated from the forwarded agent's
session credentials: it uses a dedicated Vertex service account (shared by both
walkcode instances), never the agent's own routing creds. The codex agent keeps
talking to its Azure provider; this only ever calls Haiku on Vertex.

[R6] Runs on its own single-thread executor (NOT the message executor) with a
short timeout, so a slow/hung model call can never delay the Stop hook's main
reply. Any failure — missing dep, bad creds, timeout, empty output — returns None
and the caller falls back to a first-line title. `anthropic` is imported lazily so
a missing optional dependency degrades instead of breaking server import.
"""

import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("walkcode.summarizer")

# Dedicated worker — NOT _msg_executor, so a summary call can't occupy the slot
# that delivers Feishu messages.
_summary_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="walkcode-summary")

_PROMPT = (
    "用不超过 12 个汉字概括下面这个编程任务，只输出标题本身，"
    "不要任何解释、引号或标点：\n\n{text}"
)


def _build_client(sa_path: str, project: str, region: str):
    """Lazy-import AnthropicVertex so a missing `anthropic[vertex]` dependency
    degrades to None instead of crashing the whole server import."""
    import os
    from anthropic import AnthropicVertex  # lazy

    if sa_path:
        os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", sa_path)
    return AnthropicVertex(project_id=project, region=region)


def summarize_title(
    first_user_message: str,
    recent_turn: str = "",
    *,
    project: str,
    region: str,
    sa_path: str = "",
    model: str = "claude-haiku-4-5",
    timeout: float = 8.0,
) -> str | None:
    """Blocking title generation; returns a short title or None on any failure.
    Call via :func:`summarize_async` (or your own executor) — never inline on the
    Stop path."""
    text = (first_user_message or "").strip()
    if recent_turn:
        text = f"{text}\n\n（最近进展）{recent_turn.strip()[:500]}"
    if not text or not project or not region:
        return None
    try:
        client = _build_client(sa_path, project, region)
        resp = client.messages.create(
            model=model,
            max_tokens=64,
            messages=[{"role": "user", "content": _PROMPT.format(text=text[:2000])}],
            timeout=timeout,
        )
        parts = [getattr(b, "text", "") for b in getattr(resp, "content", [])
                 if getattr(b, "type", "") == "text"]
        raw = "".join(parts).strip()
        first_line = raw.splitlines()[0] if raw else ""
        title = first_line.strip().strip("。.\"'《》「」 ")
        return title[:40] or None
    except Exception as e:  # missing dep / creds / timeout / bad output
        logger.info("summarize_title failed: %s", e)
        return None


def summarize_async(callback, first_user_message: str, recent_turn: str = "", **kwargs) -> None:
    """Submit a summarize job to the dedicated executor. ``callback(title|None)``
    runs on the executor thread once done. Never blocks the caller, so it is safe
    to fire from the Stop hook after the main reply is already sent."""
    def _job():
        title = summarize_title(first_user_message, recent_turn, **kwargs)
        try:
            callback(title)
        except Exception as e:
            logger.info("summarize callback failed: %s", e)

    _summary_executor.submit(_job)
