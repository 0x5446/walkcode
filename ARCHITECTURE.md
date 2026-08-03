# WalkCode V3 Architecture

WalkCode V3 is a clean-slate channel-native runtime. The architecture is not a
compatibility wrapper around the legacy Feishu/tmux/hook server.

## Runtime Identity

A running instance has one identity line:

```text
WALKCODE_ENV_FILE
  -> WALKCODE_PROFILE (work | personal)
  -> WALKCODE_CHANNEL
  -> bot/app credentials
  -> WALKCODE_AGENT
  -> WALKCODE_STATE_PATH (derived from profile+agent unless explicit)
```

That means:

```text
1 runtime = 1 Profile = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

Claude Code and Codex are separate runtimes. They must not share a bot token,
Lark app identity, state file, or long-running process.

The standard local deployment is four Lark/Feishu instances
({work, personal} x {claude, codex}, ADR 0043). Each profile pins its own agent
configuration at the process-environment level: `WALKCODE_CLAUDE_CONFIG_DIR`
flows into the Claude SDK subprocess as `CLAUDE_CONFIG_DIR`, and
`WALKCODE_CODEX_HOME` flows into codex subprocess/daemon spawns as
`CODEX_HOME` — giving each profile its own credentials, settings, and (for
codex) its own managed app-server daemon and control socket.

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

Lark/Feishu is the first deployable channel (ADR 0044). One
`LarkChannelAdapter` serves both tenants; `LARK_OPENAPI_DOMAIN` selects
open.feishu.cn (work) or open.larksuite.com (personal). Ingress is the
lark-oapi WebSocket client bridged from its callback thread into the asyncio
serve loop; card callbacks are acknowledged inline within Feishu's ~3s window
and button-state changes go through the durable outbox's `im.v1.message.patch`
edits. View models are rendered to Feishu interactive cards / post markdown by
`channel_native/lark_cards.py`, which ports the V2-proven card layouts
(permission three-button card, AskUserQuestion three modes, health card).
Session placement is one session per reply chain. Both placement paths *try* to
root the chain on a bot-sent **health card** that doubles as the session's
status card (`root_message_id == health_message_id`):

- channel-initiated: the user's non-reply message triggers a root card, and the
  user's own text is forwarded into the thread as the first reply;
- TUI-observed: the hook that first sees a terminal session sends the root card
  (a rootless binding is healed onto one by the maintenance tick).

Rooting on a card rather than on text is a hard requirement, not a preference:
Feishu caps text-message edits (sender-only, 20 edits, admin-set time window —
error codes 230071/230072/230075) while card patches are uncapped, and the
collapsed thread list shows the root, so the root is the surface a session
title has to live on for the life of the session.

**The equality is a best case, not an invariant.** If the root card cannot be
sent, the channel-initiated path falls back to rooting on the user's own
message (and does not later heal), and the TUI-observed path may start rootless
(the maintenance tick does heal that one). Code must therefore test
`health_message_id == root_message_id` rather than assume it. Two behaviours
hang off that test:

- when the status card IS the root, a failed edit does not fall back to sending
  a replacement card — Feishu cannot replace a thread root, so the replacement
  would land as a child and strand the root on its old title. The pointer is
  kept and the next refresh retries. That retry is bounded
  (`ROOT_CARD_EDIT_RETRY_BUDGET`, and a `PermanentDeliveryError` skips the
  budget entirely): a deleted or edit-window-expired root would otherwise fail
  identically forever, so the session gives up on the root and demotes to a
  child status card — a stale headline beats no live status at all;
- the status-card dedup cache is keyed by `(message_id, fingerprint)`, so
  moving the card (heal, demotion, send-fallback) invalidates the old entry by
  itself.

## Session titles

`Session.cached_title` is what the root card shows. It is refreshed at turn end
through one entry point (`Orchestrator._maybe_refresh_session_title`), fed by
all four turn-end signals: hooks.json `Stop` / `UserPromptSubmit` for the two
TUI paths, and the event stream's `TURN_COMPLETED` for codex app-server and
claude headless (those two never load the user-level hooks.json). On the codex
JSON-RPC shape the completion notification carries no assistant text, so the
drain loop accumulates `item/agentMessage/delta` text as fallback material.

Which title wins is decided by source rank, not by arrival order
(`SESSION_TITLE_SOURCE_RANKS`): `tui_hook` (session-id placeholder) <
`turn_digest` (last assistant message) < `initial_user_input` (the user's first
prompt) < `llm_summary` (reserved). A rank upgrade applies immediately; a
same-rank overwrite is throttled and only allowed for rolling sources —
`initial_user_input` is one-shot, so later prompts never repaint the title.
The throttle watermark (`Session.title_refreshed_at`) is persisted on the
session rather than the transport, because one codex thread hops between TUI
and app-server across takeover/handback.

Telegram is a peer `ChannelAdapter` kept as the architecture-validation
channel (code and tests stay; no further UX investment). Telegram session
placement is capability-driven:

- forum supergroup with topic-management rights: one topic per agent session;
- private chat with bot private-topic mode: one topic per agent session;
- plain private chat or non-topic group: root reply-chain fallback.

The durable channel binding key is:

```text
channel_kind + account_id + chat_id + thread_id + root_message_id
```

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

New sessions run in `WALKCODE_CWD` by default. With
`WALKCODE_WORKSPACE_ROOTS` configured, `/repo <dir> <task>` starts the session
in an allowlisted repository instead; resolution is realpath-contained so
`..` and symlinks cannot escape a root (ADR 0045).

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
