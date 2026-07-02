# ADR 0005: Streaming and Guardrail Boundaries

Date: 2026-06-27

Status: Accepted

## Context

The first transport contract used `events(...) -> list[AgentEvent]`, which was enough for fake transports and Codex batch tests but too narrow for Claude SDK-style event streams. An independent review also found that `SessionRegistry.block_input(...)` could be called on structured sessions and that Lark text rendering reused a Telegram helper.

## Decision

Broaden the event boundary and tighten registry invariants:

- `Orchestrator` accepts both buffered event lists and async event iterators.
- `SessionRegistry.block_input(...)` only succeeds when `writer_owner.kind == "external_tui"`.
- Neutral view text rendering lives in `render_view_text(...)`, not in a channel-specific adapter.

## Consequences

- Claude/Codex transports can expose streaming events without changing the orchestrator flow.
- The read-only observed-session rule is enforced in the registry, not only by caller discipline.
- Lark and Telegram rendering no longer depend on each other's helper methods.

## Verification

Contract tests cover:

- async event streams being drained and rendered;
- structured sessions rejecting `block_input`;
- Lark rendering not calling Telegram's text helper.
