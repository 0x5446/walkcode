# ADR 0018: Transport-aware Health Watchdog

Date: 2026-06-27

Status: Accepted

## Context

The legacy runtime used tmux and hook activity to infer whether a session was healthy. V3 deliberately removes tmux/TUI/hook injection from the core runtime, but users still need clear health state and watchdog behavior when a structured agent stops producing progress.

## Decision

Track health from structured transport events only. Each session stores the last progress timestamp and event type. The Orchestrator updates lifecycle state while draining events and can produce a neutral health result:

- active when progress is recent;
- waiting for permission when the transport asks for a permission decision;
- idle after `turn.completed`;
- recoverable-error after `session.error`;
- stale when a running session has exceeded the configured progress timeout.

The watchdog check is observational. It does not kill processes, interrupt turns, or synthesize transport success.

## Consequences

- Health no longer depends on terminal pane contents or hook timing.
- Timeout behavior is testable with a fake clock and fake transport.
- Channel adapters can render the same neutral health view in Telegram or Lark.
- Recovery policy remains a separate decision instead of being hidden inside the watchdog.

## Verification

Contract tests cover progress updates, lifecycle transitions, stale health detection without transport control side effects, neutral health views, and progress metadata persistence.
