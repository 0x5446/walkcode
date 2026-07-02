# ADR 0028: Codex App-server Stdio Client

Date: 2026-06-28

Status: Accepted

Superseded in part by ADR 0041 for the target Codex architecture. This ADR
remains the record for the first real stdio client slice.

## Context

Channel-native V3 already had a `CodexAppServerTransport` contract with an
injectable client, but the V3 runtime still marked Codex unavailable. That
made the product config misleading: a Codex-bound bot could be configured while
Codex still had no real runtime client.

The runtime should not expose app-server method names as `.env` transport
configuration. Codex support should be wired internally when the local `codex`
CLI is installed.

## Decision

- Add `CodexStdioAppServerClient` in the V3 runtime.
- Start `codex app-server --stdio` lazily on first request.
- Initialize the JSON-RPC session with WalkCode client metadata.
- Use `thread/start`, `thread/resume`, and `turn/start` through the existing
  `CodexAppServerTransport`.
- Buffer server notifications and expose thread-scoped events to the transport.
- Treat Codex `event_msg` notifications as first-class app-server events when
  they appear on stdout: `agent_message` becomes visible agent text and
  `task_complete` becomes `turn.completed`. This prevents a completed Codex
  turn from staying `ACTIVE` until the event timeout expires.
- Keep unverified capabilities disabled: permission callback, AskUserQuestion,
  interrupt, model switching, permission-mode switching, and checkpoint rewind.
- Build the Codex transport automatically when the `codex` CLI is present; no
  user-facing `WALKCODE_TRANSPORTS` setting is required.

The stdio client is no longer considered the final Codex architecture. ADR 0041
moves the target toward a shared app-server endpoint that both the Codex TUI and
WalkCode's Telegram adapter can join.

## Consequences

- `native doctor` can report the configured Codex agent adapter as available
  when the local `codex` CLI is installed.
- Codex remains more conservative than Claude: basic structured start/resume
  and turn streaming are wired, but high-risk controls stay off until separately
  verified.
- Real Codex turn E2E remains behind `WALKCODE_E2E_CODEX_APP_SERVER=1`
  and the shared `WALKCODE_E2E_CWD`; no app-server URL is configured because
  WalkCode starts `codex app-server --stdio` itself.

## Verification

Contract and local protocol checks cover:

- actual app-server response shape with nested `thread.id`;
- JSON-RPC notification conversion for `item/agentMessage/delta`,
  `turn/completed`, and Codex `event_msg/task_complete`;
- runtime wiring when the `codex` CLI exists;
- local stdio app-server handshake and `thread/start` without starting a model
  turn.
