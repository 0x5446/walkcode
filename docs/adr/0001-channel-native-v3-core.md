# ADR 0001: Channel-native V3 Core

Date: 2026-06-27

Status: Accepted

## Context

The current walkcode runtime is a tightly coupled Lark + tmux + hook system. It has useful product semantics: durable state, pending session binding, permission dedupe, redelivery, health status, and human approval. It also carries structural problems: Feishu identifiers leak into core state, tmux keyboard injection is not a stable transport, and hook output is used as the main event channel.

The V3 design is clean-slate. Old code is evidence and a requirements source, not a runtime compatibility layer.

## Decision

Implement a new core under `walkcode.channel_native` with these boundaries:

- `ChannelAdapter`: platform-specific IM rendering and inbound parsing.
- `AgentTransport`: structured agent lifecycle, input, output, permission, and interrupt.
- `SessionRegistry`: channel binding, transport reference, writer ownership, leases, generation, and blocked inputs.
- `InteractionStore`: short callback tokens, write-once decisions, AskUserQuestion state, and stale generation handling.
- `DurableOutbox`: idempotent outbound delivery with transient retry and permanent failure handling.
- `Orchestrator`: the only coordinator that can acquire writer leases, submit turns, route events, and drive interactions.

Telegram is the first rollout channel. Lark/Feishu is a peer `ChannelAdapter`, not a lower-tier integration. External TUI sessions are observed/read-only until an explicit takeover transaction succeeds.

## Consequences

- The new core does not import `server.py`, `tty.py`, or old Feishu state fields.
- No `LegacyTuiTransport` is provided.
- Unverified capabilities default to disabled through explicit capability objects.
- All write operations carry `generation` and must be rejected when stale.
- The first implementation slice uses fake channel and fake transport contract tests before real Telegram/Lark/Codex code.

## Verification

PR1 must pass contract tests for:

- fake channel + fake transport session turn flow;
- duplicate callback write-once decision;
- durable outbox transient/permanent delivery;
- pending channel binding;
- unknown event fallback rendering;
- transport/channel capability gates;
- writer lease acquisition and expiry;
- stale generation rejection.
