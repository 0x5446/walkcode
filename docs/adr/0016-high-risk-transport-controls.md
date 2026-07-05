# ADR 0016: High-risk Transport Controls

Date: 2026-06-27

Status: Accepted

## Context

`TransportCapabilities` already models `set_model`, `set_permission_mode`, and `checkpoint_rewind`, but capability flags alone do not make these operations safe or testable. These actions can materially change agent behavior or workspace state and must be authorized like close, interrupt, and takeover.

## Decision

Add explicit `AgentTransport` control methods:

- `set_model(...)`
- `set_permission_mode(...)`
- `rewind_checkpoint(...)`

The Orchestrator exposes owner/admin-only methods for each operation, checks stopped-session state, then gates on transport capabilities before invoking the transport. Codex keeps these capabilities disabled until real app-server support is validated. Claude headless delegates to injected client methods when present.

## Consequences

- High-risk controls are no longer implied by capability flags.
- Unsupported transports fail explicitly with `capability_disabled`.
- Control calls are testable without real Claude/Codex external dependencies.

## Verification

Contract tests cover authorization, capability gates, stopped-session guard, fake transport calls, and Claude headless delegation.
