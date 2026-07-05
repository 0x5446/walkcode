# ADR 0011: Attachment Intake Boundary

Date: 2026-06-27

Status: Accepted

## Context

V3 models attachments with `AttachmentRef`, but earlier slices only carried that list through `TurnInput`. That is not enough for real channels: Telegram and Lark identify uploaded files by platform-specific ids, while agent transports need local paths or structured file references.

The core must also avoid silently passing unsupported attachments into transports.

## Decision

Add an attachment intake boundary:

- channel adapters expose `download_attachment(...)`;
- Orchestrator checks `ChannelCapabilities.attachment_download` before accepting inbound attachments;
- attachments are downloaded and normalized before `AgentTransport.submit_turn(...)`;
- unsupported channels reject the input with `capability_disabled`;
- platform file ids stay in `AttachmentRef.source_id`, while local files use `AttachmentRef.local_path`.

## Consequences

- Agent transports receive deterministic local attachment references.
- Channel-specific file APIs stay inside adapters.
- Channels without download support fail explicitly instead of dropping files.

## Verification

Contract tests cover:

- capable-channel download before submit;
- incapable-channel rejection;
- Telegram photo/document parsing into attachment refs.
