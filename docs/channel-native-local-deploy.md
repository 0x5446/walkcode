# Channel-native V3 Runtime Reference

Date: 2026-06-27 (Telegram content removed 2026-09-30, ADR 0069)

> **Deployment steps live in `docs/lark-profile-deploy.md`** (ADR 0043/0044):
> bot setup, profile directories, env files, launchd, per-instance acceptance.
> This document keeps the channel-independent runtime reference: commands,
> `/reload`, TUI hook observation, debug gates, HITL status, and release
> posture. Lark/Feishu is the only channel; the Telegram channel and its
> deploy recipe were removed in ADR 0069.

For V3 validation, legacy `walkcode serve/start/hook`, tmux wrappers, and
Feishu-only env files are cleanup targets. They must not share a bot, state
file, or hook config with V3.

## Scope

Currently live:

- `walkcode native doctor`
- `walkcode native serve` (Lark WebSocket ingress)
- `walkcode native debug lark`
- `walkcode native hook`
- `WALKCODE_PROFILE` instance split with per-profile `CLAUDE_CONFIG_DIR` /
  `CODEX_HOME` isolation
- channel-native state persistence
- Claude agent capability probing
- Codex app-server capability probing when the `codex` CLI is installed
- Codex managed daemon control-socket mode when the standalone Codex daemon
  install exists (one daemon per CODEX_HOME/profile)
- TUI hook observation and takeover for authorized local processes
- E2E gate status reporting

Hook commands and CLI runs must set `WALKCODE_ENV_FILE` explicitly; there is no
implicit default env file (ADR 0043).

`WALKCODE_AGENT=claude|codex` selects the only Coding Agent served by one bot.
Do not use one bot for both Claude Code and Codex. When `WALKCODE_ENV_FILE`
points at an env file, values in that file own the runtime identity; stale
shell exports such as `WALKCODE_AGENT=claude` must not override a Codex env
file. `/claude` and `/codex` are rejected as old agent-selector commands.
`WALKCODE_CHANNELS` and `WALKCODE_PRIMARY_CHANNEL` are rejected, and
`WALKCODE_CHANNEL=telegram` fails with a config error naming ADR 0069.

## Local Package Smoke

Before publishing, build the package and run the V3 CLI from the wheel:

```bash
uv build
WALKCODE_WHEEL="$(ls -t dist/walkcode-*-py3-none-any.whl | head -1)"

env WALKCODE_ENV_FILE=/tmp/walkcode-native-v3.env \
  WALKCODE_CHANNEL=lark \
  LARK_APP_ID=cli_fake \
  LARK_APP_SECRET=fake \
  WALKCODE_AGENT=claude \
  WALKCODE_CWD=/tmp \
  WALKCODE_STATE_PATH=/tmp/walkcode-native-state.json \
  uv run --no-project --no-cache --with "$WALKCODE_WHEEL" \
  walkcode native doctor --json
```

## WalkCode Commands

WalkCode intercepts its own controls before agent submission, so `/status` is
not forwarded to Claude Code or Codex as prompt text:

```text
/status    current session or runtime status
/sessions  active sessions in this chat
/model     show local model inventory or switch model when the transport supports it
/skills    current skill-introspection support
/takeover  takeover fallback for TUI-origin sessions
/reload    restart this session's agent backend, keeping the conversation (alias /restart)
/repo      /repo <dir> <task>: start a new task in an allowlisted workspace (ADR 0045)
/commands  WalkCode and agent command catalog
```

`//cmd` reaches the agent as `/cmd`, for agent-native commands shadowed by a
WalkCode one (e.g. `//model`). Unknown slash commands are passed through to the
agent only inside an existing session topic. In the root chat they are
rejected, so a stray `/compact` or `/help` does not create a new coding session.

`/model` inventory is intentionally local and explicit. Claude reads the
configured `WALKCODE_CLAUDE_SETTINGS` file (or the profile's
`walkcode_model_choices`). Codex reads
`WALKCODE_CODEX_CONFIG`/`WALKCODE_CODEX_MODELS_CACHE` when set, otherwise
`$CODEX_HOME/config.toml` and `$CODEX_HOME/models_cache.json`. It is not
presented as a live provider catalog.

