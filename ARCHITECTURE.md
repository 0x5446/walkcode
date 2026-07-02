# WalkCode V3 Architecture

WalkCode V3 is a clean-slate channel-native runtime. The architecture is not a
compatibility wrapper around the legacy Feishu/tmux/hook server.

## Runtime Identity

A running instance has one identity line:

```text
WALKCODE_ENV_FILE
  -> WALKCODE_CHANNEL
  -> bot/app credentials
  -> WALKCODE_AGENT
  -> WALKCODE_STATE_PATH
```

That means:

```text
1 runtime = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

Claude Code and Codex are separate runtimes. They must not share a bot token,
Lark app identity, state file, or long-running process.

## Core Boundaries

```text
IM ChannelAdapter
  -> InboundEvent
  -> Orchestrator
  -> AgentTransport
  -> AgentEvent
  -> DurableOutbox
  -> ChannelAdapter.send_view
```

- `ChannelAdapter` owns IM-specific parsing, callbacks, attachments, and message
  rendering.
- `Orchestrator` owns session routing, authorization, single-writer leases,
  interaction state, idempotency, takeover state, and outbox enqueueing.
- `AgentTransport` owns product-specific headless execution and resume.
- `JsonFileStateStore` persists sessions, interaction tokens, auth state,
  inbound ledger, and durable outbox atomically.

The old tmux pane, Feishu thread id, and hook callback shapes are not core V3
identities.

## Channels

Telegram is the first default local deployment channel.

Telegram session placement is capability-driven:

- forum supergroup with topic-management rights: one topic per agent session;
- private chat with bot private-topic mode: one topic per agent session;
- plain private chat or non-topic group: root reply-chain fallback.

The durable channel binding key is:

```text
channel_kind + account_id + chat_id + thread_id + root_message_id
```

Lark/Feishu is a peer `ChannelAdapter`, not a lower-priority agent mode. It uses
`LARK_*` configuration and should map sessions to native Lark topics/threads
when V3 live ingress is enabled and E2E-gated.

## Agents

`WALKCODE_AGENT` is required and product-level:

- `claude` maps internally to the Claude headless transport.
- `codex` maps internally to the Codex app-server transport. In `auto` mode it
  prefers the managed Codex daemon control socket when the standalone daemon
  install exists, and falls back to `codex app-server --stdio` only when the
  shared daemon path is unavailable.

Users do not configure low-level transport names. Removed V3 env keys are
rejected:

```text
WALKCODE_CHANNELS
WALKCODE_PRIMARY_CHANNEL
WALKCODE_TRANSPORTS
WALKCODE_DEFAULT_TRANSPORT
WALKCODE_DEFAULT_AGENT
```

## IM-Started Sessions

For IM-started sessions, WalkCode launches the configured agent through the
headless transport and writes directly to that transport. Shell wrappers and
tmux are not part of this path.

Plain text in the bot starts or continues the bound agent session. `/claude` and
`/codex` are rejected because one bot cannot multiplex several coding agents.

## TUI-Started Sessions

TUI sessions are observed through `walkcode native hook`. The hook can create or
claim a V3 session when it receives durable resume ids:

- Claude: `agent_session_id`, `claude_session_id`, or `session_id`;
- Codex: `thread_id` or `codex_thread_id`.

IM input to a TUI-owned session is read-only at first. If the user chooses
takeover from IM, WalkCode can terminate only an authorized local process with
`allow_terminate=true`, resume the headless transport, and submit the blocked
input. It never injects IM text into a live TUI.

## Reliability

V3 keeps the mature product capabilities but re-implements them inside the new
boundaries:

- atomic JSON state persistence;
- pending sessions and durable bindings;
- inbound dedupe;
- durable outbox and retry/dead-letter state;
- permission and AskUserQuestion interaction state;
- hook dedupe for observed TUI sessions;
- session health/watchdog views;
- takeover confirmation with single-writer protection.

Legacy implementation details no longer define the architecture:

- no tmux key injection for IM-started sessions;
- no Feishu-only runtime config;
- no `walkcode serve/start` daemon as the V3 product path;
- no shared bot that switches between Claude and Codex.

## Migration Checks

Before a V3 runtime consumes IM updates:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env telegram
```

The runtime gate reports old LaunchAgents, old `walkcode hook` configs, shell
wrappers, and `FEISHU_*` env files as blocking cleanup items. Competing
consumers must be stopped before running `walkcode native serve`.

Detailed design history lives under [docs/adr](docs/adr) and
[docs/design/channel-native-v3-implementation.md](docs/design/channel-native-v3-implementation.md).
The latest progress/TODO export is
[docs/reports/2026-07-02-channel-native-v3-progress-todo.md](docs/reports/2026-07-02-channel-native-v3-progress-todo.md).
