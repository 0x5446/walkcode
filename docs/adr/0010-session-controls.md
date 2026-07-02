# ADR 0010: Session Controls and Transport Control Boundary

Date: 2026-06-27

Status: Accepted

## Context

The V3 design keeps `interrupt`, session close, and command menus as first-class product capabilities. Earlier slices only rendered a platform-neutral command menu; they did not connect commands to transport methods, authorization, or persistent session state.

Without a control boundary, Telegram/Lark UI could show actions that either do nothing or bypass role checks.

## Decision

Add a session-control contract:

- owner/admin may run high-risk session controls;
- collaborator/reviewer cannot interrupt or close sessions;
- `interrupt` is gated by `TransportCapabilities.interrupt`;
- close transitions the session to stopped state and blocks future turn submission;
- transport methods are invoked through `AgentTransport.interrupt(...)` and `AgentTransport.shutdown(...)`;
- command menu generation uses authorization and capabilities instead of channel-specific branching.

## Consequences

- IM command menus can become real controls without leaking channel details into the core.
- Stopped sessions are protected at the registry level, not just by UI.
- Real Claude/Codex E2E remains separately gated; the contract tests use fake transports.

## Verification

Contract tests cover:

- authorized interrupt;
- denied collaborator/reviewer control;
- capability-disabled interrupt;
- close and submit-after-close guard;
- command menu action filtering.
