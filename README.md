# WalkCode

[**English**](README_EN.md)

> **Code is cheap. Show me your talk.**

**Coding Agent 写代码，你散步。**

WalkCode 让 Coding Agent 在需要帮助时给你发消息，你用手机就能审批、回复、纠正方向。不用守在电脑前，也能掌控全局。

**边走边 Code，口喷编程。这就是 WalkCode。**

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> 聊天（话题）
                     <──tmux send-keys──  <──WS── （回复）
```

## 支持的 Coding Agent

| Coding Agent | 状态 | 说明 |
|--------|------|------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | 已支持 | 默认 Agent |
| [Codex CLI](https://github.com/openai/codex) | 已支持 | 需要独立飞书机器人 |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) / [Cline](https://github.com/cline/cline) / [Aider](https://github.com/Aider-AI/aider) 等 | 计划中 | 通过 Agent Adapter 扩展 |

> **每个 Agent 对应一个独立的飞书机器人。** 你给 Claude Code 机器人发消息就启动 Claude，给 Codex 机器人发消息就启动 Codex，体验清晰直觉。

## 为什么要用 WalkCode？

你离开了电脑。你的 Coding Agent 弹出一个权限确认，卡住了。没有 WalkCode，它等你回来。有了 WalkCode，手机震一下，你点"允许"，它接着干。

- **别让 Coding Agent 等你** —— 随时随地审批和回复
- **每个会话独立话题** —— 在话题中回复，必定送达正确的 Coding Agent
- **锁屏也能用** —— 基于 tmux，不依赖 GUI

工程师也有自己的移动互联网浪潮。

## 功能特性

**核心：**
- **权限审批** —— 直接在聊天中回复审批
- **问题回答** —— AskUserQuestion 交互式卡片，支持多问题顺序处理、多选（multiSelect）、自定义文本（Other）
- **文字回复** —— 在话题中回复文字，直接输入到对应终端
- **图文消息** —— 支持发送图片和富文本（图文混排），图片自动下载并传递给 Coding Agent
- **远程启动** —— 在聊天中发条消息，就能远程启动一个新的 Coding Agent
- **会话恢复** —— 话题中的 tmux 会话过期后，回复任意消息自动恢复
- **自动回收** —— 闲置超过 2 小时的 tmux 会话自动关闭并通知
- **多 Agent** —— 同时运行 Claude Code 和 Codex CLI，各自独立飞书机器人

**其他：**
- **多会话** —— 多个 Coding Agent 会话，一个实例，自动路由
- **会话持久化** —— 服务重启后自动恢复
- **表情回执** —— 随机表情回应确认送达
- **i18n** —— 自动检测系统语言（zh* 中文，其余英文）

## 快速开始（Claude Code）

> 下面以 Claude Code 为例。Codex CLI 的设置见 [多 Agent 设置](#多-agent-设置codex-cli)。

### 前置条件

- macOS
- [tmux](https://github.com/tmux/tmux)（`brew install tmux`）
- [uv](https://docs.astral.sh/uv/)（Python >= 3.13）
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 已安装
- [飞书](https://www.feishu.cn/)企业自建应用（免费）

### 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

自动完成：安装 tmux/uv → 通过 `uv tool install` 安装 WalkCode CLI → 创建 `.env` → 注入 Shell Wrapper → 配置 tmux 滚动历史 → 安装 Hooks。运行前可先[查看脚本内容](install.sh)。

打开一个新的终端窗口，或运行 `exec $SHELL` 重新加载当前 Shell 会话。

### 升级

```bash
walkcode upgrade
```

### 一键卸载

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/uninstall.sh | bash
```

### 配置与运行

#### 1. 创建飞书应用

