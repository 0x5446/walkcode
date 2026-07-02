# ADR 0002: Observed Takeover and Legacy Cutover

Date: 2026-06-27

Status: Accepted

## Context

Channel-native V3 has two different launch modes:

- IM-owned structured sessions, where WalkCode owns the agent transport and may submit input.
- External TUI observed sessions, where WalkCode may show selected output but must not write to the running TUI.

Treating those modes as equivalent would reintroduce the old tmux injection failure modes: lost input, process-kill races, and two simultaneous writers.

The old runtime state may contain useful forensic information, but it is not a
safe runtime dependency for the clean-slate service. V3 must start from its own
channel-native state instead of importing legacy Feishu/tmux records.

## Decision

External TUI sessions are represented as observed/read-only sessions with `writer_owner.kind == "external_tui"`.

When an IM user sends input to an observed session:

1. Store the input as a `BlockedInput`.
2. Create a `TakeoverTransaction`.
3. Require an explicit authorization step.
4. If no structured resume reference or external TUI termination boundary exists, move the transaction to `manual_only` and leave the external writer untouched.
5. Only after successful external TUI termination and structured resume may the transaction complete, move writer ownership to the orchestrator, increment generation, create a new writer lease, and mark the blocked input submitted.

Legacy state is not imported. Old records are treated as cleanup blockers or
manual forensic references only:

- install/upgrade/debug gates detect old launchd, hook, shell-wrapper, and
  Feishu env remnants.
- V3 sessions are created only from `ChannelNativeConfig`, native inbound
  channel events, native hook observation payloads, and explicit process-control
  metadata.
- Old terminal-only records are never classified as resumable agent sessions.

## Consequences

- There is no runtime `LegacyTuiTransport`.
- IM input is never injected into a live TUI.
- Process termination is part of a separate `ExternalTuiController` boundary; the core does not reuse old tmux/hook runtime code.
- Stale generations cannot authorize or complete takeover.
- Legacy Feishu fields are not mapped into live V3 `ChannelBinding` records.

## Verification

Contract tests cover:

- takeover without resume or termination boundary becoming `manual_only`;
- successful takeover moving writer ownership and generation;
- stale takeover rejection;
- no legacy state importer is exported by the runtime;
- old terminal-only state cannot become a resumable V3 session;
- debug/release gates report legacy remnants as cleanup blockers.
