# ADR 0013: Callback Acknowledgement Boundary

Date: 2026-06-27

Status: Accepted

## Context

Interactive IM callbacks have a user-visible acknowledgement path. Telegram callback queries should be answered quickly so the client stops showing progress. Lark callbacks similarly have acknowledgement and toast semantics, but the exact API shape is platform-specific.

The core must not generate Telegram or Lark callback payloads directly, and callback acknowledgement must not be tied to permission or AskUserQuestion business logic.

## Decision

Add `ChannelAdapter.ack_callback(...)`.

The Orchestrator calls it before token decision handling when the channel reports `private_callback_ack=true`. The adapter owns the platform mapping:

- Telegram calls `answerCallbackQuery` with the callback query id.
- Lark keeps callback acknowledgement behind the Lark adapter boundary.
- fake adapters record acknowledgements for contract tests.

## Consequences

- Invalid, stale, and duplicate callback operations can still clear client-side callback progress.
- Core callback handling remains channel-neutral.
- Rich per-channel toast copy remains a later UX concern.

## Verification

Contract tests cover Telegram acknowledgement, invalid-token acknowledgement, and capability-disabled acknowledgement handling.