### `/reload`

Restarts the agent backend under a session **without losing the conversation**.
The session stops with reason `backend_reload`, which is inside
`_CHANNEL_REVIVAL_STOP_REASONS`, so the next message revives that same session
(ADR 0054) on a freshly started backend.

Why it has to exist: config that is only read when the backend starts — MCP
servers above all — is otherwise unreachable for a long-running session.
Measured against codex 0.144.5:

| action | newly added MCP loaded? |
| --- | --- |
| app-server started while the config already listed it, then `thread/resume` | yes |
| config edited while the app-server runs, then `thread/resume` | **no** |
| same, then `thread/start` | yes |

The app-server snapshots `mcp_servers` at PROCESS start; `thread/resume` reuses
that snapshot and only `thread/start` re-reads the file. Before `/reload` the
only way in was abandoning the thread for a new one, losing all its context.

Per transport:

- `claude_headless` — closing reaps the per-session worker; the next resume
  spawns a fresh one. No extra restart step.
- `codex_app_server` — the session is stopped with `turn/interrupt` +
  `thread/unsubscribe` (both thread-scoped: verified that unsubscribing one
  thread leaves siblings loaded, subscribed and readable), then the app-server
  process itself is replaced, because that is where the config snapshot lives.
  This is profile-wide: sibling sessions on the same `CODEX_HOME` lose their
  connection and reconnect on their next message, and the reply says so.

Refusals:

- TUI-observed sessions — that backend is a terminal the user owns; restarting
  it belongs to the consented takeover flow.
- a turn still in flight — cycling the backend would kill it silently.
- sessions with no durable resume ref yet (no `agent_session_id` / `thread_id`,
  i.e. the first turn has not landed) — reloading would stop a session nothing
  can revive, turning `/reload` into a silent permanent kill.

Implementation note: the app-server subprocess is stopped with SIGTERM, not
SIGKILL. `codex app-server --stdio` is a node wrapper around the vendor binary;
SIGKILL reaps only the wrapper and leaves the real server orphaned, still
holding the stdout pipe, so `await process.wait()` never returns and the
"restarted" server is still the old one.

Tool calls are shown as one compact editable activity card in the session
topic: tool name and lifecycle state, never full stdout or tool output. Final
agent text still arrives as ordinary session output.

## Legacy Cleanup

V3 does not require the old `claude` / `codex` shell wrappers for IM-started
headless sessions. Those sessions are launched directly by the configured agent
adapter.

Before running real V3 validation:

- unload old `~/Library/LaunchAgents/com.walkcode*.plist` files that run
  `walkcode serve` or `walkcode start`;
- replace old `walkcode hook ...` configs with `walkcode native hook ...` only
  if you need TUI observation;
- remove `~/.zshrc` sourcing of `~/.agent-control-plane/agent-wrappers.sh`
  unless it is an explicit personal TUI alias;
- rename old `FEISHU_*` env values to their `LARK_*` counterparts;
- give each V3 runtime its own `WALKCODE_STATE_PATH`.

