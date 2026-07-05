# ADR 0021: Structured Resume Boundary

Date: 2026-06-27

Status: Accepted

## Context

Observed takeover is only safe if the external TUI writer is replaced by a live structured transport handle before the blocked input is submitted. Earlier slices could store a structured resume reference, but the Orchestrator still treated that reference as if it were already a usable handle.

That shortcut hides the most important failure point: native resume can fail. If it fails, the system must not mark the takeover complete or pretend the blocked input was delivered.

## Decision

Add a generic `ResumeSpec` and `AgentTransport.resume(...)` method.

Takeover execution now follows this order:

1. authorize the takeover;
2. call the target transport's `resume(...)`;
3. complete the registry takeover using the returned `TransportHandle`;
4. submit the blocked input with its retained idempotency key.

If resume fails, the transaction stays pre-completion and the blocked input remains unsent.

## Consequences

- The Orchestrator no longer fabricates transport handles from stored refs.
- Codex resume uses the same boundary as takeover and future import flows.
- Claude resume remains explicit and testable through injected clients; real SDK resume still needs external E2E before it is claimed.
- Resume failure is visible instead of being misreported as delivered input.

## Verification

Contract tests cover:

- observed takeover calling resume before submit;
- resume failure leaving takeover uncompleted and input unsent;
- Codex `thread/resume` behind the generic resume boundary;
- Claude injected-client resume delegation.
