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

When a Claude session runs daemon-native (`claude --bg` then attach, or a bare
launch through the profile wrappers in the
[deploy doc](docs/lark-profile-deploy.md)), the terminal TUI and Feishu/Lark
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

To see the actual system prompt / tool calls / token usage a profile sends
upstream, point that profile's Claude base URL at a local reverse proxy:

```bash
WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=http://127.0.0.1:18899
```

WalkCode wraps this value into a standalone `--settings` override
(`{"env": {"ANTHROPIC_BASE_URL": ...}}`) for the Claude Agent SDK — it does not
install, launch, or supervise any proxy process, so it never competes with
WalkCode's own Claude-session launch path. (Confirmed live: a plain
process-env override alone does not take effect, because Claude Code
re-applies the `env` block from this profile's own
`CLAUDE_CONFIG_DIR/settings.json` on top of it; `--settings` is the layer that
actually wins, same as what claude-tap itself uses when it launches the
`claude` client directly.) This override does not read or merge whatever
`WALKCODE_CLAUDE_SETTINGS` already points to — configuring both on the same
profile is rejected at startup, so pick one (see
[ADR 0047](docs/adr/0047-claude-tap-debug-proxy-passthrough.md) for why). You
run the proxy yourself, e.g. with
[claude-tap](https://github.com/liaohch3/claude-tap) (`uv tool install
claude-tap`), started with this profile's own upstream env in no-launch mode:

```bash
claude-tap --tap-no-launch --tap-client claude --tap-port 18899 --tap-no-open
```

claude-tap detects its upstream target from **its own process environment**
(`ANTHROPIC_VERTEX_BASE_URL` / `CLAUDE_CODE_USE_VERTEX` / `ANTHROPIC_BASE_URL`,
etc.), so launch it with the same set this profile already uses — otherwise it
falls back to the default `api.anthropic.com`.

If your upstream is a non-Google Vertex gateway whose path shape isn't the
standard `/v1/projects/.../publishers/anthropic/models/...:rawPredict` (e.g. an
internal gateway that drops the `/v1` prefix), claude-tap's default path
allowlist blocks it (look for `Blocked non-API path` in the sidecar log) —
add `--tap-allow-path /projects` (or whatever prefix your gateway actually
uses).

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
