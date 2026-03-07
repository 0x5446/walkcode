# WalkCode

[**English**](README.md)

> **Code is cheap. Show me your talk.**

绑死在电脑桌前那不叫 Vibe Coding。**随时随地**，才是 Vibe Coding。

WalkCode 把你的 IM 变成 AI 编程智能体的遥控器。智能体写代码，你散步。它需要帮忙时，手机响一下。你回一句话或者点一下按钮，它接着干。你接着走。

**口喷编程。边走边 Code。这就是 WalkCode。**

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> IM（话题 + 按钮）
                     <──tmux send-keys──  <──WS── （点击 / 回复）
```

## 为什么要用 WalkCode？

你在散步。你的 AI 智能体弹出一个权限确认，需要你说一声"允许"才能继续。

**没有 WalkCode：** 它卡住了。你 30 分钟后回到电脑前。节奏断了。

**有了 WalkCode：** 手机震了一下。你点了"允许"。智能体继续写代码。你继续散步。

这就是 **Yap Coding** —— 口喷编程。你说，它写。不需要键盘，不需要屏幕，不需要桌子。只需要你和你的手机。

工程师也有自己的移动互联网浪潮。

## 功能特性

- **锁屏也能用** —— `tmux send-keys` 注入，不依赖 GUI
- **话题消息** —— 每个智能体会话对应一个 IM 话题，上下文清晰有序
- **一键授权** —— 权限确认以交互卡片展示，支持 允许 / 拒绝 / 始终允许
- **文字回复** —— 在话题中回复文字，直接输入到对应终端
- **远程启动** —— 在 IM 中发条消息，就能远程启动一个新的编程智能体
- **表情回执** —— 随机表情回应确认送达
- **多会话** —— 多个智能体，一个实例，自动路由
- **会话持久化** —— 服务重启后自动恢复

## 快速开始

### 1. 创建飞书应用

1. 前往[飞书开放平台](https://open.feishu.cn/app)创建企业自建应用
2. **添加应用能力** > 机器人
3. **权限管理** > 开通以下权限：
   - `im:message` — 读取消息
   - `im:message:send_as_bot` — 以机器人身份发送消息
   - `im:message.reactions:write_only` — 添加表情回复
4. **事件与回调** > 长连接模式 > 添加事件 `im.message.receive_v1`
5. **版本管理与发布** > 创建版本 > 发布上线

### 2. 安装

```bash
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/agent-hotline.git
cd agent-hotline
uv sync
cp .env.example .env
```

编辑 `.env`，填入飞书应用的 App ID、App Secret 和 Verification Token。

### 3. 获取 open_id

```bash
uv run walkcode serve
```

在飞书中给机器人发消息，查看日志中的 `open_id`，填入 `.env` 的 `FEISHU_RECEIVE_ID`，重启服务。

### 4. 添加 Shell Wrapper

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

### 5. 安装 Hooks

```bash
uv run walkcode install-hooks
```

搞定。输入 `claude`，然后出门散步。

## 工作原理

1. Shell wrapper 将智能体启动在 tmux 会话中
2. 智能体 [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 在任务完成 / 需要权限 / 等待输入时触发
3. `walkcode hook` 检测当前 tmux 会话名并 POST 到本地服务器
4. WalkCode 在 IM 中创建**话题消息**（项目名作为标题，内容作为首条回复）
5. 你点击按钮或回复文字 —— 通过 WebSocket 实时送达
6. `tmux send-keys` 将你的回复注入到对应会话 —— 无需 GUI

### 远程启动

在 IM 中发送一条消息即可远程启动编程智能体：

1. WalkCode 创建新 tmux 会话，运行 `claude "<你的消息>"`
2. 在话题中回复确认已启动
3. 后续 hooks 自动关联到同一话题

## 使用方式

| 场景 | 你看到的 | 你要做的 |
|------|---------|---------|
| 权限确认 | 带按钮的交互卡片 | 点击 **允许** / **拒绝** / **始终允许** |
| 等待输入 | 话题中的文字消息 | 回复文字 |
| 任务完成 | 话题中的文字消息 | 回复以继续，或忽略 |
| 远程启动 | 发一条消息 | 智能体在新 tmux 会话中启动 |

## 命令行

```bash
walkcode start                            # 后台启动
walkcode start --log /tmp/walkcode.log    # 自定义日志路径
walkcode stop                             # 停止
walkcode restart                          # 重启
walkcode status                           # 查看运行状态
walkcode serve                            # 前台运行（调试用）
walkcode install-hooks                    # 安装 Hooks
walkcode test-inject <tmux-session> "hi"  # 测试注入
```

## 配置项

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | 是 | 飞书应用 ID |
| `FEISHU_APP_SECRET` | 是 | 飞书应用密钥 |
| `FEISHU_RECEIVE_ID` | 是 | 你的 open_id 或 chat_id |
| `FEISHU_VERIFICATION_TOKEN` | 是 | 飞书验证令牌 |
| `FEISHU_RECEIVE_ID_TYPE` | 否 | `open_id`（默认）或 `chat_id` |
| `WALKCODE_STATE_PATH` | 否 | 自定义状态文件路径 |
| `WALKCODE_CWD` | 否 | 远程启动会话的默认工作目录 |

## 路线图

WalkCode 的目标：**连接任意编程智能体到任意 IM。**

### 编程智能体

| 智能体 | 状态 |
|--------|------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | 已支持 |
| [Codex CLI](https://github.com/openai/codex) | 计划中 |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | 计划中 |
| [Cline](https://github.com/cline/cline) | 计划中 |
| [Aider](https://github.com/Aider-AI/aider) | 计划中 |
| [Copilot CLI](https://githubnext.com/projects/copilot-cli) | 计划中 |
| [Goose](https://github.com/block/goose) | 计划中 |
| [Amp](https://ampcode.com) | 计划中 |

### IM 平台

| 平台 | 状态 |
|------|------|
| [飞书 / Lark](https://www.feishu.cn/) | 已支持 |
| [Slack](https://slack.com/) | 计划中 |
| [Telegram](https://telegram.org/) | 计划中 |
| [Discord](https://discord.com/) | 计划中 |
| [WhatsApp](https://www.whatsapp.com/) | 计划中 |

## 社区

- [GitHub Issues](https://github.com/0x5446/agent-hotline/issues) — Bug 反馈 & 功能建议
- [GitHub Discussions](https://github.com/0x5446/agent-hotline/discussions) — 问答 & 讨论

<!-- TODO: 添加微信群/公众号二维码 -->
<!-- <img src="docs/wechat-qr.png" width="200" alt="微信交流群"> -->

## 系统要求

- macOS
- [tmux](https://github.com/tmux/tmux)（`brew install tmux`）
- [uv](https://docs.astral.sh/uv/)（Python >= 3.13）
- 飞书企业自建应用（免费）

## 参与贡献

欢迎提交 Issue 和 PR。提交前请运行 `uv run pytest` 确保测试通过。

## 声明

本项目与 Anthropic 无关。Claude 是 Anthropic 的商标。

## 许可证

[MIT](LICENSE)
