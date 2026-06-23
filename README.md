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

## ✨ 能做什么

- 🔔 **手机审批** —— Agent 卡在权限确认时，手机震一下，点「允许」它就接着干
- 💬 **手机指挥** —— 在话题里回复文字、图片、富文本，直接输入到对应终端
- 🩺 **会话健康卡片** —— 每个会话在话题顶部维护一张实时状态卡：进行中 / 等待确认 / 已完成，附模型、时长、消息数、Token 用量，关键事件实时刷新
- 🧵 **1 话题 = 1 会话 = 1 Agent** —— 零串扰，在哪个话题回复就送达哪个 Agent
- 🚀 **远程启动 + 自动恢复** —— 发条消息就在新 tmux 里拉起一个 Agent；会话过期了回复任意消息自动恢复
- 🤖 **多 Agent 并行** —— Claude Code 与 Codex CLI 同时跑，各自独立飞书机器人
- 🔌 **灵活路由** —— 每个实例可独立指定启动参数与权限模式（如 Claude 走 Vertex、Codex 全自动 `--yolo`）

## 支持的 Coding Agent

| Coding Agent | 状态 | 说明 |
|--------|------|------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | 已支持 | 默认 Agent |
| [Codex CLI](https://github.com/openai/codex) | 已支持 | 需要独立飞书机器人 |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) / [Cline](https://github.com/cline/cline) / [Aider](https://github.com/Aider-AI/aider) 等 | 计划中 | 通过 Agent Adapter 扩展 |

> **每个 Agent 对应一个独立的飞书机器人。** 你给 Claude Code 机器人发消息就启动 Claude，给 Codex 机器人发消息就启动 Codex，体验清晰直觉。

## 为什么要用 WalkCode？

你离开了电脑。你的 Coding Agent 弹出一个权限确认，卡住了。没有 WalkCode，它等你回来。有了 WalkCode，手机震一下，你点「允许」，它接着干。

- **别让 Coding Agent 等你** —— 随时随地审批和回复
- **每个会话独立话题** —— 在话题中回复，必定送达正确的 Coding Agent
- **一眼看清状态** —— 话题顶部的健康卡片随时告诉你它在跑、在等你、还是已经做完
- **锁屏也能用** —— 基于 tmux，不依赖 GUI

工程师也有自己的移动互联网浪潮。

## 功能特性

**核心：**
- **权限审批** —— 不在允许列表的工具，发交互式卡片到聊天，你点「允许 / 拒绝 / 始终允许」。终端里手动处理后，飞书卡片会同步失效，避免重复操作
- **问题回答** —— AskUserQuestion 交互式卡片，每个选项带说明文字，支持多问题顺序处理、多选（multiSelect）、自定义文本（Other）
- **文字回复** —— 在话题中回复文字，直接输入到对应终端；Agent 完成一轮后，整轮的完整回复（含多段输出）都会转发给你，不只最后一段
- **图文消息** —— 支持发送图片和富文本（图文混排），图片自动下载并传递给 Coding Agent
- **会话健康卡片** —— 每个会话的话题顶部维护一张实时状态卡，含状态、模型、时长、消息数、Token 用量（按模型分组），远程回复、权限审批、Stop 结果等关键事件会立即刷新
- **远程启动** —— 在聊天中发条消息，就能远程启动一个新的 Coding Agent
- **会话恢复** —— 话题中的 tmux 会话过期后，回复任意消息自动恢复
- **忙时可发** —— Agent 还在忙时你也能发消息，立即注入（是否排队交给终端/Agent 处理），表情确认送达
- **自动回收** —— 闲置超过 2 小时的 tmux 会话自动关闭并通知
- **多 Agent** —— 同时运行 Claude Code 和 Codex CLI，各自独立飞书机器人

**其他：**
- **多会话** —— 多个 Coding Agent 会话，一个实例，自动路由
- **会话持久化** —— 服务重启后自动恢复
- **认证自动恢复** —— Codex OAuth token 过期时自动发起 device-auth，手机上完成验证即可
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
   - `im:message:send_as_bot` — 以机器人身份发送消息（同时覆盖消息更新，健康卡片需要）
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
uv tool install "walkcode[summary] @ git+https://github.com/0x5446/walkcode.git"
```

> `summary` extra 只提供 Codex 健康卡片标题精炼依赖。未配置 `WALKCODE_SUMMARY_*` 时不会调用模型。

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

# 远程启动时完全自动执行（跳过审批 + 沙箱），适合 Codex 直发场景
WALKCODE_PERMISSION_FLAG=--yolo

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

#### 7. （推荐）开机自启动

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

### 灵活路由：每实例独立的启动参数

每个实例可以通过 `.env` 单独指定 Agent 的启动参数和权限模式，互不影响：

| 变量 | 作用 | 典型用法 |
|------|------|---------|
| `WALKCODE_PERMISSION_FLAG` | 替换默认的权限/审批 flag | Codex 设 `--yolo` 全自动；不设则用默认（Claude `--permission-mode default`、Codex `--ask-for-approval untrusted`） |
| `WALKCODE_EXTRA_ARGS` | 在 Agent 命令后插入额外参数 | Claude 走 Vertex：`--settings /Users/you/.walkcode/vertex.json`（绝对路径） |

> 这些值会经过严格的 shell 转义后拼进启动命令，只能被解析成 Agent 参数，不会被当成 shell 语法执行。start 和 resume 都会应用，恢复会话时路由保持一致。
>
> **注意**：转义会保留字面量，路径里的 `~` / `$HOME` 不会展开，必须用绝对路径（如 `/Users/you/.walkcode/vertex.json`）。让 Claude 走 Vertex 相当于把代码上下文交给你自己的云项目处理——只在受信任项目里启用；`--settings` 指向的配置和凭证（含 service account JSON）放在仓库外、不要提交进版本库。

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

### 会话健康卡片

每个会话的话题顶部维护一张交互卡片。卡片在远程回复、权限审批、Stop 结果等事件发生时立即刷新，后台 poller 只做兜底同步，让你一眼看清这个会话的状态：

| 字段 | 内容 |
|------|------|
| 状态 | 🟢 进行中 / 🟠 等待确认 / ✅ 已完成 / 🔴 出错（卡片颜色随状态变化） |
| 模型 | 当前会话使用的模型 |
| 时长 | 会话已运行时间 |
| 输入 | 你发出的消息条数 |
| Token | 累计 Token 用量（按模型分组） |

话题标题会自动用任务摘要命名（Claude 用自带 AI 标题；Codex 可选用 Haiku 在 Stop 后异步精炼，见配置项）。每个 Stop 后卡片会刷新并冻结；如果你继续在话题里回复，卡片会重新进入进行中状态。整个功能默认开启，`WALKCODE_HEALTH_CARD=0` 可关闭。

### 安全设计：远程启动的权限控制

通过聊天远程启动 Coding Agent 时，WalkCode 使用受控的权限模式启动 Agent（Claude Code 默认 `--permission-mode default`；Codex CLI 默认 `--ask-for-approval untrusted`，并固定附带 `--dangerously-bypass-hook-trust`（让 WalkCode 自己的 hook 不被逐次信任拦截）和 `--no-alt-screen`），并通过 Hook 实现飞书端的权限审批：

| 工具状态 | 行为 |
|---|---|
| 在允许列表中 | 自动通过，无需审批 |
| **不在**允许列表中 | **发送交互式卡片到聊天** —— 你点击 允许 / 拒绝 / 始终允许 |

> 如果你在实例 `.env` 里把 `WALKCODE_PERMISSION_FLAG` 设成 `--yolo`（Codex）这类完全自动的模式，Agent 会跳过审批直接执行，不再发审批卡片——请按自己的信任边界选择。

如果 30 分钟内未响应，或 WalkCode 服务端不可达、Hook 本身异常，**Hook 会 fail-open**（不阻塞 Agent），退回到 Agent 自身的原生终端权限提示。这样「Hook 挂了 = 你回到没装 WalkCode 的状态」，不会把 Coding Agent 整个卡死。

## 使用方式

| 场景 | 你看到的 | 你要做的 |
|------|---------|---------|
| 权限确认 | 带工具详情的交互式卡片 | 点击 **允许** / **拒绝** / **始终允许** |
| 问题回答 | 带选项按钮和说明的交互式卡片 | 点击选项按钮，多问题时卡片自动切换到下一题 |
| 查看进度 | 话题顶部的健康卡片 | 瞄一眼状态 / 时长 / Token，无需操作 |
| 发送图片 | 话题中回复图片 | 图片自动下载，以 `![图N](path)` 传递给 Coding Agent |
| 发送图文 | 话题中回复富文本 | 文字和图片位置保留，图片自动下载 |
| 等待输入 | 话题中的文字消息 | 回复文字 |
| 任务完成 | 话题中的整轮回复 | 回复以继续，或忽略 |
| 中途追加 | Agent 忙时发消息 | 立即注入，表情确认送达 |
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
walkcode upgrade                          # 拉取最新发布 + 重装 CLI + 重启
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
| `LARK_OPENAPI_DOMAIN` | 否 | OpenAPI 域名。飞书默认 `https://open.feishu.cn`；Lark 国际版设为 `https://open.larksuite.com`（也可用 `FEISHU_OPENAPI_DOMAIN`） |
| `PORT` / `WALKCODE_PORT` | 否 | HTTP 服务器端口（默认 `3001`） |
| `WALKCODE_CWD` | 否 | 远程启动会话的默认工作目录（默认 `~/.walkcode/workspace`） |
| `WALKCODE_AGENT` | 否 | Agent 类型：`claude`（默认）或 `codex` |
| `WALKCODE_INSTANCE` | 否 | 实例名称，用于隔离多实例的 PID/日志/状态文件 |
| `WALKCODE_ENV_FILE` | 否 | 指定 `.env` 文件路径（多实例时使用） |
| `WALKCODE_STATE_PATH` | 否 | 自定义状态文件路径（默认：Claude 主实例 `~/.walkcode/state.json`，其他实例 `~/.walkcode/<实例名>-state.json`） |
| `WALKCODE_PERMISSION_FLAG` | 否 | 替换 Agent 默认权限/审批 flag，如 Codex 设 `--yolo` |
| `WALKCODE_EXTRA_ARGS` | 否 | 在 Agent 命令后插入额外启动参数，如 Claude 走 Vertex 的 `--settings` |
| `WALKCODE_HEALTH_CARD` | 否 | 会话健康卡片开关，设 `0` 关闭（默认开启） |
| `WALKCODE_SUMMARY_VERTEX_PROJECT` | 否 | 健康卡片标题精炼用的 Vertex 项目（仅 Codex 会话；不设则用首行作标题） |
| `WALKCODE_SUMMARY_VERTEX_REGION` | 否 | Vertex 区域（默认 `global`） |
| `WALKCODE_SUMMARY_SA_PATH` | 否 | Vertex service account JSON 路径 |
| `WALKCODE_SUMMARY_MODEL` | 否 | 标题精炼模型（默认 `claude-haiku-4-5`） |
| `WALKCODE_SUMMARY_TIMEOUT` | 否 | 标题精炼超时秒数（默认 `8`） |

> 标题精炼（`WALKCODE_SUMMARY_*`）只用于 **Codex 会话**（Claude 用自带 AI 标题，不走这条路径），依赖 `summary` extra（`anthropic[vertex]`）。官方安装脚本和 `walkcode upgrade` 会默认安装该 extra；未配置时 Codex 自动降级为用任务首行作标题，不会调用模型。service account JSON 等凭证放仓库外、用绝对路径、不要提交进版本库。

## 路线图

### 功能

| 功能 | 状态 |
|------|------|
| 权限审批、问题回答、文字回复 | 已支持 |
| 图片和富文本消息 | 已支持 |
| 会话健康卡片 | 已支持 |
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
