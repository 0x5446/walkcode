# Channel-native V3 Local Deploy (Telegram, demoted)

Date: 2026-06-27 (demoted 2026-07-02)

> **The primary deployment is now the Feishu/Lark 4-instance profile setup —
> see `docs/lark-profile-deploy.md` (ADR 0043/0044).** Telegram remains a peer
> `ChannelAdapter` used for architecture validation; its code and tests stay,
> but it receives no further UX investment. This document is kept for the
> Telegram-specific runtime details that are still accurate.

This is the local deployment path for the clean-slate channel-native runtime.
For V3 validation, legacy `walkcode serve/start/hook`, tmux wrappers, and
Feishu-only env files are cleanup targets. They must not share a bot, webhook,
state file, or hook config with V3.

Current implementation progress and remaining TODO are exported in
`docs/reports/2026-07-02-channel-native-v3-progress-todo.md`.

## Scope

Currently live:

- `walkcode native doctor`
- `walkcode native serve` (Telegram polling and Lark WebSocket, dispatched by
  `WALKCODE_CHANNEL`)
- `walkcode native hook`
- Telegram long polling ingress
- Lark/Feishu WebSocket ingress with V2-ported card rendering
  (`docs/lark-profile-deploy.md`)
- `WALKCODE_PROFILE` work/personal instance split with per-profile
  `CLAUDE_CONFIG_DIR` / `CODEX_HOME` isolation
- channel-native state persistence
- Claude agent capability probing
- Codex app-server capability probing when the `codex` CLI is installed
- Codex managed daemon control-socket mode when the standalone Codex daemon
  install exists (one daemon per CODEX_HOME/profile)
- TUI hook observation and takeover for authorized local processes
- E2E gate status reporting

Not yet claimed:

- full Telegram/Lark/Claude/Codex product acceptance without explicit
  `WALKCODE_E2E_*` gates and fresh user-visible IM validation

Note: hook commands and CLI runs must set `WALKCODE_ENV_FILE` explicitly; the
old implicit `~/.walkcode/telegram-claude.env` fallback was removed with the
profile split (ADR 0043).

## Minimal Telegram + Claude Setup

Install or upgrade through the V3 scripts or `walkcode upgrade`; those paths
install the uv tool with `claude-agent-sdk` available to the `walkcode` CLI.
If Claude doctor reports unavailable after a manual install, reinstall with
`uv tool install --with claude-agent-sdk --force --reinstall --refresh-package walkcode`.

When both Claude and Codex Telegram runtimes are running, Telegram diagnostics
may see multiple `walkcode native serve` processes. That is expected for
separate bot/env/state files. Same-bot conflicts are determined by Telegram's
`409 Conflict` and pending-update diagnostics, not by process count alone.
If `getUpdates` returns `409 Conflict` while a native service is running,
`safe_to_run_serve_once` stays false, but the deployment can still be healthy:
the long-running service owns polling.

Create or edit `~/.walkcode/telegram-claude.env`:

```bash
WALKCODE_CHANNEL=telegram
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
WALKCODE_AGENT=claude

WALKCODE_CWD=/Users/you/.walkcode/workspace
WALKCODE_STATE_PATH=/Users/you/.walkcode/telegram-claude-state.json

# Optional when Claude Code uses a non-default provider/profile.
WALKCODE_CLAUDE_SETTINGS=/Users/you/.claude/profiles/vertex.json
```

`WALKCODE_AGENT=claude|codex` selects the only Coding Agent served by this bot.
Do not use one bot for both Claude Code and Codex. Run a second env/state pair
with a second bot token when both agents are needed.
When `WALKCODE_ENV_FILE` points at one of these files, values in that file own
the runtime identity; stale shell exports such as `WALKCODE_AGENT=claude` must
not override a Codex env file.

In that bot, plain text starts or continues that agent's session:

```text
summarize this repo
```

`/claude` and `/codex` are rejected as old agent-selector commands. They do not
switch the bot to another Coding Agent.

Check the runtime:

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native doctor
```

Run module-level diagnostics before any command that consumes Telegram updates:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent-smoke
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env telegram
```

The Telegram diagnostic is read-only: it peeks with `getUpdates` without an
offset and does not start Claude/Codex. It also reports sanitized bot/chat
topic capability fields such as `target_chat.recommended_placement` and
`target_chat.topic_per_session_available` without printing the token or chat id.
For forum supergroups it also reports `target_chat.bot_admin.can_manage_topics`;
that value must be true before WalkCode can create one topic per session.
Only run the consuming smoke step when it reports `safe_to_run_serve_once:
True`.

