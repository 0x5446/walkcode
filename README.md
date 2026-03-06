# CBuddy

> Drive your terminal Claude Code from Feishu.

Claude Code 在终端跑着，你离开了电脑——任务完了你不知道，等你确认权限它在空转。

CBuddy 把这些事件以**卡片消息**推到飞书。权限确认直接点按钮，文本回复打进终端，还能确认送达状态。

```
Claude Code ──Hook──> CBuddy ──API──> Feishu (卡片通知 + 权限按钮)
             <──注入──         <──WS── (按钮点击 / 文本回复)
```

## Quick Start

### 1. 创建飞书应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用
2. **添加应用能力** → 机器人
3. **权限管理** → 开通 `im:message`、`im:message:send_as_bot`
4. **事件与回调** → 订阅方式选「长连接」→ 添加事件 `im.message.receive_v1` → 确认开通推荐权限
5. **版本管理** → 创建版本 → 发布

### 2. 安装

```bash
# 安装 uv（如已安装可跳过）
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/cbuddy.git
cd cbuddy
uv sync
cp .env.example .env
```

编辑 `.env`，填入飞书 App ID / Secret / Verification Token。默认端口 3001，可通过 `PORT` 环境变量自定义。

### 3. 获取你的 open_id

启动服务后，在飞书里给你的机器人发一条消息（搜索机器人名字打开对话），日志会打印你的 `open_id`：

```bash
uv run cbuddy serve
# 日志输出: Non-reply message from ou_xxxx (use this open_id for FEISHU_RECEIVE_ID)
```

把 `ou_xxxx` 填入 `.env` 的 `FEISHU_RECEIVE_ID`，重启服务。

### 4. 安装 Claude Code Hooks & macOS 权限

```bash
uv run cbuddy install-hooks
```

重启 Claude Code 会话生效。

> **macOS 辅助功能权限**：系统设置 → 隐私与安全性 → 辅助功能 → 添加 Terminal.app。没有此权限，终端注入会报错。

---

完成。正常使用 `claude` 即可，不需要 tmux、wrapper 或任何特殊操作。

CBuddy 会把 session/thread 映射持久化到 `~/.cbuddy/state.json`，服务重启后仍能识别之前的话题回复。
回复消息时还会根据最近一次 hook 上报的进程指纹重新确认当前 TTY，避免把输入打到过期终端。
如需自定义路径，可设置 `CBUDDY_STATE_PATH`。

## 使用

飞书收到卡片消息后：

- **🔐 权限确认**：直接点卡片按钮（允许 / 拒绝 / 始终允许），一键操作
- **⏳ 等待输入 / ✅ 任务完成**：回复话题消息，文本会注入终端

| 飞书卡片 | 操作 | 效果 |
|---------|------|------|
| 🔐 需要权限确认（红色卡片） | 点击按钮 | 一键注入 y/n/a |
| ⏳ 等待输入（蓝色卡片） | 回复文本 | 打入终端 |
| ✅ 任务完成（绿色卡片） | 回复新指令 | 继续工作 |

### 送达确认

文本回复注入后，CBuddy 会自动验证终端内容是否变化：

- 📨 正在发送到 ... → ✅ 已送达（确认成功）
- 📨 正在发送到 ... → ⚠️ 已发送但未确认送达（需关注）
- 📨 正在发送到 ... → ❌ 注入失败（终端不可用等）

按钮点击后，卡片会原地更新为结果状态，同时弹出 Toast 提示。

## 多开 Claude Code

CBuddy 天然支持同时运行多个 Claude Code 会话。

每个 Claude Code 会话有唯一的 `session_id`。同一个 session 的所有通知会归入同一个飞书话题（thread），你回复话题里的任意消息都会注入到对应终端，互不干扰：

```
Terminal Tab 1 (session abc)  <-->  飞书话题 A
Terminal Tab 2 (session def)  <-->  飞书话题 B
Terminal Tab 3 (session ghi)  <-->  飞书话题 C
```

只需要运行一个 `cbuddy serve`，所有 Claude Code 会话共享同一个 CBuddy 服务。

通知标题会带上 `tty` 和 `session` 短标识，例如 `[plaudclaw ttys001 9079ba57]`，用于区分同一项目下的多个并发会话。

## How It Works

1. Claude Code [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 在任务完成/需要输入时触发，通过 `cbuddy hook` 命令将事件（含 session_id、消息内容）POST 到本地 CBuddy 服务
2. CBuddy 通过飞书 API 发送**卡片消息**（权限确认带按钮），同一 session 的消息自动归入同一话题线程
3. 用户在飞书点击按钮或回复文本，通过 WebSocket 长连接实时推送到 CBuddy
4. CBuddy 通过 AppleScript 剪贴板粘贴（Cmd+V）把内容注入对应 Terminal.app tab，并验证送达

## CLI

```bash
uv run cbuddy start                              # 后台启动（日志 ~/.cbuddy/cbuddy.log）
uv run cbuddy start --log /tmp/cbuddy.log        # 自定义日志路径
uv run cbuddy stop                                # 停止服务
uv run cbuddy restart                             # 重启服务
uv run cbuddy status                              # 查看运行状态
uv run cbuddy serve                               # 前台运行（调试用）
uv run cbuddy install-hooks                       # 安装 Claude Code Hooks
uv run cbuddy test-inject /dev/ttys003 "hello"    # 测试注入
uv run cbuddy test-inject /dev/ttys003 "y" --no-enter
```

运行时文件统一存放在 `~/.cbuddy/`：

| 文件 | 用途 |
|------|------|
| `~/.cbuddy/cbuddy.pid` | 后台进程 PID |
| `~/.cbuddy/cbuddy.log` | 服务日志（默认） |
| `~/.cbuddy/state.json` | Session 持久化 |

## Requirements

- macOS (AppleScript + Terminal.app)
- [uv](https://docs.astral.sh/uv/) (Python 3.13 由 uv 自动管理)
- 飞书企业自建应用 (免费)

## [License](LICENSE)

MIT
