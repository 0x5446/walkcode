# ADR 0023: External TUI Termination Boundary

Date: 2026-06-27

Status: Accepted

## Context

Observed sessions are read-only because another TUI owns the live writer. A native resume reference is not enough to make takeover safe: the old TUI can still accept input after the IM side resumes the same agent session.

The clean-slate design must not bring back the old tmux/hook runtime, but it still needs an explicit process-control boundary before an observed session can become IM-owned.

## Decision

Observed takeover requires two independent references:

- a structured `resume_ref` for the target agent transport;
- a `terminate_ref` for an `ExternalTuiController`.

The accepted takeover action must terminate the external TUI before
`AgentTransport.resume(...)` and before blocked input submission.

If termination cannot be performed because no `terminate_ref` or controller exists, the takeover becomes `manual_only`. If termination fails, the transaction becomes `failed`, the blocked input stays unsent, and writer ownership stays with `external_tui`.

Claude Code `Stop` hooks are turn-completion notifications, not process-exit
signals. They must not be used to skip the termination boundary. Automatic
takeover may skip process termination only when WalkCode has an explicit
process-exit style signal or no live external writer/termination reference is
present.

## Consequences

- The core keeps a clean boundary: process control is an injectable adapter, not old tmux/hook code.
- A session with only a native resume id is not automatically safe to take over.
- Telegram and Lark can still render their own confirmation/progress UI through neutral view models.
- Real OS/process termination remains externally wired and must be E2E-gated before being claimed.

## Verification

Contract tests must prove:

- termination happens before resume and blocked-input submission;
- missing termination capability becomes `manual_only`;
- termination failure does not resume, submit, or transfer writer ownership.

Verified with:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover_process_control.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_takeover_process_control.py tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py tests/test_channel_native_takeover.py
18 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
118 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```