Run one polling cycle:

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve --once --poll-timeout 0
```

`--once` is only a smoke/debug mode. It consumes the currently pending updates
and exits. The bot will not answer later Telegram messages unless a long-running
poller is still active.

Run continuously:

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve
```

For local manual testing, run the poller in the foreground. For long-running
local deployment, use launchd or another process manager; do not reuse the old
`walkcode start` daemon path:

```bash
cat > ~/Library/LaunchAgents/com.walkcode.telegram-claude.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.walkcode.telegram-claude</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/walkcode</string>
    <string>native</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>WALKCODE_ENV_FILE</key>
    <string>/Users/YOU/.walkcode/telegram-claude.env</string>
  </dict>
  <key>WorkingDirectory</key>
  <string>/Users/YOU/.walkcode/workspace</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/Users/YOU/.walkcode/telegram-claude.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/YOU/.walkcode/telegram-claude.err.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.walkcode.telegram-claude.plist
```

## Local Package Smoke

Before publishing, build the package and run the V3 CLI from the wheel:

```bash
uv build
WALKCODE_WHEEL="$(ls -t dist/walkcode-*-py3-none-any.whl | head -1)"

env WALKCODE_ENV_FILE=/tmp/walkcode-native-v3.env \
  WALKCODE_CHANNEL=telegram \
  TELEGRAM_BOT_TOKEN=123456:fake-token \
  WALKCODE_AGENT=claude \
  WALKCODE_CWD=/tmp \
  WALKCODE_STATE_PATH=/tmp/walkcode-native-state.json \
  uv run --no-project --no-cache --with "$WALKCODE_WHEEL" \
  walkcode native doctor --json
```

## Channel Selection

One local runtime instance binds exactly one IM channel. Telegram and Lark are
peer `ChannelAdapter` types in the project, but they are selected per instance:

```bash
WALKCODE_CHANNEL=telegram
WALKCODE_AGENT=claude
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
```

or:

```bash
WALKCODE_CHANNEL=lark
WALKCODE_AGENT=claude
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=xxx
LARK_RECEIVE_ID=ou_xxx
LARK_RECEIVE_ID_TYPE=open_id
```

Run separate runtime instances with separate env files and state paths if both
Telegram and Lark ingress are needed. `WALKCODE_CHANNELS` and
`WALKCODE_PRIMARY_CHANNEL` are rejected so one process cannot silently compete
for two IM ingress streams.

In this V3 slice, Telegram is the live ingress. V3 live Lark ingress still
needs a separate runtime adapter and real E2E gate before it is marked
deployable.

## Telegram Session Placement

Telegram placement is selected by platform capability, not by the core session
model. The best UX is one native topic per WalkCode session when the target chat
supports it; otherwise the runtime falls back to one session per root
message/reply chain.

Runtime identity is:

```text
channel_kind + account_id + chat_id + thread_id + root_message_id
```

- `chat_id` is the private chat, group, or supergroup.
- `thread_id` is the Telegram topic id when the chat uses native topics.
- `root_message_id` is the bot message that anchors one WalkCode session.

Recommended Telegram setups:

- `private chat`: simplest setup. It works immediately, but if the bot does not
  have private topic mode enabled, multiple concurrent sessions share one chat
  and use reply-chain fallback.
- `forum supergroup`: clearest Telegram UX. Enable topics for the supergroup,
  add the bot as an administrator, and grant topic-management rights. WalkCode
  can then create one topic per Claude Code or Codex session, including
  sessions first observed from a local TUI hook. For root text tasks in the
  group, disable the bot's Telegram privacy mode through BotFather or otherwise
  ensure the bot receives the root message.
- `private chat with bot topic mode`: also maps cleanly to one topic per
  session, but requires enabling Telegram bot private topics for that bot.

Group-per-session is not the default. It requires pre-provisioned groups and
creates extra membership, notification, and archive management work.

Current V3 bot model:

- one runtime instance selects one IM channel through `WALKCODE_CHANNEL`;
- one runtime instance selects one Coding Agent through `WALKCODE_AGENT`;
- one Telegram bot token or Lark app identity belongs to that one Coding Agent;
- one agent session should occupy one native topic/thread whenever the channel
  supports it.

For the cleanest Telegram group UX, use one forum supergroup per agent bot.
A shared group with multiple agent bots requires stricter mention/reply
discipline or bot privacy settings to avoid multiple bots consuming the same
root text.