The runtime debug gate reports these remnants:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env runtime
```

## TUI Hook Observation

`walkcode native hook` is the V3 hook ingress for local TUI sessions. It reads
one JSON object from stdin:

```bash
walkcode native hook sync --agent claude < hook.json
walkcode native hook stop --agent codex --json < hook.json
walkcode native hook PreToolUse --agent claude --defer < hook.json
```

Channel environment context: every channel-driven agent conversation carries a
preamble telling the agent the user is on Feishu (Lark) and cannot see the
local machine; anything the user must see goes into the reply as a URL
reachable from their phone/browser (plain text replies are relayed
automatically — the agent must not reach for chat/IM tools). Claude
headless sessions get it appended to the system prompt via the SDK's
`claude_code` preset on every launch/resume (post-takeover resumes included);
Codex app-server threads get it prepended to the first turn of each
launched/resumed thread (codex has no append-system-prompt surface). Read-only
TUI observation injects nothing.

For real TUI hook configs, use `--defer` and omit `--json`: the hook is written
to a local spool and the command exits quickly with no stdout. The running
`walkcode native serve` process drains that spool from an independent
maintenance task and performs the topic/status/tool-progress updates, so
read-only TUI transcript sync does not wait on channel ingress. `--json` is
only for manual debugging.

For Codex TUI observation, `$CODEX_HOME/hooks.json` must include
`SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PermissionRequest`, and `Stop`. `UserPromptSubmit` is what opens the topic
(ADR 0066): a TUI session appears in the channel with its first prompt, titled
by that prompt; opening a TUI without typing anything posts nothing. Without
`UserPromptSubmit` the TUI never appears in the channel at all.

TUI exit is detected by a maintenance pass every 30 seconds (one batched `ps`
over the observed sessions' recorded processes, matched by pid and start
time): a closed terminal's topic turns "ended" within about 30 seconds, no
restart or `SessionEnd` hook needed. codex-cli (verified 0.144.5) does not emit `MessageDisplay` or
`PostToolUseFailure` — configuring them is harmless but dead. Assistant text
therefore has no codex hook carrier: mid-turn narration is mirrored
incrementally from the rollout transcript (ADR 0055), and the turn-final text
rides the `Stop` hook's `last_assistant_message`.

Since codex-cli 0.157 a TUI started with the default `~/.codex` attaches to
the managed app-server daemon WalkCode also uses, and the daemon runs every
thread's hooks (ADR 0064). **Observation is still hook-based today**: WalkCode
attributes codex hooks by turn id, and its app-server drain consumes only the
turns WalkCode itself started. TUI turns after a takeover are mirrored from
the event stream instead of hooks (ADR 0065; see the Codex notes below). The
target architecture — reading everything from the app-server protocol instead
of reconstructing it from hooks — is
`docs/adr/0041-codex-unified-app-server-client-architecture.md`.

Required durable resume ids:

- Claude: `agent_session_id`, `claude_session_id`, or `session_id`
- Codex: `thread_id` or `codex_thread_id`

TUI-created observed sessions go to `WALKCODE_LARK_TUI_CHAT_ID`, or to the only
entry of `LARK_ALLOWED_CHAT_IDS` when exactly one is configured. IM input to a
TUI-owned session is not injected into the live TUI; it is blocked, rendered as
a takeover prompt, and submitted only after confirmed takeover. Readonly is
enforced by the writer/takeover state machine, not by closing the topic. If the
TUI has already stopped, takeover resumes the structured Claude/Codex transport
and skips process termination.

Tool hooks from observed TUI sessions are compact progress signals, not full
stdout/stderr mirrors. Configure `PreToolUse`, `PostToolUse`,
`PostToolUseFailure` (Claude only — codex never emits it), and permission
hooks with `--defer`.

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
process and takeover reports that it cannot start automatically. This applies
to a TUI that holds the thread in its own process. A hook run inside a codex
app-server (the shared daemon) records `{"controller_kind": "shared_app_server"}`
instead: the daemon owns the thread, so takeover resumes it alongside the TUI
and stops nothing (ADR 0064). Claude Code
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
uv run --with claude-agent-sdk python scripts/channel_native_debug.py tests lark
```

Then run the real-environment probes:

```bash
ENV=~/.walkcode/work-claude.env
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV agent-smoke
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file $ENV lark
```

Use these optional flags when you want `walkcode native doctor` to mark the
real gates as enabled:

```bash
WALKCODE_E2E_LARK=1
WALKCODE_E2E_LARK_CHAT_ID=oc_xxx
WALKCODE_E2E_CLAUDE_HEADLESS=1
WALKCODE_E2E_CODEX_APP_SERVER=1
WALKCODE_E2E_CWD=/Users/you/.walkcode/e2e/channel-native-smoke
```

