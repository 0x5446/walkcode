# WalkCode

WalkCode V3 is a channel-native Coding Agent runtime. It treats IM as the
primary interaction surface instead of wrapping the old Feishu/tmux/hook runtime.

## V3 Model

Each local runtime instance has one clear identity line:

```text
1 runtime = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

- Select the channel with `WALKCODE_CHANNEL=telegram|lark`.
- Bind the coding agent with `WALKCODE_AGENT=claude|codex`.
- When `WALKCODE_ENV_FILE` is explicit, the env file owns the Channel/Agent
  identity so stale shell variables do not cross-wire runtimes.
- Claude Code and Codex require separate bots, env files, state files, and runtime processes.
- `/claude` and `/codex` are not in-bot agent routers; they are rejected.
- Telegram prefers one forum/private topic per agent session and falls back to a root reply-chain when topics are unavailable.
- Lark/Feishu is a peer `ChannelAdapter`. Telegram is the first default local deploy path; live V3 Lark ingress needs its own E2E gate before it is marked deployable.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

The installer follows only the V3 path:

- installs `uv` and the `walkcode` CLI;
- installs `claude-agent-sdk` into the `walkcode` uv tool environment;
- creates a V3 `~/.walkcode/telegram-claude.env` template;
- blocks on legacy LaunchAgent, hook, legacy shell-wrapper, and `FEISHU_*` env remnants;
- does not install tmux wrappers;
- does not write legacy `walkcode hook` config;
- does not start legacy `walkcode serve/start`.

## Minimal Config

`~/.walkcode/telegram-claude.env`:

```bash
WALKCODE_CHANNEL=telegram
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
WALKCODE_AGENT=claude
WALKCODE_STATE_PATH=/Users/you/.walkcode/telegram-claude-state.json
WALKCODE_CWD=/Users/you/.walkcode/workspace
```

Codex uses another bot and env:

```bash
WALKCODE_CHANNEL=telegram
TELEGRAM_BOT_TOKEN=987654:telegram-codex-bot-token
WALKCODE_AGENT=codex
WALKCODE_STATE_PATH=/Users/you/.walkcode/telegram-codex-state.json
WALKCODE_CWD=/Users/you/.walkcode/workspace
```

## Run Locally

Check config and agent capability first:

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native doctor
```

Run module gates from a repository checkout:

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env telegram
```

Only consume updates after the Telegram gate reports `safe_to_run_serve_once: true`:

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve --once --poll-timeout 0
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve
```

For long-running local deploy, point launchd or another process manager directly
at `walkcode native serve`. Do not use the legacy `walkcode start` daemon path.

## Migrating From Legacy

V3 does not inherit the old Feishu/tmux/hook runtime. Before switching:

- unload `~/Library/LaunchAgents/com.walkcode*.plist` files that run `walkcode serve` or `walkcode start`;
- replace `walkcode hook ...` in `~/.claude/settings.json` or `~/.codex/hooks.json` with `walkcode native hook ...` only if TUI observation and takeover are needed;
- keep `~/.agent-control-plane/agent-wrappers.sh` only as a V3 pure pass-through helper; remove wrappers that contain tmux, old `walkcode hook/serve/start/status/test-inject`, old WalkCode env, or `FEISHU_*`;
- move old `~/.walkcode/*.env` files containing `FEISHU_*` away from the V3 Telegram runtime, or convert them to Lark-only `LARK_*`;
- give Claude and Codex separate bots, env files, state files, and runtime processes.

See [docs/channel-native-local-deploy.md](docs/channel-native-local-deploy.md) for deployment and validation details.
The current progress/TODO export is
[docs/reports/2026-07-02-channel-native-v3-progress-todo.md](docs/reports/2026-07-02-channel-native-v3-progress-todo.md).

## Development Checks

```bash
uv run --with pytest python -m pytest tests/test_channel_native_*.py
uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
```
