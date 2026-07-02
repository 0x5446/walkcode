# ADR 0017: Permission Callback Round-trip

Date: 2026-06-27

Status: Accepted

## Context

The clean-slate core already has neutral permission prompt rendering and write-once callback tokens, but that is not enough. A transport permission request must become a channel prompt, and a channel callback must be authorized and delivered back to the same transport request. Otherwise the UI can record a decision that the agent never receives.

## Decision

Treat `permission.requested` as a first-class transport event. The Orchestrator registers a permission interaction with:

- the current session and generation;
- the transport request id;
- tool name and input;
- allowed actions;
- explicit high-risk metadata.

When a callback arrives, the Orchestrator resolves the token, checks the current session generation, authorizes the actor with `AuthorizationStore.can_decide_permission(...)`, verifies `TransportCapabilities.permission_callback`, consumes the write-once token, and calls `AgentTransport.approve_permission(...)`.

## Consequences

- Permission UI decisions are coupled to transport delivery instead of being UI-only state.
- High-risk approvals use the same owner/admin boundary as other high-risk controls.
- Unsupported transports fail with `capability_disabled` before a token is consumed.
- Codex app-server permission approval is enabled for app-server server-request
  methods covered by ADR 0041/0042; unverified high-risk controls remain
  capability-gated separately.

## Verification

Contract tests cover prompt creation from transport events, role-gated callbacks, disabled capability behavior, fake transport approval calls, and Claude headless client delegation.
