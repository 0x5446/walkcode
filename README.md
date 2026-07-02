# WalkCode

WalkCode V3 是 channel-native 的 Coding Agent runtime。它把 IM 当成一等交互界面，而不是把旧版 Feishu/tmux/hook runtime 包一层继续使用。

## V3 模型

一个本地运行实例只绑定一条清晰身份线：

```text
1 runtime = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

- Channel 使用 `WALKCODE_CHANNEL=telegram|lark` 选择。
- Coding Agent 使用 `WALKCODE_AGENT=claude|codex` 显式绑定。
- 显式设置 `WALKCODE_ENV_FILE` 时，该 env 文件里的 Channel/Agent 身份优先，避免当前 shell 的旧变量串台。
- Claude Code 和 Codex 必须使用两个 bot、两个 env、两个 state、两个 runtime。
- 同一个 bot 里不支持 `/claude`、`/codex` 切 agent；这些命令会被拒绝。
- Telegram 优先使用 forum topic / private topic 做 `1 agent session = 1 topic`，不具备 topic 能力时退回 root reply-chain。
- Lark/飞书是同级 `ChannelAdapter`。当前 V3 默认落地优先 Telegram；Lark live ingress 需要独立 E2E gate 后再标记 deployable。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

安装脚本只做 V3 路径：

- 安装 `uv` 和 `walkcode` CLI；
- 把 `claude-agent-sdk` 安装进 `walkcode` 的 uv tool 环境；
- 创建 `~/.walkcode/telegram-claude.env` V3 模板；
- 阻断旧版 LaunchAgent、hook、legacy shell wrapper、`FEISHU_*` env 残留；
- 不安装 tmux wrapper；
- 不写旧版 `walkcode hook`；
- 不启动旧版 `walkcode serve/start`。

## 最小配置

`~/.walkcode/telegram-claude.env`：

```bash
WALKCODE_CHANNEL=telegram
TELEGRAM_BOT_TOKEN=123456:telegram-bot-token
WALKCODE_AGENT=claude
WALKCODE_STATE_PATH=/Users/you/.walkcode/telegram-claude-state.json
WALKCODE_CWD=/Users/you/.walkcode/workspace
```

Codex 使用另一个 bot 和 env：

```bash
WALKCODE_CHANNEL=telegram
TELEGRAM_BOT_TOKEN=987654:telegram-codex-bot-token
WALKCODE_AGENT=codex
WALKCODE_STATE_PATH=/Users/you/.walkcode/telegram-codex-state.json
WALKCODE_CWD=/Users/you/.walkcode/workspace
```

## 本地运行

先检查配置和 agent 能力：

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native doctor
```

在仓库 checkout 里跑模块级 gate：

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env agent
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/telegram-claude.env telegram
```

确认 `telegram` gate 报 `safe_to_run_serve_once: true` 后再消费 update：

```bash
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve --once --poll-timeout 0
WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env walkcode native serve
```

长期运行建议用 launchd 或其他进程管理器直接执行 `walkcode native serve`。不要用旧版 `walkcode start`。

## 从旧版迁移

V3 不继承旧版 Feishu/tmux/hook runtime。切换前清理：

- 卸载或停掉运行 `walkcode serve` / `walkcode start` 的 `~/Library/LaunchAgents/com.walkcode*.plist`。
- 把 `~/.claude/settings.json`、`~/.codex/hooks.json` 里的 `walkcode hook ...` 改成 `walkcode native hook ...`，仅在需要 TUI 只读观测和 takeover 时配置。
- `~/.agent-control-plane/agent-wrappers.sh` 只能保留 V3 纯转发 helper；包含 tmux、旧 `walkcode hook/serve/start/status/test-inject`、旧 WalkCode env 或 `FEISHU_*` 的 wrapper 必须清掉。
- 把旧 `~/.walkcode/*.env` 里的 `FEISHU_*` 迁走或转成 Lark 专用 `LARK_*`，不要让它们参与 V3 Telegram runtime。
- 给 Claude/Codex 分配独立 bot、env、state 和 runtime。

更多部署与验收细节见 [docs/channel-native-local-deploy.md](docs/channel-native-local-deploy.md)。
当前关键进展和 TODO 导出见
[docs/reports/2026-07-02-channel-native-v3-progress-todo.md](docs/reports/2026-07-02-channel-native-v3-progress-todo.md)。

## 开发验证

```bash
uv run --with pytest python -m pytest tests/test_channel_native_*.py
uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
```
