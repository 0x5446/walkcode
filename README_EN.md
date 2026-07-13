# WalkCode

WalkCode V3 is a channel-native Coding Agent runtime. It treats IM as the
primary interaction surface instead of wrapping the old Feishu/tmux/hook runtime.

## V3 Model

Each local runtime instance has one clear identity line:

```text
1 runtime = 1 Profile = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

- `WALKCODE_PROFILE=work|personal` names the instance and derives the default
  state path and launchd label; each profile isolates its agent credentials
  and config through `WALKCODE_CLAUDE_CONFIG_DIR` / `WALKCODE_CODEX_HOME`
  (codex gets one managed app-server daemon per profile).
- Select the channel with `WALKCODE_CHANNEL=lark|telegram`.
- Bind the coding agent with `WALKCODE_AGENT=claude|codex`.
- When `WALKCODE_ENV_FILE` is explicit, the env file owns the instance
  identity; hook commands must carry `WALKCODE_ENV_FILE` explicitly (there is
  no implicit default).
- Claude Code and Codex require separate bots, env files, state files, and runtime processes.
- `/claude` and `/codex` are not in-bot agent routers; they are rejected.
- **Lark/Feishu is the first deployable channel**: one adapter serves both the
  company Feishu tenant (open.feishu.cn) and personal Lark tenant
  (open.larksuite.com) via `LARK_OPENAPI_DOMAIN`; sessions map to reply-chain
  topics, and the card UI is ported from the proven V2 Feishu design
  (three-button permission card, AskUserQuestion three modes, health card).
- Telegram remains the architecture-validation channel (code and tests stay;
  no further UX investment).

The standard local deployment is a {work, personal} x {claude, codex} instance
matrix (add more profiles for extra model routes); see
[docs/lark-profile-deploy.md](docs/lark-profile-deploy.md).

## Dual-Drive: Terminal and IM Share One Claude Session

When a Claude session runs daemon-native (a manual `claude --bg` then attach,
or the explicit dual-UI opt-in described in the
[deploy doc](docs/lark-profile-deploy.md); **since ADR 0050 the default is
single-master UI** — a bare wrapper launch is a plain TUI with read-only IM
observation plus takeover), the terminal TUI and Feishu/Lark
**read and write the same session at the same time**:

- **Direct write from IM**: a message in the session topic is injected into
  the terminal session (as if typed there), acknowledged with an emoji
  reaction on your message (text receipt as fallback); terminal-side input
  and model replies stream back into the topic.
- **Permission approvals on IM**: tools that would prompt for permission
  (Bash / Edit / Write, minus whatever your allow rules already cover) render
  as cards — Allow / Always allow / Deny — and a click takes effect in the
  terminal session immediately. The mechanism is a blocking PreToolUse hook
  (the "gate") built entirely on Claude Code's public hook protocol; no
  private APIs.
- **AskUserQuestion on IM**: model questions render as option cards
  (single / multi select / free text); submitted answers are injected into the
  tool input, so the terminal never shows the dialog.
- **State sync**: running / waiting-for-approval / ended status cards update
  live; confirmations handled on the terminal side sync back to the topic.

Enable it by switching the claude profile's PreToolUse hook to the `--gate`
variant (the enlarged hook timeout is required — the 60s default would kill
the waiting hook first):

```json
"PreToolUse": [{"matcher": "", "hooks": [{
  "type": "command",
  "command": "WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env walkcode native hook PreToolUse --agent claude --gate",
  "timeout": 1830
}]}]
```

Tunables: `WALKCODE_CLAUDE_GATE_MODE=auto|off|ask_only`,
`WALKCODE_CLAUDE_GATE_TIMEOUT` (default 1800s; on timeout the hook abstains and the native terminal prompt takes over),
`WALKCODE_CLAUDE_GATE_TOOLS` (replace the default gated tool set). Fail-safe:
when the walkcode service is not running the hook abstains and the native
terminal prompt flow keeps working; `WALKCODE_CLAUDE_DAEMON_MODE=off` reverts
to read-only observation + takeover entirely.

Design and protocol notes:
[docs/design/claude-daemon-multi-ui-sync.md](docs/design/claude-daemon-multi-ui-sync.md),
[docs/design/daemon-appserver-protocol-reference.md](docs/design/daemon-appserver-protocol-reference.md),
[ADR 0046](docs/adr/0046-claude-daemon-reply-and-subscribe-sync.md).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

The installer follows only the V3 path:

- installs `uv` and the `walkcode` CLI;
- installs `claude-agent-sdk` and `lark-oapi` into the `walkcode` uv tool environment;
- blocks on legacy LaunchAgent, hook, legacy shell-wrapper, and `FEISHU_*` env remnants;
- does not install tmux wrappers;
- does not write legacy `walkcode hook` config;
- does not start legacy `walkcode serve/start`.

## Minimal Config

`~/.walkcode/work-claude.env` (company Feishu tenant):

```bash
WALKCODE_PROFILE=work
WALKCODE_CHANNEL=lark
WALKCODE_AGENT=claude
LARK_APP_ID=cli_xxx
LARK_APP_SECRET=xxx
LARK_OPENAPI_DOMAIN=https://open.feishu.cn
LARK_ALLOWED_CHAT_IDS=oc_xxx
WALKCODE_CLAUDE_CONFIG_DIR=/Users/you/.claude-profiles/work
WALKCODE_CWD=/Users/you/.walkcode/workspace
WALKCODE_WORKSPACE_ROOTS=/Users/you/Documents/workspace
```

Personal instances use bots from a Lark tenant with
`LARK_OPENAPI_DOMAIN=https://open.larksuite.com`; codex instances set
`WALKCODE_AGENT=codex` plus `WALKCODE_CODEX_HOME`. The full four-instance
matrix and launchd templates live in
[docs/lark-profile-deploy.md](docs/lark-profile-deploy.md); all variables are
documented in [.env.example](.env.example).

