# ADR 0014: Lark Attachment Intake Parity

Date: 2026-06-27

Status: Accepted

## Context

Telegram attachment intake is now normalized before transport submission. Lark must keep peer adapter status and preserve its existing image/file capability, but Lark resource download needs both the file key and the source message id.

The core `AttachmentRef` currently carries only `source_id`, `mime`, and `local_path`, which is enough for Telegram but not enough for Lark.

## Decision

Extend `AttachmentRef` with `source_message_id`.

`LarkChannelAdapter` parses image/file message content into attachment refs and uses the injected Lark API boundary to download the resource into a local temp file. The Orchestrator keeps using the generic channel `download_attachment(...)` contract; no Lark-specific condition is added to core routing.

## Consequences

- Telegram and Lark use the same attachment intake flow.
- Lark-specific resource identifiers remain inside the adapter-facing `AttachmentRef`.
- Persistence round-trips blocked inputs and pending attachment refs with source message context.

## Verification

Contract tests cover Lark image/file parsing, Lark binary download, and Orchestrator submit after Lark attachment normalization.
