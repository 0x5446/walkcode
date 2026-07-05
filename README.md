# WalkCode

WalkCode V3 是 channel-native 的 Coding Agent runtime。它把 IM 当成一等交互界面，而不是把旧版 Feishu/tmux/hook runtime 包一层继续使用。

## V3 模型

一个本地运行实例只绑定一条清晰身份线：

```text
1 runtime = 1 Profile = 1 Channel = 1 bot/app identity = 1 Coding Agent
```

- Profile 使用 `WALKCODE_PROFILE=work|personal` 命名实例，派生默认 state 路径和 launchd label；每个 profile 通过 `WALKCODE_CLAUDE_CONFIG_DIR` / `WALKCODE_CODEX_HOME` 完全隔离 agent 的凭证与配置（codex 每个 profile 一个独立 app-server daemon）。
- Channel 使用 `WALKCODE_CHANNEL=lark|telegram` 选择。
- Coding Agent 使用 `WALKCODE_AGENT=claude|codex` 显式绑定。
- 显式设置 `WALKCODE_ENV_FILE` 时，该 env 文件里的身份优先；hook 命令必须显式携带 `WALKCODE_ENV_FILE`（无隐式默认）。
- Claude Code 和 Codex 必须使用两个 bot、两个 env、两个 state、两个 runtime。
- 同一个 bot 里不支持 `/claude`、`/codex` 切 agent；这些命令会被拒绝。
- **Lark/飞书是首发部署渠道**：同一个 adapter 通过 `LARK_OPENAPI_DOMAIN` 同时支持公司飞书（open.feishu.cn）和 Lark（open.larksuite.com）；会话按话题 reply-chain 放置，卡片体系移植自 V2 已验证的飞书 UI（权限三按钮卡、AskUserQuestion 三模式、健康卡）。
- Telegram 是架构验证通道（代码与测试保留，不再打磨 UX）；forum topic 会话放置逻辑仍然可用。

标准本地部署是 {work, personal} × {claude, codex} 的实例矩阵（可为不同模型路由
加更多 profile），见 [docs/lark-profile-deploy.md](docs/lark-profile-deploy.md)。

## 双端同步：终端与 IM 共驾同一个 Claude 会话

Claude 会话以 daemon-native 方式运行时（`claude --bg` 启动后 attach，或用
[部署文档](docs/lark-profile-deploy.md)里的 profile wrapper 裸启动），终端 TUI
和飞书/Lark **同时可读可写同一个会话**：

- **IM 直写**：在会话话题里发消息，文字直接注入终端会话（等同终端敲入回车），
  并收到「✅ 已发送到终端会话」回执；终端侧的输入与模型回答也实时同步回话题。
- **权限审批在 IM 完成**：会触发权限确认的工具（Bash / Edit / Write 等，减去
  你 allow 规则已覆盖的）渲染为飞书卡片——允许 / 始终允许 / 拒绝，点选即刻在
  终端会话生效。实现是一个阻塞的 PreToolUse hook（"gate"），只用 Claude Code
  的公开 hook 协议，不依赖私有 API。
- **AskUserQuestion 在 IM 作答**：模型提问渲染为选项卡（单选 / 多选 / 自由
  文本），提交后答案注入工具输入，终端不再弹对话框。
- **状态同步**：运行中 / 等待确认 / 已结束的状态卡实时更新；在终端处理过的
  确认也会回传话题。

启用：把 claude profile `settings.json` 的 PreToolUse hook 换成 `--gate` 变体
（必须放大 hook 超时，否则 60s 默认值会先杀掉等待中的 hook）：

```json
"PreToolUse": [{"matcher": "", "hooks": [{
  "type": "command",
  "command": "WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env walkcode native hook PreToolUse --agent claude --gate",
  "timeout": 1830
}]}]
```

