# WalkCode

[**English**](README.md)

> **Code is cheap. Show me your talk.**

**Coding Agent 写代码，你散步。**

WalkCode 让 Coding Agent 在需要帮助时给你发消息，你用手机就能审批、回复、纠正方向。不用守在电脑前，也能掌控全局。

**边走边 Code，口喷编程。这就是 WalkCode。**

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> 聊天（话题）
                     <──tmux send-keys──  <──WS── （回复）
```

## 为什么要用 WalkCode？

你离开了电脑。你的 Coding Agent 弹出一个权限确认，卡住了。没有 WalkCode，它等你回来。有了 WalkCode，手机震一下，你点"允许"，它接着干。

- **别让 Coding Agent 等你** —— 随时随地审批和回复
- **每个会话独立话题** —— 在话题中回复，必定送达正确的 Coding Agent
- **锁屏也能用** —— 基于 tmux，不依赖 GUI

工程师也有自己的移动互联网浪潮。

## 功能特性

**核心：**
- **权限审批** —— 直接在聊天中回复审批
- **文字回复** —— 在话题中回复文字，直接输入到对应终端
- **远程启动** —— 在聊天中发条消息，就能远程启动一个新的 Coding Agent
- **会话恢复** —— 话题中的 tmux 会话过期后，回复任意消息自动恢复
- **自动回收** —— 闲置超过 2 小时的 tmux 会话自动关闭并通知

**其他：**
- **多会话** —— 多个 Coding Agent，一个实例，自动路由
- **会话持久化** —— 服务重启后自动恢复
- **表情回执** —— 随机表情回应确认送达
- **i18n** —— 自动检测系统语言（zh* 中文，其余英文）

## 架构设计：1:1:1 映射

> 想深入了解内部设计——会话映射、生命周期、恢复流程、Hook 协议、状态持久化——请参阅 [ARCHITECTURE.md](ARCHITECTURE.md)（英文）。

WalkCode 的核心设计：**1 个聊天话题 = 1 个 tmux 会话 = 1 个 Coding Agent 进程。** 零串扰，上下文天然隔离，消息路由无状态。

```
飞书话题 A  <──1:1──>  tmux: claude-myapp-12345  <──1:1──>  Claude Code (myapp)
飞书话题 B  <──1:1──>  tmux: claude-api-67890    <──1:1──>  Claude Code (api)
```

### 远程启动的工作原理

你可以直接从聊天启动 Coding Agent —— 不需要打开终端：

1. 你在聊天中发送一条消息（如"修复 myapp 的登录 bug"）
2. WalkCode 创建 tmux 会话：`claude "修复 myapp 的登录 bug"`
3. WalkCode 在话题中回复确认已启动
4. WalkCode 记住关联关系：`tmux 会话名 → 聊天消息 ID`（存储在 `_pending_roots` 中）
5. 当 Coding Agent 的 hooks 首次触发时，WalkCode 匹配 tmux 名称，将此会话关联到该话题
6. 此后，该 Coding Agent 的所有事件都回复到同一话题 —— 1:1:1 关联建立完成

### 安全设计：远程启动的权限控制

通过聊天远程启动 Coding Agent 时，WalkCode 使用 `--permission-mode default` 启动 Claude Code，并通过 **PermissionRequest Hook** 实现飞书端的权限审批：

| 工具状态 | 行为 |
|---|---|
| 在 `permissions.allow` 中 | 自动通过，无需审批 |
| **不在** `permissions.allow` 中 | **发送交互式卡片到聊天** —— 你点击 允许 / 拒绝 / 始终允许 |

点击**始终允许**时，该工具会被自动添加到 `~/.claude/settings.json` 的 `permissions.allow` 列表，后续调用直接通过。如果 2 分钟内未响应，请求自动拒绝。

这比 `dangerouslySkipPermissions`（全部自动通过）更安全，比 `dontAsk`（静默拒绝未知工具）更好用。对于新工具你始终在回路中做决策，对于已审批的工具则完全自动化。

## 快速开始

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

自动拉取最新代码、重装 CLI 并重启守护进程。无需重新加载 Shell。

### 一键卸载

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/uninstall.sh | bash
```

清理守护进程、Shell Wrapper、tmux 配置、Claude Code Hooks 和 `~/.walkcode` 目录。如果自定义了安装路径，加 `WALKCODE_DIR=/your/path` 前缀即可。运行前可先[查看脚本内容](uninstall.sh)。

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

#### 4. （推荐）防止 macOS 系统休眠

WalkCode 依赖持续的网络连接来接收飞书消息。如果 Mac 在接外部电源时进入系统休眠，网络会被挂起——休眠期间发送的消息直到唤醒后才会被收到，期间无法远程操控电脑。

接外电时，建议禁止系统休眠和磁盘休眠（屏幕仍可正常关闭）：

```bash
sudo pmset -c sleep 0 && sudo pmset -c disksleep 0 && sudo pmset -c standby 0 && sudo pmset -c hibernatemode 0
```

此设置仅影响接外电时的行为，电池模式不受影响。

### 手动安装

<details>
<summary>逐步说明</summary>

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

#### 2. 安装

```bash
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/walkcode.git ~/.walkcode
cd ~/.walkcode
uv sync
cp .env.example .env
```