1. 前往[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用
2. **添加应用能力** > 机器人
3. **权限管理** > 开通以下权限：
   - `im:message` — 读取消息
   - `im:message:send_as_bot` — 以机器人身份发送消息（同时覆盖消息更新）
   - `im:message.reactions:write_only` — 添加表情回复
4. **事件与回调** > 长连接模式 > 添加事件：
   - `im.message.receive_v1` — 接收消息
   - `card.action.trigger` — 接收卡片按钮点击
5. **版本管理与发布** > 创建版本 > 发布上线

#### 2. 编辑 `.env`

```bash
vim ~/.walkcode/.env
```

填入飞书应用的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。

#### 3. 获取 open_id

```bash
walkcode serve
```

在飞书中给机器人发任意消息，WalkCode 会在控制台直接打印发送者的 `open_id`。将其填入 `.env` 的 `FEISHU_RECEIVE_ID`，然后 Ctrl+C 并启动守护进程：

```bash
walkcode start
```

搞定。输入 `claude`，然后出门散步。

#### 4. （推荐）开机自启动

创建 launchd plist 文件，让 WalkCode 在登录时自动启动并在崩溃时自动重启：

```bash
cat > ~/Library/LaunchAgents/com.walkcode.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.walkcode</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/walkcode</string>
        <string>serve</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>WALKCODE_ENV_FILE</key>
        <string>/Users/YOU/.walkcode/.env</string>
        <key>HOME</key>
        <string>/Users/YOU</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>/Users/YOU/.walkcode</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>ExitTimeOut</key>
    <integer>15</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>/Users/YOU/.walkcode/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOU/.walkcode/launchd.err.log</string>
</dict>
</plist>
EOF
```

> **重要：**
> - 将 `/Users/YOU` 替换为你实际的用户目录。
> - 用 `walkcode serve`（前台 uvicorn）而非 `walkcode start`（daemonizer）。`start` 会 fork 后父进程立即退出，launchd 追不到真实 PID，`KeepAlive` 失效。`serve` 是前台阻塞，launchd 全程盯住 PID，崩溃即重启。
> - `KeepAlive { SuccessfulExit=false, Crashed=true }` 保证手动 `launchctl unload` 干净退出不触发重启，但异常崩溃会按 `ThrottleInterval=10s` 节流重拉。
> - `WALKCODE_ENV_FILE` 指向绝对路径，launchd 环境不经过 direnv/shell rc，不显式指定会找不到 Feishu 凭证。

```bash
launchctl load ~/Library/LaunchAgents/com.walkcode.plist     # 加载并启动
launchctl unload ~/Library/LaunchAgents/com.walkcode.plist   # 停止并卸载
launchctl list | grep walkcode                               # 查看状态（PID + 退出码）
```

> **注意：** launchd 加载后，**不要再手动 `walkcode start`** — 两个进程会抢同一端口互相踩脚。只能用 `launchctl load/unload` 管理，或先 `launchctl unload` 后再手动 `walkcode start` 做调试。

#### 5. （推荐）防止 macOS 系统休眠

WalkCode 依赖持续的网络连接来接收飞书消息。接外电时，建议禁止系统休眠（屏幕仍可正常关闭）：

```bash
sudo pmset -c sleep 0 && sudo pmset -c disksleep 0 && sudo pmset -c standby 0 && sudo pmset -c hibernatemode 0
```

此设置仅影响接外电时的行为，电池模式不受影响。

<details>
<summary>手动安装（逐步说明）</summary>

#### 1. 创建飞书应用

（同上）

#### 2. 安装

```bash
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/0x5446/walkcode.git
```

#### 3. 配置

```bash
mkdir -p ~/.walkcode && cp .env.example ~/.walkcode/.env
vim ~/.walkcode/.env  # 填入飞书 App ID 和 Secret
```

#### 4. 添加 Shell Wrapper

添加到 `~/.zshrc`（或 `~/.bashrc`）：

```bash
claude() {
  if [ -z "$TMUX" ]; then
    case "$1" in
      --version|-v|--help|-h|-p|--print)
        command claude "$@"
        return
        ;;
    esac
    local session="claude-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command claude $(printf '%q ' "$@")"
  else
    command claude "$@"
  fi
}
```

然后：`source ~/.zshrc`

#### 5. 配置 tmux 滚动历史

添加到 `~/.tmux.conf`：

```bash
set-option -ga terminal-overrides ',*:smcup@:rmcup@'
```

#### 6. 安装 Hooks

```bash
walkcode install-hooks
```

</details>

## 多 Agent 设置（Codex CLI）

WalkCode 支持同时运行多个 Agent，**每个 Agent 对应一个独立的飞书机器人**。以 Codex CLI 为例：

### 原理

```
飞书机器人 A (Claude)  ──>  WalkCode 实例 A (port 3001)  ──>  claude
飞书机器人 B (Codex)   ──>  WalkCode 实例 B (port 3002)  ──>  codex
```

每个实例有独立的 `.env`、端口、PID 文件、日志和状态，互不干扰。

### 步骤

#### 1. 安装 Codex CLI

```bash
npm install -g @openai/codex
```

#### 2. 创建第二个飞书应用

按照[创建飞书应用](#1-创建飞书应用)步骤，创建一个新的飞书机器人（如命名为 "Codex"）。

#### 3. 创建 Codex 实例配置

```bash
cat > ~/.walkcode/codex.env << 'EOF'
# Codex instance
WALKCODE_AGENT=codex
WALKCODE_INSTANCE=codex
PORT=3002

# Feishu App for Codex bot
FEISHU_APP_ID=cli_codex_xxx
FEISHU_APP_SECRET=xxx
FEISHU_RECEIVE_ID=ou_xxx

# Lark 国际版应用请启用这一行
# LARK_OPENAPI_DOMAIN=https://open.larksuite.com

# 认证方式（二选一）：
# 方式 A：ChatGPT 订阅（推荐）— 先运行 codex login 完成 OAuth 登录
#   Token 过期时 WalkCode 会自动发起 device-auth，你在手机上完成验证即可
# 方式 B：API Key
# OPENAI_API_KEY=sk-xxx
EOF
```

> `FEISHU_RECEIVE_ID` 必须用 Codex 机器人对应应用下获取到的 `open_id`。不要复用 Claude 机器人里打印出的 `open_id`，飞书/Lark 的 `open_id` 是按应用隔离的，跨应用会导致 `open_id cross app` 发送失败。

#### 4. 添加 Shell Wrapper

添加到 `~/.zshrc`（或 `~/.bashrc`），让本地运行 `codex` 时自动套 tmux：

```bash
codex() {
  if [ -z "$TMUX" ]; then
    local session="codex-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command codex --no-alt-screen $(printf '%q ' "$@")"
  else
    command codex "$@"
  fi
}
```

然后：`source ~/.zshrc`

> `--no-alt-screen` 让 Codex 的输出保留在 tmux 滚动历史中，WalkCode 需要它来捕获输出。

#### 5. 安装 Codex Hooks

```bash
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode install-hooks --agent codex
```

这会写入 `~/.codex/hooks.json`（hook 命令自动指向 port 3002）并启用 Codex 的 hooks feature flag。

安装后的 hook 命令会显式携带 `WALKCODE_AGENT=codex`、`WALKCODE_PORT=3002`，并保留 `WALKCODE_ENV_FILE`，避免 Codex 审批回包误用 Claude 协议。

#### 6. 启动 Codex 实例

```bash
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode start
```

现在你有两个飞书机器人：给 Claude 机器人发消息启动 Claude Code，给 Codex 机器人发消息启动 Codex CLI。

**认证过期自动恢复：** 当 Codex OAuth token 过期时，WalkCode 会自动检测并发起 device-auth 流程，在飞书发送验证链接和验证码，你在手机浏览器上完成验证即可，无需回到电脑前。

#### 6. （推荐）开机自启动

为 Codex 实例创建第二个 launchd plist（与 Claude 那份是同模板，只换 `Label`、`WALKCODE_ENV_FILE` 和日志路径）：

```bash
cat > ~/Library/LaunchAgents/com.walkcode-codex.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.walkcode-codex</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/walkcode</string>
        <string>serve</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>WALKCODE_ENV_FILE</key>
        <string>/Users/YOU/.walkcode/codex.env</string>
        <key>HOME</key>
        <string>/Users/YOU</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>/Users/YOU/.walkcode</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>ExitTimeOut</key>
    <integer>15</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>/Users/YOU/.walkcode/launchd.codex.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOU/.walkcode/launchd.codex.err.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.walkcode-codex.plist
```

> 将 `/Users/YOU` 替换为你的实际用户目录。同理推荐用 `serve` 而非 `start`（见 Claude 实例注释）。

### 多实例管理

```bash
# Claude 实例（默认）
walkcode start                                              # 启动
walkcode stop                                               # 停止
walkcode status                                             # 状态

# Codex 实例
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode start      # 启动
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode stop       # 停止
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode status     # 状态
```

### 文件布局

```
~/.walkcode/
  .env                # Claude 实例配置
  codex.env           # Codex 实例配置
  walkcode.pid/log    # Claude 实例运行状态
  codex.pid/log       # Codex 实例运行状态
  state.json          # Claude 会话状态
  codex-state.json    # Codex 会话状态
  images/             # 共享图片缓存
```

## 架构设计：1:1:1 映射

> 想深入了解内部设计——会话映射、生命周期、恢复流程、Hook 协议、状态持久化——请参阅 [ARCHITECTURE.md](ARCHITECTURE.md)（英文）。

WalkCode 的核心设计：**1 个聊天话题 = 1 个 tmux 会话 = 1 个 Coding Agent 进程。** 零串扰，上下文天然隔离，消息路由无状态。

```
飞书话题 A  <──1:1──>  tmux: claude-myapp-12345  <──1:1──>  Claude Code (myapp)
飞书话题 B  <──1:1──>  tmux: claude-api-67890    <──1:1──>  Claude Code (api)
飞书话题 C  <──1:1──>  tmux: walkcode-99999      <──1:1──>  Codex CLI (api)
```

### 安全设计：远程启动的权限控制

通过聊天远程启动 Coding Agent 时，WalkCode 使用受控的权限模式启动 Agent（Claude Code 使用 `--permission-mode default`，Codex CLI 使用 `--ask-for-approval untrusted`），并通过 Hook 实现飞书端的权限审批：

| 工具状态 | 行为 |
|---|---|
| 在允许列表中 | 自动通过，无需审批 |
| **不在**允许列表中 | **发送交互式卡片到聊天** —— 你点击 允许 / 拒绝 / 始终允许 |

如果 2 分钟内未响应，或 WalkCode 服务端不可达、Hook 本身异常，**Hook 会 fail-open**（不阻塞 Agent），退回到 Agent 自身的原生终端权限提示。这样「Hook 挂了 = 你回到没装 WalkCode 的状态」，不会把 Coding Agent 整个卡死。

## 使用方式

| 场景 | 你看到的 | 你要做的 |
|------|---------|---------|
| 权限确认 | 带工具详情的交互式卡片 | 点击 **允许** / **拒绝** / **始终允许** |
| 问题回答 | 带选项按钮的交互式卡片 | 点击选项按钮，多问题时卡片自动切换到下一题 |
| 发送图片 | 话题中回复图片 | 图片自动下载，以 `![图N](path)` 传递给 Coding Agent |
| 发送图文 | 话题中回复富文本 | 文字和图片位置保留，图片自动下载 |
| 等待输入 | 话题中的文字消息 | 回复文字 |
| 任务完成 | 话题中的文字消息 | 回复以继续，或忽略 |
| 会话过期 | 在旧话题中回复 | 自动恢复会话 |
| 远程启动 | 在聊天中发一条消息 | Coding Agent 在新 tmux 会话中启动 |

## 命令行

```bash
walkcode start                            # 后台启动
walkcode stop                             # 停止
walkcode restart                          # 重启
walkcode status                           # 查看运行状态
walkcode serve                            # 前台运行（调试用）
walkcode install-hooks                    # 安装 Claude Code Hooks
walkcode install-hooks --agent codex      # 安装 Codex CLI Hooks
walkcode upgrade                          # 拉取最新代码 + 重装 CLI + 重启
walkcode uninstall                        # 卸载 WalkCode
walkcode clean-images 1d                  # 清理 1 天前的图片（可选 1d/1w/1m/180d）
walkcode test-inject <tmux-session> "hi"  # 测试注入
```

## 配置项

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | 是 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 是 | 飞书应用密钥 |
| `FEISHU_RECEIVE_ID` | 否 | 你的 open_id 或 chat_id（运行 `walkcode serve` 获取） |
| `FEISHU_RECEIVE_ID_TYPE` | 否 | `open_id`（默认）或 `chat_id` |
| `LARK_OPENAPI_DOMAIN` | 否 | OpenAPI 域名。飞书默认 `https://open.feishu.cn`；Lark 国际版设为 `https://open.larksuite.com` |
| `PORT` / `WALKCODE_PORT` | 否 | HTTP 服务器端口（默认 `3001`） |
| `WALKCODE_CWD` | 否 | 远程启动会话的默认工作目录（默认 `~/.walkcode/workspace`） |
| `WALKCODE_AGENT` | 否 | Agent 类型：`claude`（默认）或 `codex` |
| `WALKCODE_INSTANCE` | 否 | 实例名称，用于隔离多实例的 PID/日志/状态文件 |
| `WALKCODE_ENV_FILE` | 否 | 指定 `.env` 文件路径（多实例时使用） |
| `WALKCODE_STATE_PATH` | 否 | 自定义状态文件路径 |

## 路线图

### 功能

| 功能 | 状态 |
|------|------|
| 权限审批、问题回答、文字回复 | 已支持 |
| 图片和富文本消息 | 已支持 |
| 远程启动和会话恢复 | 已支持 |
| 多 Agent（Claude Code + Codex CLI） | 已支持 |
| 合并转发消息 | 计划中 |

### 聊天平台

| 平台 | 状态 |
|------|------|
| [飞书 / Lark](https://www.feishu.cn/) | 已支持 |
| [Slack](https://slack.com/) | 计划中 |
| [Telegram](https://telegram.org/) | 计划中 |
| [Discord](https://discord.com/) | 计划中 |

## 社区

- [GitHub Issues](https://github.com/0x5446/walkcode/issues) — Bug 反馈 & 功能建议
- [GitHub Discussions](https://github.com/0x5446/walkcode/discussions) — 问答 & 讨论

### 飞书交流群

<img src="docs/images/feishu-group-qr.jpg" width="300" alt="飞书交流群">

## 参与贡献

欢迎提交 Issue 和 PR。

## 声明

本项目与 Anthropic 和 OpenAI 无关。Claude 是 Anthropic 的商标，Codex 是 OpenAI 的商标。

## 许可证

[MIT](LICENSE)