### Optional: debug proxy (claude-tap)

To see the actual system prompt / tool calls / token usage each profile sends
upstream, attach a local [claude-tap](https://github.com/liaohch3/claude-tap)
reverse proxy to each claude profile (launchd-resident, starts at login,
auto-respawns on crash), with all traces aggregated in one dashboard at
http://127.0.0.1:19527:

```bash
uv tool install claude-tap
./scripts/claude-tap-setup.sh init      # scaffold ~/.walkcode/claude-tap/taps.conf
vi ~/.walkcode/claude-tap/taps.conf     # fill in port/upstream per profile
./scripts/claude-tap-setup.sh apply     # start taps, wire profile envs, restart instances (idempotent)
./scripts/claude-tap-setup.sh remove    # tear everything down, restore direct connection
```

The WalkCode-side switch is one line per profile env —
`WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` — managed by the
setup script. Under the hood WalkCode merges the profile's `settings.json` env
with the proxy address and passes it to the Claude Agent SDK via `--settings`
as a 0600 file: it does not install, launch, or supervise any proxy process,
and secrets never reach argv. Why it must work this way (plain env overrides
don't take effect; a `--settings` env map replaces the profile env wholesale)
is recorded in [ADR 0047](docs/adr/0047-claude-tap-debug-proxy-passthrough.md);
deployment details, the three upstream shapes (OAuth / Vertex gateway / native
Google Vertex), and troubleshooting live in
[docs/claude-tap-deploy.md](docs/claude-tap-deploy.md).

Note: `WALKCODE_CLAUDE_SETTINGS` cannot be combined with this switch on the
same profile (rejected at startup); once enabled, the profile's new headless
sessions hard-depend on the local tap — run `remove` to decouple when not
actively debugging. Terminal TUI sessions bypass the proxy by default; to
route them into the same dashboard, see the「可选：终端 TUI 会话接入」
(optional terminal TUI wiring) section of the deploy doc — a launch-time soft
dependency that falls back to a direct connection when the tap is not
listening.

## Run Locally

Check config, credentials, and agent capability first:

```bash
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native doctor
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native debug lark
```

Run module gates from a repository checkout:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env agent
uv run --with claude-agent-sdk --with lark-oapi python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env lark
```

Start the resident service (use launchd for long-running deploys):

```bash
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native serve
```

In-session commands: `/status`, `/sessions`, `/model`, `/takeover`, and
`/repo <dir> <task>` (start a new session inside the
`WALKCODE_WORKSPACE_ROOTS` allowlist).

## Migrating From Legacy

V3 does not inherit the old Feishu/tmux/hook runtime. Before switching:

- unload `~/Library/LaunchAgents/com.walkcode*.plist` files that run `walkcode serve` or `walkcode start`;
- replace hook commands in each profile's `{CLAUDE_CONFIG_DIR}/settings.json` or `{CODEX_HOME}/hooks.json` with `WALKCODE_ENV_FILE=... walkcode native hook ...` only if TUI observation and takeover are needed;
- keep `~/.agent-control-plane/agent-wrappers.sh` only as a V3 pure pass-through helper; remove wrappers that contain tmux, old `walkcode hook/serve/start/status/test-inject`, old WalkCode env, or `FEISHU_*`;
- convert old `FEISHU_*` env values to `LARK_*` (the `LegacyFeishuEnvConverter` prints suggestions);
- give each profile x agent its own bot, env file, state file, and runtime process.

See [docs/lark-profile-deploy.md](docs/lark-profile-deploy.md) and
[docs/channel-native-local-deploy.md](docs/channel-native-local-deploy.md)
(Telegram, demoted) for deployment and validation details.
The current progress/TODO export is
[docs/reports/2026-07-02-channel-native-v3-progress-todo.md](docs/reports/2026-07-02-channel-native-v3-progress-todo.md).

## Development Checks

```bash
uv run --with pytest python -m pytest tests/test_channel_native_*.py
uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
```
