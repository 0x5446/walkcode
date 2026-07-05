# ADR 0020: Takeover Orchestrator Boundary

Date: 2026-06-27

Status: Accepted

## Context

The registry-level takeover transaction already prevents direct IM writes into an external TUI session. That is not enough for a product-safe flow: real users click buttons in Telegram or Lark, and those clicks must go through authorization, capability checks, stale-generation checks, and visible prompt/progress views before any writer ownership changes.

If takeover remains only a low-level registry API, callers can accidentally consume callback tokens before discovering that the actor is unauthorized or the target transport cannot support external TUI takeover.

## Decision

Observed-session input creates a blocked input and a takeover transaction, then renders a platform-neutral `takeover_prompt` view through the outbox.

Takeover callback handling is owned by the Orchestrator:

- callback tokens map to a takeover interaction context, not to Telegram or Lark payloads;
- owner/admin authorization is checked before token consumption;
- `TransportCapabilities.external_tui_takeover` is checked before token consumption;
- missing structured resume references move the transaction to `manual_only` and leave the external TUI as writer;
- supported takeover completes the registry transaction, moves writer ownership to the structured transport, and submits the retained blocked input using its original idempotency key.

This slice still does not stop or kill a real process. Process control remains outside the core contract until a dedicated process-control adapter is designed and tested.

## Consequences

- Telegram and Lark can render different takeover UI while sharing the same callback semantics.
- Unauthorized or unsupported callbacks can be retried by an authorized actor because the token is not consumed.
- IM input on observed sessions remains durable until it is submitted, cancelled, expired, or marked manual-only.
- The implementation keeps the clean-slate rule: no `LegacyTuiTransport`, no keyboard injection, and no hook-driven write path.

## Verification

Contract tests cover:

- blocked observed input rendering a takeover prompt;
- unauthorized takeover callback not consuming the token;
- missing resume reference producing `manual_only`;
- disabled external-TUI takeover capability not consuming the token;
- successful takeover submitting the retained blocked input exactly once;
- stale takeover callback rejection.