Codex defaults to `WALKCODE_CODEX_APP_SERVER_MODE=auto`. In `auto` mode,
WalkCode starts/uses the managed Codex app-server daemon when
`$CODEX_HOME/packages/app-server-daemon/current/bin/codex` resolves to a real
file (the standalone CLI package alone is not enough; a dangling `current`
link counts as missing), then connects directly to the daemon's Unix control
socket with a WebSocket JSON-RPC client. Otherwise it falls back to an
isolated `codex app-server --stdio` client. If that install is missing on
another machine, keep `auto` or force `stdio`; do not force `daemon` until the
standalone install exists.

Since codex 0.157 the TUI under the same `CODEX_HOME` is served by that shared
daemon too. After a channel takes over such a TUI session (ADR 0064), turns the
user keeps typing in the TUI are mirrored to the topic from the event stream
(ADR 0065): one "⌨️ 终端输入" message and, when the turn ends, one summary
(commands run, narration, final reply). Mirroring never changes the session's
state. `WALKCODE_CODEX_MIRROR=off` turns it off and restores ADR 0064 behavior.
A TUI turn whose events arrive while the mirror is unsubscribed (between a
reconnect and the next reconcile pass, every few seconds) is not mirrored; the
conversation itself is unaffected.

`agent-smoke` is dry-run by default. It reports the configured agent adapter
capability without launching Claude/Codex. Use `agent-smoke --live` only when
you intentionally want a real agent launch and minimal prompt outside IM. Live
smoke must observe a non-error agent event; `session.error` makes the gate fail
and usually means auth/provider settings are missing.

## HITL Status

- Claude headless: permission prompts and AskUserQuestion go through the SDK
  `can_use_tool` callback; TUI sessions use the blocking PreToolUse gate
  (ADR 0068).
- Codex app-server: server-request handling is implemented for command
  execution approval, file change approval, permission-profile approval, tool
  request-user-input, and basic MCP elicitation form mode (ADR 0042).

For TUI-origin sessions the channel may show read-only HITL context. It must
not answer a TUI-owned prompt until takeover has resumed the structured
transport and verified that the native request is still pending. After
takeover succeeds, pre-takeover pending HITL requests are marked stale and
rendered as stale context instead of being answered blindly (ADR 0051 continues
them as fresh cards by default).

Expected state gate before consuming IM updates:

- `state_file.load_ok: True`
- `write_probe.ok: True`

`sessions.expired_writer_leases` is informational only (ADR 0059). The lease
is stamped when a writer is acquired and never renewed while a turn runs, so
any session mid-turn for longer than the lease TTL shows an "expired" lease —
that is the normal shape of a healthy long-running turn, not a stale writer.
Lease expiry no longer blocks submits. To spot a genuinely wedged session,
look at `last_progress_at` / `last_progress_event` staleness instead.

If state contains read-only external TUI observations whose recorded local
process has already exited, repair them:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py \
  --env-file ~/.walkcode/work-codex.env \
  state --repair-stale-external-tui --json
```

This repair creates a `*.bak-*` copy of the state file, marks only dead observed
TUI sessions stopped, and never kills a live TUI process.

If `runtime` reports competing consumers, stop or unload those processes before
running a second consumer of the same bot. On macOS this can include
LaunchAgent-managed legacy services such as `com.walkcode` or
`com.walkcode-codex`. The same runtime gate reports legacy launch agents, old
`walkcode hook` configs, shell wrappers, and old `FEISHU_*` env files as
blocking cleanup items.

## Release Posture

V3 release validation uses the native runtime as the product path:

- do not run old `walkcode serve/start/hook` against the same bot or hooks;
- require `WALKCODE_CHANNEL`, `WALKCODE_AGENT`, and a dedicated
  `WALKCODE_STATE_PATH` (or `WALKCODE_PROFILE`);
- block install/upgrade when legacy LaunchAgent, old hook, shell wrapper, or
  `FEISHU_*` remnants are present;
- require the module-level gates and real E2E evidence before publishing a
  local deploy recipe;
- keep top-level install/upgrade docs on the V3 native path; legacy runtime
  material belongs only in historical notes or cleanup guidance.
