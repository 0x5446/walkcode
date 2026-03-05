# CBuddy

> Drive your terminal Claude Code from Feishu.

Claude Code 在终端跑着，你离开了电脑——任务完了你不知道，等你确认权限它在空转。

CBuddy 把这些事件推到飞书。你在手机上回复，内容直接打进电脑终端。

```
Claude Code ──Hook──> CBuddy ──API──> Feishu (手机收通知)
             <──注入──         <──WS── (手机回复消息)
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
git clone https://github.com/0x5446/cbuddy.git
cd cbuddy
pip install -e .
cp .env.example .env
```

编辑 `.env`，填入飞书 App ID / Secret / Verification Token。

### 3. 获取你的 open_id

启动服务后，在飞书里给你的机器人发一条消息（搜索机器人名字打开对话），日志会打印你的 `open_id`：

```bash
cbuddy serve
# 日志输出: Non-reply message from ou_xxxx (use this open_id for FEISHU_RECEIVE_ID)
```

把 `ou_xxxx` 填入 `.env` 的 `FEISHU_RECEIVE_ID`，重启服务。

### 4. 安装 Claude Code Hooks & macOS 权限

```bash
cbuddy install-hooks
```

重启 Claude Code 会话生效。

> **macOS 辅助功能权限**：系统设置 → 隐私与安全性 → 辅助功能 → 添加 Terminal.app。没有此权限，终端注入会报错。

---

完成。正常使用 `claude` 即可，不需要 tmux、wrapper 或任何特殊操作。

## 使用

飞书收到消息后，**回复那条消息**（长按 → 回复）：

| 飞书消息 | 回复 | 效果 |
|---------|------|------|
| 🔐 需要权限确认 | `y` / `n` / `a` | 允许 / 拒绝 / 始终允许 |
| ⏳ 等待输入 | 任意文本 | 打入终端 |
| ✅ 任务完成 | 新指令 | 继续工作 |

## 多开 Claude Code

CBuddy 天然支持同时运行多个 Claude Code 会话。

每个 Claude Code 会话有唯一的 `session_id`。同一个 session 的所有通知会归入同一个飞书话题（thread），你回复话题里的任意消息都会注入到对应终端，互不干扰：

```
Terminal Tab 1 (session abc)  <-->  飞书话题 A
Terminal Tab 2 (session def)  <-->  飞书话题 B
Terminal Tab 3 (session ghi)  <-->  飞书话题 C
```

只需要运行一个 `cbuddy serve`，所有 Claude Code 会话共享同一个 CBuddy 服务。

## How It Works

1. Claude Code [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) 在任务完成/需要输入时触发，通过 `cbuddy hook` 命令将事件（含 session_id、消息内容）POST 到本地 CBuddy 服务
2. CBuddy 通过飞书 API 发消息，同一 session 的消息自动归入同一话题线程
3. 用户在飞书回复，通过 WebSocket 长连接实时推送到 CBuddy
4. CBuddy 通过 AppleScript 把回复内容 keystroke 到对应 Terminal.app tab

## CLI

```bash
cbuddy serve                              # 启动服务
cbuddy install-hooks                       # 安装 Claude Code Hooks
cbuddy test-inject /dev/ttys003 "hello"    # 测试注入
cbuddy test-inject /dev/ttys003 "y" --no-enter
```

## Requirements

- macOS (AppleScript + Terminal.app)
- Python >= 3.10
- 飞书企业自建应用 (免费)

## [License](LICENSE)

MIT
