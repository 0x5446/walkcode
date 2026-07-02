# Channel-native V3 Progress and TODO

Date: 2026-07-02

This report is the current working export for WalkCode V3. It summarizes the
implemented behavior, local verification, and remaining TODO after the Codex
managed app-server control-socket work.

## Product Decisions Now Aligned

- One local runtime instance has exactly one channel, one bot/app identity, one
  coding agent, and one state file.
- Telegram is the first deployable channel. Lark/Feishu remains a peer
  `ChannelAdapter`, but live V3 Lark ingress is not marked deployable until it
  has its own real E2E gate.
- Claude Code and Codex use separate Telegram bots, env files, state files, and
  launchd services. One bot does not route both agents through `/claude` and
  `/codex`.
- Telegram uses one forum/private topic per agent session when the chat
  supports topics. When topics are unavailable, it falls back to a root
  reply-chain.
- IM-started sessions use the configured headless/structured transport.
  TUI-started sessions begin as read-only observations and require takeover
  before IM input is submitted to the agent.
- V3 does not depend on tmux. Legacy tmux wrappers, old `walkcode hook`, old
  `walkcode serve/start`, and `FEISHU_*` env leakage are cleanup targets.

## Implementation Progress

- Channel-native runtime boundaries are in place:
  `ChannelAdapter -> Orchestrator -> AgentTransport -> DurableOutbox`.
- Atomic JSON state persistence, inbound dedupe, durable outbox, auth state,
  callback tokens, interaction state, and session registry round-trip through
  the V3 state store.
- Telegram native runtime is configured for long polling, topic-per-session,
  callback acknowledgement, command menu installation, Markdown rendering, and
  compact tool/progress views.
- TUI observation and takeover are implemented with single-writer protection.
  IM input to a TUI-owned topic prompts takeover instead of being injected into
  a live TUI.
- Claude headless transport is wired through `claude-agent-sdk`. The installed
  uv tool environment has `claude-agent-sdk`, so launchd services can use the
  normal `walkcode` CLI.
- Codex Telegram-origin sessions use `CodexAppServerTransport`.
- `CodexManagedAppServerClient` now starts/uses the managed Codex daemon and
  talks directly to the daemon Unix control socket using WebSocket JSON-RPC.
  It no longer relies on `codex app-server proxy`.
- Codex standalone daemon 0.142.5 is installed locally under
  `~/.codex/packages/standalone/current/codex`.
- Codex `auto` mode now selects the managed daemon path when the standalone
  install exists, and falls back to `codex app-server --stdio` only when the
  shared daemon path is unavailable.
- Codex server-request HITL foundation is implemented for command approvals,
  file approvals, permission-profile approvals, tool request-user-input, and
  basic MCP elicitation form mode.
- Durable HITL request/decision storage exists separately from short callback
  tokens.
- Pre-takeover pending HITL from observed sessions is stale-marked after
  takeover instead of being silently treated as still answerable.

## Current Local Verification

Unit and contract tests:

```text
uv run --with pytest python -m pytest tests/test_channel_native_*.py -q
297 passed

uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py
passed
```

Codex daemon and managed client:

```text
codex app-server daemon version
status=running
managedCodexVersion=0.142.5
appServerVersion=0.142.5
socketPath=~/.codex/app-server-control/app-server-control.sock

CodexManagedAppServerClient.request("thread/start", ...)
returned a non-empty thread id from the managed daemon control socket.
```

Live agent smoke:

```text
telegram-codex agent-smoke --live
ok=true
event_types=[turn.delta, turn.completed]

telegram-claude agent-smoke --live
ok=true
event_types included turn.delta, tool.started, tool.completed, turn.completed
```

Codex remote TUI smoke:

```text
codex --remote unix://~/.codex/app-server-control/app-server-control.sock ...
connected to the same daemon and returned walkcode-remote-smoke-ok
```

Installed runtime services:

```text
uv tool install --force --editable /Users/alpha/Documents/workspace/walkcode --with claude-agent-sdk
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-claude
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-codex

com.walkcode.telegram-claude loaded
com.walkcode.telegram-codex loaded
```

Runtime diagnostics:

```text
telegram-claude doctor: agent_status.available=true
telegram-codex doctor: agent_status.available=true
runtime gate: competing_consumer_count=0, legacy_remnant_count=0
state gate: inbound_in_progress=0, pending_bindings=0
codex outbox: pending_count=0, dead_count=0
claude outbox: pending_count=0, dead_count=4
```

The four Claude dead outbox records are historical Telegram 429 failures from
old sessions. They are not pending work and do not block new sessions.

## Remaining TODO

1. Finish the long-lived Codex shared app-server session client.
   The current managed client can handshake, request, and smoke-test the daemon
   path. The remaining work is response/notification correlation for long-lived
   multi-client sessions, streaming subscription ownership, reconnect, and
   routing by `threadId`.

2. Make Codex TUI observation primarily app-server based.
   The verified `codex --remote unix://...` path proves the TUI can attach to
   the same daemon. The next step is making WalkCode-managed Codex TUI launches
   use that path by default, so TUI user input, assistant output, tool calls,
   status, and HITL come from the same app-server stream.

3. Complete Codex TUI takeover by `threadId`.
   The target is: identify the Codex thread, resume the same thread from
   Telegram, enforce single-writer ownership, submit the blocked Telegram input,
   and avoid forked TUI/Telegram conversations.

4. Complete live HITL E2E gates.
   Required live cases are command approval, file approval, permission-profile
   approval, tool request-user-input, MCP elicitation where feasible, and
   takeover with a pending HITL request.

5. Keep improving Telegram process UI.
   Tool/progress messages already have compact rendering, but the UX still
   needs real-session validation for thinking/tool/status presentation,
   Markdown edge cases, topic creation copy, and command menu behavior.

6. Run user-visible Telegram acceptance.
   The services are running, but final product acceptance still needs fresh
   manual Telegram messages in the Claude and Codex groups to validate:
   General-topic task creation, topic-per-session routing, normal follow-up,
   TUI read-only transcript sync, takeover prompt, and post-takeover writing.

7. Promote Lark only after its own live gate.
   Lark remains a peer adapter in the architecture. It should not be described
   as deployable until live ingress, topic/thread placement, callbacks, and HITL
   are verified against real Lark credentials.

## Main References

- `ARCHITECTURE.md`
- `.env.example`
- `docs/channel-native-local-deploy.md`
- `docs/design/channel-native-v3-implementation.md`
- `docs/adr/0027-single-channel-agent-config.md`
- `docs/adr/0041-codex-unified-app-server-client-architecture.md`
- `docs/adr/0042-hitl-takeover-and-telegram-full-capability.md`
