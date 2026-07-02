# Feishu/Lark 4-Instance Profile Deploy

WalkCode V3 的标准本地部署：{work, personal} × {claude, codex} 共 4 个运行实例。
work 两个 bot 在公司飞书租户（open.feishu.cn），personal 两个 bot 在 Lark 租户
（open.larksuite.com）。设计决策见 ADR 0043（profile 拆分）、ADR 0044（Lark live
ingress）、ADR 0045（/repo 工作目录）。

每个实例 = 1 个 profile + 1 个渠道 + 1 个 bot 身份 + 1 个 agent + 1 份 env +
1 个状态文件 + 1 个 launchd 服务。

## 1. Bot 前置条件（每租户一次）

每个 bot 应用需要：

- 开启机器人能力；
- 权限：`im:message`、`im:message:send_as_bot`、`im:resource`；
- 事件订阅使用**长连接模式**（免公网回调），订阅 `im.message.receive_v1`；
- 卡片回调 `card.action.trigger`（长连接同通道）；
- 发布版本。

work 可复用 V2 时代已配好的两个公司飞书 bot；personal 在 Lark 开发者后台新建
两个。

## 2. Agent Profile 配置目录（每 profile 一次）

`~/.local/bin` 下有四个 profile wrapper（独立可执行脚本，任何 shell 上下文都生效）：

| wrapper | 等价于 |
|---|---|
| `claude-work` | `CLAUDE_CONFIG_DIR=~/.claude-profiles/work claude` |
| `claude-personal` | `CLAUDE_CONFIG_DIR=~/.claude-profiles/personal claude` |
| `codex-work` | `CODEX_HOME=~/.codex-profiles/work codex` |
| `codex-personal` | `CODEX_HOME=~/.codex-profiles/personal codex` |

首次登录（每 profile 一次）：

```bash
claude-work      # 登录后 /exit；claude-personal 同理
codex-work login # codex-personal login 同理
```

**日常规则：终端起 TUI 一律用 wrapper，不用裸 `claude`/`codex`。** hook 配置
住在各 profile 的配置目录里，用哪个 wrapper 启动，TUI 观察就锚定到哪个
runtime 实例；裸命令读 `~/.claude`/`~/.codex`，不属于任何 profile。
Codex 的 managed app-server daemon 也按 CODEX_HOME 分家（每 profile 一个
daemon + socket）。

TUI hook 归属锚定：把 walkcode hook 命令写进各 profile 的
`{CLAUDE_CONFIG_DIR}/settings.json` / `{CODEX_HOME}/hooks.json`，**命令必须显式
带该 profile 的 env 文件**（没有隐式默认，漏配会直接报错而不是错投）：

```
WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env walkcode native hook <type> --agent claude --defer
```

## 3. Env 文件（×4）

`~/.walkcode/{profile}-{agent}.env`，模板见 `.env.example`。关键差异项：

| | work-claude | work-codex | personal-claude | personal-codex |
|---|---|---|---|---|
| WALKCODE_PROFILE | work | work | personal | personal |
| WALKCODE_AGENT | claude | codex | claude | codex |
| LARK_APP_ID/SECRET | 公司 bot A | 公司 bot B | Lark bot C | Lark bot D |
| LARK_OPENAPI_DOMAIN | open.feishu.cn | open.feishu.cn | open.larksuite.com | open.larksuite.com |
| WALKCODE_CLAUDE_CONFIG_DIR | ~/.claude-profiles/work | — | ~/.claude-profiles/personal | — |
| WALKCODE_CODEX_HOME | — | ~/.codex-profiles/work | — | ~/.codex-profiles/personal |

共同项：`WALKCODE_CHANNEL=lark`、`LARK_ALLOWED_CHAT_IDS`/`LARK_ALLOWED_OPEN_IDS`
白名单、`WALKCODE_CWD`、按需 `WALKCODE_WORKSPACE_ROOTS`（启用 `/repo`）。
状态路径和 codex socket 不用写，按 profile 自动推导。

## 4. launchd（×4）

`~/Library/LaunchAgents/com.walkcode.{profile}-{agent}.plist`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.walkcode.work-claude</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/zsh</string>
    <string>-lc</string>
    <string>WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env walkcode native serve</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/Users/you/.walkcode/logs/work-claude.out.log</string>
  <key>StandardErrorPath</key><string>/Users/you/.walkcode/logs/work-claude.err.log</string>
</dict>
</plist>
```

装载：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.walkcode.work-claude.plist
```

升级用的重启列表（写进 shell 环境或升级前 export）：

```bash
export WALKCODE_V3_LAUNCHD_LABELS="com.walkcode.work-claude,com.walkcode.work-codex,com.walkcode.personal-claude,com.walkcode.personal-codex"
```

`walkcode upgrade` 会安装 `--with claude-agent-sdk --with lark-oapi` 并逐个
kickstart 上述 label。

## 5. 逐实例验收（按顺序，过一个再开下一个）

对每个实例：

```bash
export WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env

# 1) 配置与凭证自检（SDK-free tenant token 探测）
walkcode native doctor
walkcode native debug lark

# 2) live 卡片门禁（发卡→patch，需 WALKCODE_E2E_LARK=1 + CHAT_ID）
python3 scripts/channel_native_debug.py --env-file $WALKCODE_ENV_FILE lark --live

# 3) 常驻
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.walkcode.work-claude.plist
```

真人验收清单（每实例在目标群里做一遍）：

- 根会话发文本 → 新话题会话建立、回复回到话题；
- `/repo <目录> <任务>` → 会话在指定目录启动（状态卡显示 cwd）；
- 权限卡三按钮（允许/拒绝/始终允许）回环；
- AskUserQuestion 三模式（单选 / 多选 toggle+提交 / 其他自由文本）；
- 发图片/文件 → agent 收到本地附件；
- `/status`、`/sessions`、`/model`；
- TUI 起会话 → 话题只读观察 → 接管提示 → 接管后可写。

部署顺序：work-claude → work-codex（验证 CODEX_HOME 双 daemon 隔离）→
personal-claude / personal-codex（验证 larksuite 域名差异）。

## 6. Telegram 实例退役

4 个 Lark 实例稳定运行约一周后：

```bash
launchctl bootout gui/$(id -u)/com.walkcode.telegram-claude
launchctl bootout gui/$(id -u)/com.walkcode.telegram-codex
```

env/state 文件归档不删；Telegram 渠道代码与测试保留（架构验证通道，见
ADR 0044）。

## 7. 已知边界

- Lark WS 断线重连会重投事件：InboundLedger 按 `lark:{event_id}` 去重，验收时
  建议演练一次断网；
- 卡片回调 3 秒窗口偶发超时：内联降级为"正在处理…" toast，终态由 outbox 的
  editCard patch 兜底；
- `serve --once` 不支持 lark（WS 推送无拉取语义），预检用 doctor + debug lark。
