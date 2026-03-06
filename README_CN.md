# Agent Hotline

[**English**](README.md)

**让你的 AI 智能体在需要帮助时给你打电话。**

> 基于[飞书](https://www.feishu.cn/)的 [Claude Code](https://docs.anthropic.com/en/docs/claude-code) 人机协作方案 —— 随时随地 vibe coding。

Claude Code 执行任务时，有时需要确认权限、补充上下文或等待你的输入，通常只能阻塞等待。Agent Hotline 解决了这个问题：它通过飞书给你发消息，你用手机回复，Claude 就能继续工作 —— 即使你锁屏了也没关系。

```
Claude Code (tmux) ──Hook──> Agent Hotline ──API──> 飞书（话题 + 按钮）
                    <──tmux send-keys──      <──WS── （点击 / 回复）
```

## 功能特性

- **锁屏也能用** —— 使用 `tmux send-keys` 注入，不依赖 GUI，锁屏、离开电脑都不影响
- **话题消息** —— 每个 Claude Code 会话对应一个飞书话题，上下文清晰有序
- **一键授权** —— 权限确认以交互卡片展示，支持 允许 / 拒绝 / 始终允许 按钮
- **文字回复** —— 在话题中回复文字，直接输入到对应的终端
- **表情回执** —— 随机表情回应确认送达，不产生额外消息噪音
- **多会话** —— 同时运行多个 Claude Code 实例，回复自动路由到正确的终端
- **会话持久化** —— 服务重启后会话恢复，飞书话题继续使用
- **完全透明** —— Shell wrapper 自动创建 tmux 会话，你只需照常输入 `claude`

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
# 前置依赖
brew install tmux

# 安装 uv（已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/agent-hotline.git
cd agent-hotline
uv sync
cp .env.example .env
```

编辑 `.env`，填入飞书应用的 App ID、App Secret 和 Verification Token。

### 3. 获取 open_id

启动服务后，在飞书中给你的机器人发送任意消息：

```bash
uv run agent-hotline serve
# 日志输出：Non-reply message from ou_xxxx (use this open_id for FEISHU_RECEIVE_ID)
```

将 `ou_xxxx` 填入 `.env` 的 `FEISHU_RECEIVE_ID`，然后重启服务。

### 4. 添加 Shell Wrapper

将以下内容添加到 `~/.zshrc`（或 `~/.bashrc`）：

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

然后执行：`source ~/.zshrc`

这个 wrapper 会透明地将 Claude Code 运行在 tmux 会话中。你还是照常输入 `claude`，其余的 wrapper 自动处理。

### 5. 安装 Claude Code Hooks

```bash
uv run agent-hotline install-hooks
```

重启 Claude Code 会话以激活。

搞定。输入 `claude`，就能收到飞书通知 —— 即使你锁屏了也能正常工作。

## 工作原理

1. Shell wrapper 将 Claude Code 启动在 tmux 会话中
2. Claude Code [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 在任务完成 / 需要权限 / 等待输入时触发
3. `agent-hotline hook` 检测当前 tmux 会话名并 POST 到本地服务器
4. Agent Hotline 在飞书中创建**话题消息**（项目名作为标题，内容作为首条回复）
5. 你点击按钮或回复文字 —— 通过飞书 WebSocket 实时送达
6. `tmux send-keys` 将你的回复注入到对应会话 —— 无需 GUI

## 使用方式

| 场景 | 你看到的 | 你要做的 |
|------|---------|---------|
| 权限确认 | 带按钮的交互卡片 | 点击 **允许** / **拒绝** / **始终允许** |
| 等待输入 | 话题中的文字消息 | 回复文字 |
| 任务完成 | 话题中的文字消息 | 回复以继续，或忽略 |

送达状态以表情回应的形式展示在你的消息上。

## 多会话

每个 Claude Code 会话在飞书中自动创建独立话题。在话题中回复任意消息，即可注入到对应终端：

```
tmux: claude-project-a-12345  <-->  飞书话题「project-a | 重构代码...」
tmux: claude-project-b-67890  <-->  飞书话题「project-b | 新增功能...」
```

一个 `agent-hotline` 实例即可管理所有会话。

## 命令行

```bash
agent-hotline start                            # 后台启动
agent-hotline start --log /tmp/hotline.log     # 自定义日志路径
agent-hotline stop                             # 停止
agent-hotline restart                          # 重启
agent-hotline status                           # 查看运行状态
agent-hotline serve                            # 前台运行（调试用）
agent-hotline install-hooks                    # 安装 Claude Code hooks
agent-hotline test-inject <tmux-session> "hi"  # 测试 tmux 注入
```

运行时文件位于 `~/.agent-hotline/`：

| 文件 | 用途 |
|------|------|
| `agent-hotline.pid` | 守护进程 PID |
| `agent-hotline.log` | 服务日志 |
| `state.json` | 会话持久化 |

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
