# ADR 0027: Single Channel and Agent-bound Runtime Config

Date: 2026-06-28

Status: Accepted

## Context

The early V3 runtime config exposed `WALKCODE_CHANNELS`,
`WALKCODE_PRIMARY_CHANNEL`, `WALKCODE_TRANSPORTS`, and
`WALKCODE_DEFAULT_TRANSPORT`. That made the clean-slate architecture look like a
single local process could bind multiple IM ingress channels at the same time.
It also leaked low-level transport implementation names into user config.

For the intended local deployment model, one WalkCode runtime instance binds one
IM ingress channel. The project supports multiple channel adapter types
(`telegram`, `lark`), but a running instance chooses exactly one of them.

Claude Code and Codex are product-level agents. Their implementation still uses
transport adapters internally, but a running bot binds to exactly one agent.
Users should not need to understand transport names.

## Decision

- Use `WALKCODE_CHANNEL=telegram|lark` for the selected IM channel.
- Remove user-facing `primary_channel`; one instance has no primary/secondary
  channel relationship.
- Reject multi-channel config in one runtime instance.
- Require `WALKCODE_AGENT=claude|codex` to bind this bot/app identity to exactly
  one Coding Agent.
- Reject removed runtime fields instead of treating them as compatibility
  aliases: `WALKCODE_CHANNELS`, `WALKCODE_PRIMARY_CHANNEL`,
  `WALKCODE_TRANSPORTS`, and `WALKCODE_DEFAULT_TRANSPORT`.
- When `WALKCODE_ENV_FILE` is explicitly set, values in that file own the
  runtime identity fields. Ambient shell variables must not silently turn a
  `telegram-codex.env` runtime into a Claude runtime.
- `native doctor` reports only the configured agent and its capability status.
- Keep `AgentTransport` as the internal architecture boundary.

## Consequences

- A Telegram instance and a Lark instance are separate deployments if both are
  needed.
- Claude Code and Codex need separate bot/app identities, env files, state
  paths, and runtime processes when both are needed.
- The config matches the product mental model: one IM channel, one bot/app
  identity, one Coding Agent.
- Operators can keep multiple env files open in the same shell without
  inheriting stale `WALKCODE_AGENT` or `WALKCODE_CHANNEL` values from a previous
  command.
- Runtime status shows the current agent only; cross-agent discovery belongs in
  documentation and setup tooling, not a live bot runtime.

## Verification

Contract tests must prove:

- singular Telegram and Lark config;
- rejection of removed plural channel and transport env fields;
- explicit env-file values overriding stale ambient identity variables;
- `WALKCODE_AGENT=codex` maps to the Codex app-server transport internally;
- `native doctor` reports `channel`, `agent`, and `agent_status` instead of
  removed runtime config fields.
