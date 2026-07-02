# ADR 0041: Codex Unified App-server Client Architecture

Date: 2026-07-01

Status: Accepted as target architecture; server-request foundation, managed
daemon control-socket client selection, and local remote-TUI attach smoke
implemented; long-lived shared remote-thread runtime pending.

## Context

The early V3 Codex adapter had two different observation paths:

- Telegram-origin Codex sessions used `CodexAppServerTransport` over a
  WalkCode-owned `codex app-server --stdio` process.
- TUI-origin Codex sessions are observed through local hooks such as
  `SessionStart`, `UserPromptSubmit`, `MessageDisplay`, `PreToolUse`, and
  `PermissionRequest`.

That split made the product work incrementally, but it is not the best Codex
architecture. The current `auto` mode now prefers the managed Codex daemon
control socket when the standalone daemon install exists, and keeps stdio only
as a fallback. Codex 0.142.5 exposes an app-server protocol intended for rich
clients. The local CLI confirms that:

- `codex --remote <ADDR>` lets the TUI connect to a remote app-server endpoint;
- `codex app-server` supports `stdio://`, `unix://`, and experimental
  `ws://IP:PORT` transports;
- `codex app-server generate-ts --experimental` and
  `generate-json-schema --experimental` emit the protocol for the installed
  Codex version.

The generated 0.142.x protocol includes:

- client requests: `thread/start`, `thread/resume`, `thread/read`,
  `thread/turns/list`, `thread/turns/items/list`, `thread/unsubscribe`,
  `turn/start`, `turn/steer`, and `turn/interrupt`;
- server notifications: `thread/started`, `thread/status/changed`,
  `turn/started`, `item/agentMessage/delta`, `item/started`,
  `item/completed`, tool/progress notifications, `turn/completed`, and
  token-usage/status updates;
- server requests for HITL and client-owned work:
  `item/commandExecution/requestApproval`,
  `item/fileChange/requestApproval`,
  `item/permissions/requestApproval`,
  `item/tool/requestUserInput`,
  `mcpServer/elicitation/request`, and `item/tool/call`.

The generated `ThreadResumeParams` explicitly says a running thread can be
rejoined by `threadId`. That is the key primitive for making the TUI and
Telegram clients attach to the same Codex thread instead of reconstructing
state from hooks.

## Decision

Codex support should move to a unified app-server client architecture.

The target runtime shape is:

1. A Codex-bound WalkCode service owns or connects to one local app-server
   endpoint for its bot instance.
2. Telegram-origin sessions call `thread/start` and `turn/start` on that
   endpoint.
3. TUI-origin sessions should be started as Codex TUI clients connected to the
   same endpoint with `codex --remote ...` whenever WalkCode manages or
   documents the launch path.
4. If a TUI session already exists outside WalkCode, WalkCode should prefer
   `thread/resume` against the shared app-server by `threadId` over hook-only
   transcript reconstruction.
5. WalkCode's Codex adapter becomes a protocol client that handles both
   JSON-RPC responses and server-initiated requests. It no longer treats
   app-server notifications as only a post-turn batch list.
6. Hooks remain a fallback compatibility path for legacy or unmanaged local
   TUI launches, but they are no longer the primary Codex TUI observation
   mechanism.

The session identity stays the Codex `threadId`. Telegram topic identity is a
placement of that thread, not the product session id.

## Protocol Mapping

WalkCode should map app-server protocol events into existing neutral session
events:

- `item/agentMessage/delta` -> `turn.delta`
- `turn/completed` -> `turn.completed`
- `item/started` / `item/completed` for command, file change, MCP, and dynamic
  tool items -> compact `tool.started` / `tool.completed` / `tool.failed`
- `thread/status/changed` -> status card state
- `thread/tokenUsage/updated` -> status card usage fields when available
- `item/commandExecution/requestApproval` -> permission prompt
- `item/fileChange/requestApproval` -> permission prompt
- `item/permissions/requestApproval` -> permission prompt
- `item/tool/requestUserInput` -> AskUserQuestion prompt
- `mcpServer/elicitation/request` -> AskUserQuestion or form prompt

Because server requests are JSON-RPC requests, the final Telegram/Lark decision
must answer the original request id. It is not enough to record a UI decision in
`InteractionStore`.

## Takeover