编辑 `.env`，填入飞书应用的 App ID 和 App Secret。

#### 3. 获取 open_id

```bash
uv run walkcode serve
```

在飞书中给机器人发消息，发送者的 `open_id` 会直接打印在控制台。填入 `.env` 的 `FEISHU_RECEIVE_ID`，重启服务。

#### 4. 添加 Shell Wrapper

添加到 `~/.zshrc`（或 `~/.bashrc`）：

```bash
claude() {
  if [ -z "$TMUX" ]; then
    local session="claude-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command claude $@"
  else
    command claude "$@"
  fi
}
```

然后：`source ~/.zshrc`

#### 5. 配置 tmux 滚动历史

添加到 `~/.tmux.conf`：

```bash
# 禁用备用屏幕切换，使 TUI 输出（如 Claude Code）保留在滚动历史中
set-option -ga terminal-overrides ',*:smcup@:rmcup@'
```

然后：`tmux source-file ~/.tmux.conf`（如果 tmux 正在运行）

#### 6. 安装 Hooks

```bash
uv run walkcode install-hooks
```

</details>

## 工作原理

1. Shell wrapper 将 Coding Agent 启动在 tmux 会话中
2. Coding Agent [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 在任务完成 / 需要权限 / 等待输入时触发
3. `walkcode hook` 检测当前 tmux 会话名并 POST 到本地服务器
4. WalkCode 在聊天中创建**话题消息**（`项目名 | session_id | prompt` 作为标题，内容作为首条回复）
5. 权限请求通过**交互式卡片**发送，附带 允许 / 拒绝 / 始终允许 按钮 —— 你的决策直接返回给 Hook 进程（无需终端注入）
6. 文字回复通过 `tmux send-keys` 注入到对应会话 —— 无需 GUI

## 使用方式

| 场景 | 你看到的 | 你要做的 |
|------|---------|---------|
| 权限确认 | 带工具详情的交互式卡片 | 点击 **允许** / **拒绝** / **始终允许** |
| 等待输入 | 话题中的文字消息 | 回复文字 |
| 任务完成 | 话题中的文字消息 | 回复以继续，或忽略 |
| 会话过期 | 在旧话题中回复 | 自动通过 `--resume` 恢复会话 |
| 远程启动 | 在聊天中发一条消息 | Coding Agent 在新 tmux 会话中启动 |

## 命令行

```bash
walkcode start                            # 后台启动
walkcode start --log /tmp/walkcode.log    # 自定义日志路径
walkcode stop                             # 停止
walkcode restart                          # 重启
walkcode status                           # 查看运行状态
walkcode serve                            # 前台运行（调试用）
walkcode install-hooks                    # 安装 Hooks
walkcode upgrade                          # 拉取最新代码 + 重装 CLI + 重启
walkcode uninstall                        # 卸载 WalkCode
walkcode test-inject <tmux-session> "hi"  # 测试注入
```

## 配置项

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | 是 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 是 | 飞书应用密钥 |
| `FEISHU_RECEIVE_ID` | 否 | 你的 open_id 或 chat_id（运行 `walkcode serve` 获取） |
| `FEISHU_RECEIVE_ID_TYPE` | 否 | `open_id`（默认）或 `chat_id` |
| `WALKCODE_STATE_PATH` | 否 | 自定义状态文件路径 |
| `WALKCODE_CWD` | 否 | 远程启动会话的默认工作目录（默认 `~/.walkcode/workspace`） |

## 路线图

WalkCode 的目标：**连接任意 Coding Agent 到任意聊天平台。**

### Coding Agent

| Coding Agent | 状态 |
|--------|------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | 已支持 |
| [Codex CLI](https://github.com/openai/codex) | 计划中 |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | 计划中 |
| [Cline](https://github.com/cline/cline) | 计划中 |
| [Aider](https://github.com/Aider-AI/aider) | 计划中 |
| [Copilot CLI](https://githubnext.com/projects/copilot-cli) | 计划中 |
| [Goose](https://github.com/block/goose) | 计划中 |
| [Amp](https://ampcode.com) | 计划中 |

### 功能

| 功能 | 状态 |
|------|------|
| 多模态消息（图片、富文本、合并转发） | 计划中 |
| Slash 命令回复与选项选择 | 计划中 |

### 聊天平台

| 平台 | 状态 |
|------|------|
| [飞书 / Lark](https://www.feishu.cn/) | 已支持 |
| [Slack](https://slack.com/) | 计划中 |
| [Telegram](https://telegram.org/) | 计划中 |
| [Discord](https://discord.com/) | 计划中 |
| [WhatsApp](https://www.whatsapp.com/) | 计划中 |

## 社区

- [GitHub Issues](https://github.com/0x5446/walkcode/issues) — Bug 反馈 & 功能建议
- [GitHub Discussions](https://github.com/0x5446/walkcode/discussions) — 问答 & 讨论

<!-- TODO: 添加微信群/公众号二维码 -->
<!-- <img src="docs/wechat-qr.png" width="200" alt="微信交流群"> -->

## 参与贡献

欢迎提交 Issue 和 PR。提交前请运行 `uv run pytest` 确保测试通过。

## 声明

本项目与 Anthropic 无关。Claude 是 Anthropic 的商标。

## 许可证

[MIT](LICENSE)
