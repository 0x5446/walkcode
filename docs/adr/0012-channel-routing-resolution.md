# ADR 0012: Channel Routing and Active Binding Resolution

Date: 2026-06-27

Status: Accepted

## Context

Telegram private chats and forum topics do not always carry a stable root message id on every user message. If the core only matches the full `ChannelBinding` tuple, a normal follow-up message creates a new session instead of continuing the active one.

The opposite failure is also dangerous: when multiple sessions are active in the same chat or topic, guessing the target session can inject input into the wrong agent.

## Decision

Add an active binding resolution contract in `SessionRegistry`:

- exact `ChannelBinding.key()` matches keep highest priority;
- if the inbound event has no `root_message_id`, the registry may fall back to one running session with the same `channel_kind`, `account_id`, `chat_id`, and `thread_id`;
- stopped sessions do not count as active fallback candidates;
- if more than one active candidate exists, return `ambiguous_session` and do not submit input.

This keeps Telegram private/topic routing usable while preserving the single-writer safety model.

## Consequences

- Users can continue the only active Telegram private or topic session naturally.
- Ordinary group chats with multiple sessions must use reply/root context or a future explicit session picker.
- The Orchestrator still does not branch on Telegram or Lark names; it delegates routing to the registry using channel-neutral binding fields.

## Verification

Contract tests cover private follow-up routing, forum topic follow-up routing, exact reply root routing, and ambiguous rootless rejection.