For Codex TUI topics, takeover should become a client ownership transition
around the same app-server thread:

1. identify the topic's Codex `threadId`;
2. stop or detach the TUI writer when required by the single-writer policy;
3. call `thread/resume` by `threadId`;
4. attach WalkCode as the write client;
5. answer any pending server request or submit the blocked Telegram input.

If the TUI and Telegram are both connected to the same app-server thread and
the app-server protocol can enforce write ownership, the long-term target is to
avoid killing the TUI process. Until that is verified, WalkCode keeps the
current conservative single-writer rule: Telegram takeover may require TUI
termination before WalkCode submits input.

## Consequences

- Codex TUI input, assistant output, tool calls, token usage, status, and HITL
  all come from the same protocol source.
- Telegram-origin and TUI-origin Codex sessions can converge on one event
  stream instead of mixing app-server events with hooks.
- HITL can be implemented for Codex without inventing hook-specific approval
  formats.
- The old `codex app-server --stdio` client remains useful for tests and simple
  local mode, but the deployable architecture should prefer a durable daemon or
  shared endpoint that both the TUI and WalkCode can join.
- WebSocket transport is experimental, so local deployment should prefer the
  app-server daemon/control socket or a Unix socket until remote WebSocket auth
  and stability are verified.

## Migration

1. Generate and snapshot protocol fixtures for the installed Codex version used
   in tests.
2. Replace `CodexStdioAppServerClient.events(thread_id) -> list` with a
   bidirectional app-server session client that can:
   - send client requests;
   - correlate responses by JSON-RPC id;
   - route server notifications by `threadId`;
   - persist pending server requests by request id;
   - answer server requests after IM decisions.
3. Add a Codex runtime mode that connects to a daemon/control socket.
4. Update documented TUI launch wrappers to use `codex --remote unix://...`.
5. Keep hook observation enabled only as fallback and mark hook-created Codex
   observations as `legacy_external_tui_hook`.

The runtime defaults to `WALKCODE_CODEX_APP_SERVER_MODE=auto`:

- if the local Codex standalone daemon install exists at the CLI-managed
  location, WalkCode starts/uses `codex app-server daemon` and connects through
  the daemon's Unix control socket using a WebSocket JSON-RPC connection;
- if the standalone daemon install is missing, WalkCode falls back to the
  isolated `codex app-server --stdio` client so Telegram-origin Codex sessions
  still work instead of failing at startup;
- `stdio` forces the isolated client, while `daemon`, `managed`, or `shared`
  require the standalone daemon/control-socket path.

## Implementation Progress

2026-07-01:

- `CodexStdioAppServerClient.events(...)` now returns HITL server requests
  without waiting for `turn/completed`, so blocked turns can render a Telegram
  prompt instead of hanging until the turn finishes.
- `CodexAppServerTransport` maps Codex app-server approval and
  request-user-input server requests to neutral WalkCode events.
- `CodexAppServerTransport` can answer the original JSON-RPC request id through
  `answer_request(...)` for command/file/permission approvals and tool/MCP user
  input.
- `CodexManagedAppServerClient` now starts the managed daemon and talks to the
  app-server control socket directly with a WebSocket JSON-RPC connection.
  Local validation found that `codex app-server proxy` is not the deployable
  WalkCode client path.
- The local machine now has the CLI-managed standalone Codex install
  (`codex-cli 0.142.5`) under `~/.codex/packages/standalone/current/codex`, so
  `auto` mode selects the shared daemon path instead of falling back to
  `--stdio`.
- Local `codex --remote unix://...` attach smoke now connects to the same
  daemon and completes a minimal prompt.
- Remaining work: response/notification correlation for long-lived
  multi-client sessions, full Telegram HITL live E2E, app-server-native Codex
  TUI transcript sync, and Codex TUI takeover E2E.

## Verification

Required before implementation is considered complete:

- protocol fixture tests for server requests, notifications, and response
  correlation;
- local app-server daemon smoke: WebSocket handshake over the Unix control
  socket, `thread/start`, TUI `--remote` attach, `turn/start`, notification
  drain;
- Telegram-origin Codex live E2E with command/file/permission approval;
- TUI-origin Codex live E2E where Telegram sees the TUI user input without
  relying on `UserPromptSubmit`;
- takeover E2E where a pending Codex HITL request remains recoverable after
  `thread/resume`.