For Telegram forum supergroups, set the runtime's allowed chat/TUI target to
the supergroup id. The bot must be able to manage topics; otherwise WalkCode
falls back to the root reply-chain behavior.
Confirm this with `target_chat.bot_admin.can_manage_topics=true` in
`scripts/channel_native_debug.py telegram --json`. Telegram's
`createForumTopic` contract for forum supergroups requires the bot to be a chat
administrator with the `can_manage_topics` administrator right. Member-level
topic creation settings, or `getMe.allows_users_to_create_topics` for private
bot chats, are not enough for automatic per-session topic creation in a
supergroup.

When WalkCode creates a forum topic it also picks a distinct topic icon. It
prefers a random default custom emoji from Telegram's forum-topic icon sticker
set, and falls back to a random allowed topic color when that sticker list is
not available.

## Telegram Native Commands and Progress

Telegram bot commands are installed on polling service startup:

```text
/status    current session or runtime status
/sessions  active sessions in this chat
/model     show local model inventory or switch model when the transport supports it
/skills    current skill-introspection support
/takeover  takeover fallback for TUI-origin sessions
/commands  installed WalkCode and agent command catalog
```

The command menu is agent-specific. A Claude bot registers WalkCode controls plus
known Claude Code slash commands; a Codex bot registers WalkCode controls plus
known Codex slash commands. Command-menu sync is best-effort and runs after
polling work, so a transient `setMyCommands` failure cannot block inbound
messages. WalkCode-owned controls are intercepted before agent submission, so
`/status` is not forwarded to Claude Code or Codex as ordinary prompt text.
Unknown slash commands are only passed through to the agent inside an existing
session topic or reply chain. In General/root chat they are rejected, so a stray
`/compact` or `/help` does not accidentally create a new coding session.

In long-running polling mode, WalkCode confirms a Telegram update after the
turn is submitted to Claude/Codex. It does not wait for the full agent response
before acknowledging the offset, so one slow turn does not block later Telegram
messages. Agent output continues to stream through the session topic.

TUI observed-session refresh, deferred TUI hook drain, and outbox flush run as
independent maintenance tasks. A slow or stuck Telegram `getUpdates` long-poll
must not starve read-only TUI transcript sync.

Telegram command names cannot contain hyphens. Hyphenated agent commands are
registered with underscore aliases in the native menu, such as `/add_dir`, and
WalkCode translates them back to the agent-native slash command, such as
`/add-dir`, before forwarding inside a session.

`/model` inventory is intentionally local and explicit. Claude reads the
configured `WALKCODE_CLAUDE_SETTINGS` file. Codex reads
`WALKCODE_CODEX_CONFIG`/`WALKCODE_CODEX_MODELS_CACHE` when set, otherwise
`~/.codex/config.toml` and `~/.codex/models_cache.json`. It is not presented as
a live provider catalog.

When WalkCode receives user text, it best-effort adds a `✅` reaction to that
message. After accepted user input is routed to its target session topic,
WalkCode sends Telegram `typing` chat action before submitting the turn to the
agent. Together these are the visible "received and processing" signals.
Telegram client check marks are not treated as the source of truth.

Tool calls are shown as a compact editable `Agent activity` message in the
session topic. WalkCode shows the tool name and lifecycle state, but not full
stdout or tool output. Final agent text still arrives as ordinary session
output. There is no `/progress` toggle; activity is part of the default Telegram
UX.

If the Telegram UI is ambiguous, a logged-in Telegram user session can grant
the exact administrator right through MTProto:

```bash
TELEGRAM_API_ID=... TELEGRAM_API_HASH=... \
  uv run --with telethon python scripts/telegram_grant_manage_topics.py --phone +15551234567
```

Run `--dry-run` first to inspect the target bots without requiring a user API
session.

To run Claude Code and Codex together, start two runtime instances with separate
env files, state paths, and bot/app identities:

```text
Claude bot -> WALKCODE_AGENT=claude -> claude state file
Codex bot  -> WALKCODE_AGENT=codex  -> codex state file
```

See `docs/adr/0033-telegram-session-placement-and-bot-model.md`.

## Legacy Cleanup

V3 does not require the old `claude` / `codex` shell wrappers for IM-started
headless sessions. Those sessions are launched directly by the configured agent
adapter. The default migration posture is to remove legacy wiring unless you
intentionally keep a private local TUI shortcut outside WalkCode's runtime.

Before running real V3 validation:

- unload old `~/Library/LaunchAgents/com.walkcode*.plist` files that run
  `walkcode serve` or `walkcode start`;
- replace old `walkcode hook ...` configs with `walkcode native hook ...` only
  if you need TUI observation;