可调项：`WALKCODE_CLAUDE_GATE_MODE=auto|off|ask_only`、
`WALKCODE_CLAUDE_GATE_TIMEOUT`（默认 1800s，超时按拒绝处理）、
`WALKCODE_CLAUDE_GATE_TOOLS`（替换默认权限拦截工具集）。安全兜底：walkcode
服务没在运行时 hook 自动弃权，终端原生权限提示照常工作；
`WALKCODE_CLAUDE_DAEMON_MODE=off` 可整体回退到只读观察 + takeover 模式。

设计与协议细节：[docs/design/claude-daemon-multi-ui-sync.md](docs/design/claude-daemon-multi-ui-sync.md)、
[docs/design/daemon-appserver-protocol-reference.md](docs/design/daemon-appserver-protocol-reference.md)、
[docs/adr/0046](docs/adr/0046-claude-daemon-reply-and-subscribe-sync.md)。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

安装脚本只做 V3 路径：

- 安装 `uv` 和 `walkcode` CLI；
- 把 `claude-agent-sdk`、`lark-oapi` 安装进 `walkcode` 的 uv tool 环境；
- 阻断旧版 LaunchAgent、hook、legacy shell wrapper、`FEISHU_*` env 残留；
- 不安装 tmux wrapper；
- 不写旧版 `walkcode hook`；
- 不启动旧版 `walkcode serve/start`。

## 最小配置

`~/.walkcode/work-claude.env`（公司飞书租户）：

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

personal 实例用 Lark 租户的 bot 与 `LARK_OPENAPI_DOMAIN=https://open.larksuite.com`；
codex 实例把 `WALKCODE_AGENT=codex` 并配 `WALKCODE_CODEX_HOME`。完整 4 实例矩阵和
launchd 模板见 [docs/lark-profile-deploy.md](docs/lark-profile-deploy.md)，全部
变量见 [.env.example](.env.example)。

## 本地运行

先检查配置、凭证和 agent 能力：

```bash
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native doctor
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native debug lark
```

在仓库 checkout 里跑模块级 gate：

```bash
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env config
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env runtime
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env state
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env outbox
uv run --with claude-agent-sdk python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env agent
uv run --with claude-agent-sdk --with lark-oapi python scripts/channel_native_debug.py --env-file ~/.walkcode/work-claude.env lark
```

启动常驻服务（长期运行建议 launchd）：

```bash
WALKCODE_ENV_FILE=~/.walkcode/work-claude.env walkcode native serve
```

会话内可用命令：`/status`、`/sessions`、`/model`、`/takeover`、
`/repo <目录> <任务>`（在 `WALKCODE_WORKSPACE_ROOTS` 白名单内选择仓库启动新会话）。

## 从旧版迁移

V3 不继承旧版 Feishu/tmux/hook runtime。切换前清理：

- 卸载或停掉运行 `walkcode serve` / `walkcode start` 的 `~/Library/LaunchAgents/com.walkcode*.plist`。
- 把各 profile 的 `{CLAUDE_CONFIG_DIR}/settings.json`、`{CODEX_HOME}/hooks.json` 里的 hook 改成 `WALKCODE_ENV_FILE=... walkcode native hook ...`，仅在需要 TUI 只读观测和 takeover 时配置。
- `~/.agent-control-plane/agent-wrappers.sh` 只能保留 V3 纯转发 helper；包含 tmux、旧 `walkcode hook/serve/start/status/test-inject`、旧 WalkCode env 或 `FEISHU_*` 的 wrapper 必须清掉。
- 把旧 `~/.walkcode/*.env` 里的 `FEISHU_*` 转成 `LARK_*`（有 `LegacyFeishuEnvConverter` 提示）。
- 给每个 profile × agent 分配独立 bot、env、state 和 runtime。

更多部署与验收细节见 [docs/lark-profile-deploy.md](docs/lark-profile-deploy.md)
与 [docs/channel-native-local-deploy.md](docs/channel-native-local-deploy.md)（Telegram，已降级）。
当前关键进展和 TODO 导出见
[docs/reports/2026-07-02-channel-native-v3-progress-todo.md](docs/reports/2026-07-02-channel-native-v3-progress-todo.md)。

## 开发验证

```bash
uv run --with pytest python -m pytest tests/test_channel_native_*.py
uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
```
