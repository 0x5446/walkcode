# ADR 0003: View, Authorization, and Delivery Contracts

Date: 2026-06-27

Status: Accepted

## Context

Channel-native V3 must support Telegram and Lark without putting Telegram HTML, Telegram callback payloads, Lark cards, or Feishu ids into core state. It must also prevent group users from silently writing to an agent session and must keep outbound delivery from silently disappearing.

## Decision

Core rendering uses platform-neutral view models produced by `ViewModelFactory`.

The first supported view models are:

- `permission_prompt`
- `ask_user_question`
- `health`
- `error`
- `command_menu`
- `takeover_prompt`

Channel adapters translate those views into native UI:

- Telegram uses text plus inline keyboards with short callback tokens.
- Lark uses card-like send/update calls with the same view payload.

Authorization is modeled separately from channel parsing:

- `AuthorizationStore` grants `owner`, `collaborator`, `reviewer`, or `admin` per session.
- owner/collaborator/admin may submit input.
- only owner/admin may approve high-risk actions or takeover.
- denied input returns an explicit blocked result before any transport call.

Outbound delivery goes through `DurableOutbox` and `OutboxDispatcher`:

- view models are enqueued before adapter delivery.
- transient adapter failures stay pending.
- permanent adapter failures move to the dead queue.
- inbound event ids are checked through `InboundLedger` before routing.

## Consequences

- Core state remains channel-neutral.
- Telegram and Lark can optimize UX without changing interaction semantics.
- Multi-user groups get a real authorization gate before transport writes.
- Duplicate inbound events do not start duplicate turns.
- Direct channel sends from the orchestrator are no longer the default path for transport output.

## Verification

Contract tests cover:

- Telegram and Lark rendering from the same permission view model;
- AskUserQuestion option and `Other` awaiting state;
- neutral health/error/command/takeover views;
- role gates for submit, high-risk permission, and takeover;
- unauthorized submit not calling transport;
- duplicate inbound event rejection;
- outbox transient and permanent failure mapping.
