# ADR 0006: Retention and Compaction Policies

Date: 2026-06-27

Status: Accepted

## Context

The channel-native core now persists sessions, interactions, outbox items, authorization state, and inbound ledger state. Persistence alone is not enough: callback tokens, completed interactions, sent deliveries, and dead-lettered deliveries must not grow forever.

The design also needs to keep enough audit history to debug permission decisions and delivery failures. Immediate deletion would make incidents hard to inspect.

## Decision

Add explicit retention policies at the store boundary:

- `InteractionContext` records creation and expiry timestamps.
- `InteractionStore.compact()` removes expired tokens, expired unresolved interactions, decided interactions after the decision-retention window, and any `Other` awaiting bindings that point at removed interactions.
- `DeliveryItem` records `finished_at` when it moves to `sent` or `dead`.
- `DurableOutbox.compact()` removes sent and dead-letter records after their configured retention windows.
- retention configuration and completion timestamps are persisted in `JsonFileStateStore`.

## Consequences

- The service can run continuously without unbounded interaction/outbox growth in the JSON-backed implementation.
- Auditable records are kept for a bounded window rather than deleted immediately.
- Production storage can later replace JSON snapshots without changing the core retention contract.

## Verification

Contract tests cover:

- expired callback tokens and unresolved interactions being compacted together;
- decided interactions staying during retention and being pruned afterward;
- stale `Other` awaiting bindings being removed;
- sent and dead outbox records being retained and compacted by policy;
- persistence preserving retention settings and delivery completion timestamps.