- remove `~/.zshrc` sourcing of `~/.agent-control-plane/agent-wrappers.sh`
  unless it is an explicit personal TUI alias;
- move old `~/.walkcode/*.env` files containing `FEISHU_*` to a legacy backup
  directory, or convert them to `LARK_*` only when validating the Lark adapter;
- give each V3 runtime its own `WALKCODE_STATE_PATH`.

The runtime debug gate reports these remnants:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
```

## TUI Hook Observation

`walkcode native hook` is the V3 hook ingress for local TUI sessions. It reads
one JSON object from stdin:

```bash
walkcode native hook sync --agent claude < hook.json
walkcode native hook stop --agent codex --json < hook.json
walkcode native hook PreToolUse --agent claude --defer < hook.json
```

For real TUI hook configs, use `--defer` and omit `--json`: the hook is written
to a local spool and the command exits quickly with no stdout. The running
`walkcode native serve` process drains that spool from an independent
maintenance task and performs Telegram topic/status/tool-progress updates. This
drain is not gated by Telegram `getUpdates` returning, so read-only TUI
transcript sync can continue while the IM ingress long-poll is slow or
temporarily stuck. `--json` is only for manual debugging.

For current fallback Codex TUI observation, `~/.codex/hooks.json` must include
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PermissionRequest`, and `Stop`. Missing `UserPromptSubmit` means the channel
can show assistant output from later hooks but cannot show the user's TUI
input. codex-cli (verified 0.144.5) does not emit `MessageDisplay` or
`PostToolUseFailure` — configuring them is harmless but dead. Assistant text
therefore has no codex hook carrier: mid-turn narration is mirrored
incrementally from the rollout transcript (ADR 0055), and the turn-final text
rides the `Stop` hook's `last_assistant_message`.

The target Codex architecture is different: use a shared Codex app-server
endpoint so the Codex TUI and WalkCode's Telegram runtime attach to the same
`threadId`. In that model, Telegram reads user input, assistant output, tool
events, approvals, and request-user-input events from the app-server protocol
instead of reconstructing them from hooks. See
`docs/adr/0041-codex-unified-app-server-client-architecture.md`.

Required durable resume ids:

- Claude: `agent_session_id`, `claude_session_id`, or `session_id`
- Codex: `thread_id` or `codex_thread_id`

Optional Telegram routing for TUI-created observed sessions:

```bash
WALKCODE_TELEGRAM_TUI_CHAT_ID=123456789
WALKCODE_TELEGRAM_TUI_THREAD_ID=77
TELEGRAM_ALLOWED_USER_IDS=456789
```

If `WALKCODE_TELEGRAM_TUI_CHAT_ID` is not set and exactly one
`TELEGRAM_ALLOWED_CHAT_IDS` value exists, that chat is used. If the target is a
forum supergroup and `WALKCODE_TELEGRAM_TUI_THREAD_ID` is not set, WalkCode
creates one topic for each observed TUI session. IM input to a TUI-owned session
is not injected into the live TUI; it is blocked, rendered as a takeover prompt,
and submitted only after confirmed takeover.

WalkCode does not close or reopen Telegram topics to express read-only state.
The topic stays open; readonly is enforced by the writer/takeover state machine.
If the TUI has already stopped, takeover resumes the structured Claude/Codex
transport and skips process termination.

Tool hooks from observed TUI sessions are compact progress signals, not full
stdout/stderr mirrors. Configure `PreToolUse`, `PostToolUse`,
`PostToolUseFailure` (Claude only — codex never emits it), and permission
hooks with `--defer`; Telegram will update
one editable `Agent activity` message in the session topic when an observed
session already exists.

Automatic takeover requires a hook-provided `terminate_ref` such as:

```json
{
  "terminate_ref": {
    "controller_kind": "process",
    "process_ref": {
      "pid": 12345,
      "allow_terminate": true
    }
  }
}
```

Without `allow_terminate=true`, WalkCode will not kill a still-running TUI
process and takeover reports that it cannot start automatically. Claude Code
`Stop` hooks are turn-completion events, not proof that the TUI process exited,
so they do not remove the termination requirement by themselves.

## Module-level Debug Gates

Use these gates when preparing a real E2E run:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests agent-smoke
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests telegram
```

Then run the real-environment probes:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent-smoke
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env telegram
```

Use these optional flags when you want `walkcode native doctor` to mark the
real gates as enabled:

