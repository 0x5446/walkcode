# ADR 0004: Persistence and Retry Hardening

Date: 2026-06-27

Status: Accepted

## Context

An independent Claude Code review correctly identified that the initial `DurableOutbox`, session registry, interaction store, authorization store, and inbound ledger were still in-memory contracts. That was enough for early boundary tests, but it was not enough to support the reliability claims in the design.

The same review also found that transient delivery retries had no delay or attempt cap, inbound dedupe could consume an event before a failed handler completed, and Telegram's real HTTP branch used blocking IO inside an async method.

## Decision

Add a first persistence boundary before adding more external channel features:

- `JsonFileStateStore` writes an atomic snapshot containing sessions, interactions, outbox, authorization, and inbound ledger state.
- `SessionRegistry`, `InteractionStore`, `DurableOutbox`, `AuthorizationStore`, and `InboundLedger` provide explicit `to_dict` / `from_dict` contracts.
- `DurableOutbox` tracks `next_attempt_at`, `last_error`, retry delay, and `max_attempts`; repeated transient failures eventually move to the dead queue.
- `InboundLedger` uses `start`, `complete`, and `fail`; handler exceptions release the event for retry.
- `TelegramBotApi` runs the synchronous urllib request in a worker thread instead of blocking the event loop.

## Consequences

- The core now has a tested crash-recovery serialization boundary.
- This is still not a final production database decision.
- Outbox retry scheduling is deterministic and testable, but no background scheduler is included yet.
- Telegram real E2E is still gated on credentials and target chat validation.

## Verification

Contract tests cover:

- blocked input, writer lease, callback token, role, inbound ledger, and pending outbox snapshot round-trip;
- transient retry backoff and max-attempt dead lettering;
- immediate permanent dead lettering;
- inbound event retry after handler exception;
- Telegram real HTTP branch running off the event loop.
