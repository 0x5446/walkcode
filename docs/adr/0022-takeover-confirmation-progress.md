# ADR 0022: Takeover Prompt and Progress Views

Date: 2026-06-27

Status: Accepted

## Context

Observed takeover can stop or supersede a user's external TUI writer. Even with process control behind a separate adapter boundary, the product action is high-risk because it changes who owns future agent input.

The earlier two-step confirmation design produced too much Telegram UI noise:
users saw a takeover card, then another options card, and then progress
messages. In practice the topic itself is already the session boundary, so the
confirmation should be the first explicit takeover button.

The design calls for channel-specific UX, but the core still needs a channel-neutral prompt and progress contract so Telegram and Lark can render their best UI without changing semantics.

## Decision

`takeover_and_send` is the confirmed action. It performs takeover directly:
authorize, terminate the external TUI when required, resume the structured
transport, and submit the blocked input.

The Orchestrator renders:

- `takeover_progress` before termination/resume/submit;
- `manual_only` when no native resume reference or termination boundary exists;
- `takeover_progress(phase="failed")` when resume or submit fails.

`confirm_takeover` remains accepted only as a backward-compatible legacy
callback action for already-persisted tokens. New UI must not generate it.
New UI also must not generate `Keep read-only` or `Manual steps` buttons.

## Consequences

- Telegram uses one explicit `Take over` / `Take over and send` button.
- Lark can show richer impact text in the first card while keeping the same
  callback token model.
- Resume failure and manual-only outcomes are visible through normal channel delivery.
- No channel adapter needs to branch on takeover business rules.

## Verification

Contract tests cover:

- first click completing takeover;
- manual-only terminal view;
- resume-failed progress view.

Verified with:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py
11 passed

uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py tests/test_channel_native_callback_ack.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_takeover.py
39 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```