```bash
WALKCODE_E2E_TELEGRAM=1
WALKCODE_E2E_TELEGRAM_CHAT_ID=123456789
WALKCODE_E2E_CLAUDE_HEADLESS=1
WALKCODE_E2E_CODEX_APP_SERVER=1
WALKCODE_E2E_CWD=/Users/you/.walkcode/e2e/channel-native-smoke
```

Codex defaults to `WALKCODE_CODEX_APP_SERVER_MODE=auto`. In `auto` mode,
WalkCode starts/uses the managed Codex app-server daemon when the local Codex
standalone daemon install is present, then connects directly to the daemon's
Unix control socket with a WebSocket JSON-RPC client. If the standalone daemon
install is missing, it falls back to an isolated `codex app-server --stdio`
client.

The shared-daemon path is the target architecture because it can also be used
by `codex --remote`; see
`docs/adr/0041-codex-unified-app-server-client-architecture.md`. The current
local deployment is verified with `codex-cli 0.142.5`, the CLI-managed
standalone install at `~/.codex/packages/standalone/current/codex`, and the
control socket at `~/.codex/app-server-control/app-server-control.sock`.
If that install is missing on another machine, keep `auto` or force `stdio`;
do not force `daemon` until the standalone install exists.

`agent-smoke` is dry-run by default. It reports the configured agent adapter
capability without launching Claude/Codex. Use `agent-smoke --live` only when
you intentionally want a real agent launch and minimal prompt outside IM. Live
smoke must observe a non-error agent event; `session.error` makes the gate fail
and usually means auth/provider settings are missing.

## HITL Status

Telegram already has the neutral callback machinery for permission prompts,
AskUserQuestion prompts, and takeover prompts. Current HITL status:

- Claude headless: keep the existing adapter boundary and finish real SDK E2E
  before claiming full parity.
- Codex app-server: server-request handling is implemented for command
  execution approval, file change approval, permission-profile approval, tool
  request-user-input, and basic MCP elicitation form mode. See
  `docs/adr/0042-hitl-takeover-and-telegram-full-capability.md`.

For TUI-origin sessions, Telegram may show read-only HITL context. It must not
answer a TUI-owned prompt until takeover has resumed the structured transport
and verified that the native request is still pending. The safe implemented
behavior today is: after takeover succeeds, pre-takeover pending HITL requests
are marked stale and rendered as stale context instead of being answered
blindly.

Expected Telegram gate before `serve --once`:

- `bot.ok: True`
- `webhook.has_url: False`
- `runtime.competing_consumer_count: 0`
- `pending_updates.count` is `0`, or every pending item has `chat_allowed=True`
- if a pending item targets an existing session, `submit_would_accept=True`
- `safe_to_run_serve_once: True`

Expected state gate before consuming IM updates:

- `state_file.load_ok: True`
- `write_probe.ok: True`

`sessions.expired_writer_leases` is informational only (ADR 0059). The lease
is stamped when a writer is acquired and never renewed while a turn runs, so
any session mid-turn for longer than the lease TTL shows an "expired" lease —
that is the normal shape of a healthy long-running turn, not a stale writer.
Lease expiry no longer blocks submits and must not gate `serve --once`. To
spot a genuinely wedged session, look at `last_progress_at` /
`last_progress_event` staleness instead.

If state contains read-only external TUI observations whose recorded local
process has already exited, repair them before private-chat E2E:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py \
  --env-file ~/.walkcode/telegram-codex.env \
  state --repair-stale-external-tui --json
```

This repair creates a `*.bak-*` copy of the state file, marks only dead observed
TUI sessions stopped, and never kills a live TUI process.

If `runtime` reports competing consumers, stop or unload those processes before
running any command that consumes Telegram updates. On macOS this can include
LaunchAgent-managed legacy services such as `com.walkcode` or
`com.walkcode-codex`; they must not run while a module-level Telegram smoke is
being verified. The same runtime gate reports legacy launch agents, old
`walkcode hook` configs, shell wrappers, and old `FEISHU_*` env files as
blocking cleanup items.

## Release Posture

V3 release validation uses the native runtime as the product path:

- do not run old `walkcode serve/start/hook` against the same bot or hooks;
- require `WALKCODE_CHANNEL`, `WALKCODE_AGENT`, and a dedicated
  `WALKCODE_STATE_PATH`;
- block install/upgrade when legacy LaunchAgent, old hook, shell wrapper, or
  `FEISHU_*` remnants are present;
- require the module-level gates and real E2E evidence before publishing a
  local deploy recipe;
- keep top-level install/upgrade docs on the V3 native path; legacy runtime
  material belongs only in historical notes or cleanup guidance.
