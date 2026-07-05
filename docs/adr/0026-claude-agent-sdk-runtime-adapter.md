# ADR 0026: Claude Agent SDK Runtime Adapter

Date: 2026-06-28

Status: Accepted

## Context

The channel-native V3 runtime uses `ClaudeHeadlessTransport` for the first
Telegram 1:1 live path. The real `claude_agent_sdk` package exposes
`ClaudeSDKClient(options=ClaudeAgentOptions(...))`, `connect()`, `query()`, and
`receive_response()`. The initial prototype transport used a provisional
`cwd/start/submit/events` shape that works for fake clients but not for the real
SDK.

## Decision

Adapt `ClaudeHeadlessTransport` to the real SDK surface while keeping the fake
client hooks used by contract tests:

- construct default clients with `ClaudeAgentOptions(cwd=...)`;
- call `connect()` during launch when present;
- submit text with `query()` when `submit()` is not available;
- drain `receive_response()` when `events()` is not available;
- convert SDK assistant text/result/error messages into neutral `AgentEvent`s.
- install and upgrade the uv tool with `--with claude-agent-sdk`, so the
  deployed `walkcode` CLI can report Claude as available without requiring an
  ad hoc wrapper command.

## Consequences

- The Telegram -> Claude headless -> Telegram V3 path can run against the
  installed SDK package.
- Existing focused tests can still inject small fake clients.
- Real SDK behavior remains behind explicit E2E gates and local Claude auth.
- A plain `uv tool install walkcode` may still omit the SDK; the supported V3
  install and upgrade surfaces own the runtime dependency injection.

## Verification

Contract tests must prove:

- launch builds SDK options with the configured cwd and connects the client;
- submit uses `query()` with the user text;
- SDK text/result messages become channel-neutral turn events.
- install and upgrade commands include `--with claude-agent-sdk`.
