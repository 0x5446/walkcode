# ADR 0030: Idle Structured Session Reacquire

Date: 2026-06-28

Status: Accepted; partially superseded by ADR 0059 (the "active sessions
require a non-expired lease" rule below is withdrawn — the lease was never
renewed mid-turn, so it silently dropped every message sent >TTL into a
long-running turn)

## Context

The V3 runtime can run as `serve --once` during local Telegram smoke
tests. After a successful first turn, the Python process exits and the in-memory
transport handle is no longer usable. The persisted session remains
`lifecycle_state=IDLE`, but its writer lease eventually expires. Treating that
expired lease as an active writer failure blocks the next Telegram message even
though the agent turn completed cleanly.

The same recovery shape applies after a structured transport emits
`session.error` and the session moves to `ERROR_RECOVERABLE`. The live writer is
not trustworthy anymore, but the durable Claude/Codex resume reference can be
used to continue the product session on the next IM input.

Extending the lease TTL is not sufficient. The next process must use a durable
agent resume reference, not the old in-memory handle id.

## Decision

`IDLE` and `ERROR_RECOVERABLE` mean there is no active writer lease that should
receive direct input. Both are reusable structured sessions when they have a
durable resume reference.

- `turn.completed` releases the active writer lease.
- Transport-specific durable resume references are persisted into
  `session.transport_ref`.
  - Claude headless stores the Claude SDK `session_id` as
    `agent_session_id`.
  - Codex app-server already stores `thread_id`.
- The next IM input for an `IDLE` or `ERROR_RECOVERABLE` structured session
  first reacquires the writer: it calls `AgentTransport.resume(...)`, stores the
  new handle reference, creates a fresh writer lease, then submits the user turn.
- State and Telegram diagnostics do not fail merely because an `IDLE` session
  has no active lease or an old expired lease.
- ~~Active or waiting sessions still require a non-expired writer lease. Those
  states remain unsafe if the lease is missing or expired.~~ **Withdrawn by
  ADR 0059**: the lease was never renewed while a turn ran, so this rule
  silently dropped every message sent more than one TTL into a long-running
  turn. Active sessions now accept submits regardless of lease state; worker
  liveness is proven by the submit itself (`TransportUnavailable` → resume
  fallback).

## Consequences

- A completed or recoverable-error Telegram conversation can continue across
  separate `serve --once` invocations and transient provider failures.
- ~~Diagnostics can distinguish a reusable idle session from a stale active
  writer.~~ Superseded by ADR 0059: an "expired" lease on an active session
  is the normal shape of a long-running turn and is reported as
  informational only.
- If an idle or recoverable-error session lacks a durable resume reference, the
  Telegram gate remains unsafe and the offset is not confirmed.
- Existing active-turn safety remains intact for permission waits, ask-user
  waits, and in-progress turns.

## Verification

Contract tests cover:

- `turn.completed` releases the active lease and persists Claude
  `agent_session_id`.
- follow-up input to an expired `IDLE` session resumes and reacquires the writer
  before submit.
- follow-up input to an `ERROR_RECOVERABLE` session resumes and reacquires the
  writer before submit.
- ~~Telegram diagnostics allow an idle resumable session but still block
  active expired leases.~~ Superseded by ADR 0059: active sessions past the
  lease TTL are reported submittable.
- state diagnostics allow idle sessions without an active lease.
