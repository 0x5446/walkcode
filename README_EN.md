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

The standard local deployment is four Lark/Feishu instances
({work, personal} x {claude, codex}); see
[docs/lark-profile-deploy.md](docs/lark-profile-deploy.md).

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
