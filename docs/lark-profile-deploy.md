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

### 1.1 personal 的个人飞书 fallback bot（Lark 配额耗尽时）

Lark 免费租户 API 额度为每月 10000 次调用，耗尽后（错误码 99991403）personal
两实例的出站消息全部失败，且额度到次月 1 日才恢复。fallback 方案：在**个人
飞书租户**（open.feishu.cn 个人版）再建一对同名 bot，随时可切：

| 飞书个人版 app | App ID | 服务实例 |
|---|---|---|
| Claude Code | `cli_aac0e4cd5238dcc2` | personal-claude |
| Codex | `cli_aac0da7b7df8dcdc` | personal-codex |

app 配置与第 1 节清单完全一致（Bot 能力 + 4 scope + 长连接事件/回调 +
发布版本；个人版租户发版免审核、即时生效）。

**切到飞书 fallback**（2026-07-05 已执行）：

1. 备份 Lark env：`cp personal-claude.env personal-claude.env.lark-backup`（codex 同理）；
2. env 换成飞书 app 的 `LARK_APP_ID`/`LARK_APP_SECRET`，
   `LARK_OPENAPI_DOMAIN=https://open.feishu.cn`，白名单清空（bootstrap）；
3. `launchctl kickstart -k` 两实例，向新 bot 各发一条消息，从
   `{profile}-state.json` 抓真实 `open_id`/`chat_id` 回填白名单，再 kickstart。

**切回 Lark**（次月额度恢复后）：`cp personal-{claude,codex}.env.lark-backup`
覆盖回 env，`launchctl kickstart -k` 两实例即可——Lark state 里的旧话题绑定
未清除，切回后原话题继续可用。

已知噪音：切换 bot 后，state 里绑定旧 bot 话题的存活会话（尤其还开着的
TUI daemon 会话）发进度消息会报 `230002 Bot/User can NOT be out of the chat`
并丢弃——属预期，旧会话结束后自然消失，不影响新会话。注意这些失败调用同样
消耗当前 bot 的 API 额度，切换后尽快结束旧终端会话。

## 2. Agent Profile 配置目录（每 profile 一次）

`~/.local/bin` 下有四个 profile wrapper（独立可执行脚本，任何 shell 上下文都生效）：

5 wrapper ↔ 5 实例 ↔ 5 bot 对应（2026-07-04 定型）：

| wrapper | 路由 | walkcode 实例 | bot |
|---|---|---|---|
| `claude-work` | enterprise 订阅 OAuth | work-claude | 飞书 Claude Code |
| `claude-work2` | 公司 Claude llm-proxy（Vela key，`~/.claude-profiles/work2` 独立 profile） | work2-claude | 飞书 ccp |
| `claude-personal` | Vertex 直连 | personal-claude | Lark Claude Code |
| `codex-work` | 公司 Codex llm-proxy（Vela key） | work-codex | 飞书 Codex |
| `codex-personal` | Azure（本地 proxy） | personal-codex | Lark Codex |

应急 Vertex 路由片段保留在 `~/.claude-profiles/work/routes/vertex.json`
（`claude --settings` 按次注入，或写 `WALKCODE_CLAUDE_SETTINGS` 给实例用）。

⚠️ 建新 bot 的两个坑（ccp 实测）：p2p 消息事件投递必须加**专用 scope**
`im:message.p2p_msg:readonly`（大 scope `im:message` 不够），且 scope 要随
版本发布才对事件路由生效；`open_id` 按应用隔离，白名单不能复用其他 bot 的
open_id——先放空白名单收首条事件抓真实值再回填。

历史 wrapper `cc`/`ccv`/`ccp` shell 函数（`~/.agent-control-plane/agent-wrappers.sh`）
已于 2026-07-03 移除；`ccs`/`codex-api` 归档在 `~/.walkcode-attic/20260703-wrappers/`。
telegram 双实例已于 2026-07-04 退役（plist 在 `~/.walkcode-attic/20260704-telegram/`）。

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

claude 的 **PreToolUse 例外**：daemon 多端闭环（ADR 0046 v2/v3）要求它用
gate 变体，且必须放大 Claude 侧 hook 超时（v3 对 daemon 会话捕获后立即弃权，
但 dontAsk / 非 daemon 会话仍走阻塞路径，默认 60s 会先杀掉 hook、静默退
回终端原生提示）：

```json
"PreToolUse": [{"matcher": "", "hooks": [{
  "type": "command",
  "command": "WALKCODE_ENV_FILE=$HOME/.walkcode/work-claude.env walkcode native hook PreToolUse --agent claude --gate",
  "timeout": 1830
}]}]
```

gate 行为（v3 真双端）：AskUserQuestion 与会原生弹权限的工具（Bash/Edit/Write
等，减去 allow 规则命中）在 daemon 会话上**终端对话框与飞书卡片同时可答，先答
先生效**——飞书点卡经 attach 按键注入驱动原生对话框；dontAsk / 非 daemon 会话
保留 v2 阻塞式（飞书为主）。walkcode 服务没在跑时 hook 自动弃权、终端原生提示
照旧。调参：`WALKCODE_CLAUDE_GATE_STYLE=dual|block`（block 整体退回 v2）、
`WALKCODE_CLAUDE_GATE_MODE=auto|off|ask_only`、`WALKCODE_CLAUDE_GATE_TIMEOUT`
（仅 block 路径）、`WALKCODE_CLAUDE_GATE_TOOLS`。

## 3. Env 文件（×4）

`~/.walkcode/{profile}-{agent}.env`，模板见 `.env.example`。关键差异项：

| | work-claude | work-codex | personal-claude | personal-codex |
|---|---|---|---|---|
| WALKCODE_PROFILE | work | work | personal | personal |
| WALKCODE_AGENT | claude | codex | claude | codex |
| LARK_APP_ID/SECRET | 公司 bot A | 公司 bot B | Lark bot C¹ | Lark bot D¹ |
| LARK_OPENAPI_DOMAIN | open.feishu.cn | open.feishu.cn | open.larksuite.com¹ | open.larksuite.com¹ |
| WALKCODE_CLAUDE_CONFIG_DIR | ~/.claude-profiles/work | — | ~/.claude-profiles/personal | — |
| WALKCODE_CODEX_HOME | — | ~/.codex-profiles/work | — | ~/.codex-profiles/personal |

¹ Lark 额度耗尽期间 personal 两列切到个人飞书 fallback bot
（open.feishu.cn，见 1.1 节）；Lark 原值备份在
`personal-{claude,codex}.env.lark-backup`。

共同项：`WALKCODE_CHANNEL=lark`、`LARK_ALLOWED_CHAT_IDS`/`LARK_ALLOWED_OPEN_IDS`
白名单、`WALKCODE_CWD`、按需 `WALKCODE_WORKSPACE_ROOTS`（启用 `/repo`）。
状态路径和 codex socket 不用写，按 profile 自动推导。

claude 实例默认保留 daemon 传输能力（ADR 0046，`DAEMON_MODE` 默认 auto）：
**bg 会话**（`daemon_live`）飞书直写走 daemon `reply`，socket 路径由
`WALKCODE_CLAUDE_CONFIG_DIR` 自动推导；普通 TUI 会话走 hooks 只读观察 +
takeover（ADR 0050 默认形态）。要彻底禁用 daemon 面设
`WALKCODE_CLAUDE_DAEMON_MODE=off`。

单 master UI（ADR 0050，2026-07-13 起为默认，翻回 ADR 0048 的 daemon 默认）：
`WALKCODE_CLAUDE_SPAWN_MODE` 默认 `headless`——飞书新建会话 headless 出生
（飞书独占），TUI 会话 hook 只读观察 + takeover 乒乓；attach 端双端并发渲染
混乱是翻回的原因。双 UI 大一统（ADR 0048：飞书新建会话生而为 daemon bg
worker，终端可 attach、飞书 v3 真双端）仍完整可用，显式设
`WALKCODE_CLAUDE_SPAWN_MODE=daemon` 开启；显式 `SPAWN_MODE=daemon` +
`DAEMON_MODE=off` 的矛盾组合在配置期报错。
`WALKCODE_CLAUDE_LIST_ADOPT=off` 关掉 list 兜底收编（默认开：walkcode
不认识的活 daemon job——如手动 `claude --bg`——会被补建为观察会话）。
要彻底关掉 daemon 面（含收编与 reply 直写），设
`WALKCODE_CLAUDE_DAEMON_MODE=off` 单变量即可。

⚠️ 收编（及一切 TUI 观察会话）依赖一个可解析的观察群：`LARK_ALLOWED_CHAT_IDS`
若不止一个，必须显式设 `WALKCODE_LARK_TUI_CHAT_ID`，否则收编只会静默跳过并
打 `claude daemon list adopt skipped ...`——开关看似生效却见不到观察会话。
只有单条白名单群时才会自动用它当观察群。收编策略可在 `native doctor` 的
`claude_daemon.spawn_mode` / `list_adopt` 字段核对实际生效值。

claude wrapper 默认回归纯 TUI（ADR 0050）：wrapper 内置
`WALKCODE_NO_BG=1`，裸启动 = 普通 `claude` TUI，`--resume` 恢复官方原义，
`/exit` 就是退出。飞书侧对 TUI 会话只读观察，想写先过 takeover 卡；终端
`claude --resume <uuid>`（用状态卡上的最新 id）即夺回 TUI master。

handoff 撞上 pending 提问/权限卡时（ADR 0051）：终端 resume 认领会立即
释放原 headless worker（限时 shutdown，pending 权限按 deny 解除）并在
话题补发「已过期，请到终端作答」通知（原卡点按被 generation 校验拒绝）；
takeover 方向可选 `WALKCODE_HANDOFF_CONTINUE=auto` 让悬空提问在接管后
自动以新卡重现（默认 off，真机验证重问率后再开）。

如需临时回到 daemon-native 双 UI（ADR 0048 形态：裸启动 = `claude --bg` +
attach + `--resume` DWIM），在 wrapper 里去掉 `WALKCODE_NO_BG=1` 并把实例
env 的 `WALKCODE_CLAUDE_SPAWN_MODE` 显式设回 `daemon`；attach 模式下
`/exit` = detach（会话保活），结束用 `claude stop <short>`，DWIM 调试用
`WALKCODE_RESUME_DWIM_DRYRUN=1`。

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

daemon-native 会话另验（ADR 0046 v3，真双端）——**ADR 0050 后这是显式
opt-in 路径**，验收前先去掉 wrapper 的 `WALKCODE_NO_BG=1` 并在实例 env
显式设 `WALKCODE_CLAUDE_SPAWN_MODE=daemon`（或直接手动 `claude --bg` 起
会话），否则以下双端行为不会出现：

- 飞书发消息 → 终端实时出现该输入，飞书**无 "TUI input" 回显**、用户消息
  被贴表情回执（reaction 失败时回退 "✅ 已发送到终端会话" 文本）；
- 会话内触发 AskUserQuestion → **终端原生对话框与飞书卡片同时出现**（卡片
  带"终端与飞书均可回答，先答先生效"注记）；飞书点选提交 → 终端对话框被
  按键注入解除、卡片翻"✅ 已回答"、模型按答案继续；
- 会话内触发权限工具（如 Bash 写命令）→ 终端权限框与飞书权限卡同时出现；
  飞书点允许 → 命令执行、卡翻"✅ 已允许"；点拒绝 → 命令不执行、turn 取消
  回 idle（会话可继续输入）；
- **终端先答**：终端按键后话题出现"✅ 已在终端处理"，其后迟点旧卡 →
  卡片如实翻"已在终端处理，本卡片未生效"（不得显示成功）；
- "始终允许"：本会话内同工具后续**零卡片自动放行**（serve 日志见
  `auto_allow_session ... mode=notify` + `inject_ok`；重启 walkcode 后
  记忆失效属预期）；
- 自动放行类调用（如 `date` 这类安全只读命令）不发卡、不留悬空按钮；
- v3 卡在场时无旧橙色提醒卡、无 "Claude needs your permission" 英文透传；
  空闲会话不弹权限橙卡；
- `permission_mode=dontAsk` 与非 daemon 普通 TUI 会话仍走 v2 阻塞 gate
  （飞书为主答、终端等待）；
- 终端 `/exit`（detach）→ 状态卡不标已结束、无 Take over 按钮；
  `claude stop <short>` 后状态卡才转已结束。

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
- ~~两个 Codex bot 的入站事件投递自 2026-07-02 起失效~~ → **已定位并修复
  （2026-07-05）**：根因是 §2 已记载的老坑再犯——两个 codex env 的
  `LARK_ALLOWED_OPEN_IDS` 复用了同 profile claude bot 的 open_id（open_id
  按应用隔离，跨 bot 无效），p2p 消息全部被 sender 白名单**静默**拒掉
  （`UNAUTHORIZED`，无任何日志）。修复即本文的标准流程：临时放空
  OPEN_IDS → 收首条消息从 state 抓真实 open_id → 回填 → 重启，双实例
  收紧后复验通过。教训固化：改 env 后必须真机发一条消息回归；
  白名单拒收零日志是排障黑洞，后续给 UNAUTHORIZED 拒收加 degrade 日志。
- personal Lark 租户免费 API 月配额有限，耗尽（错误码 99991403）后该 bot
  当月无法再发消息/卡片。2026-07 月配额被状态卡无效重复 patch 烧穿后，
  v0.10.56 起状态卡刷新带指纹去重：仅实质状态变化（阶段/按钮/gate 等待等）
  才调 API，工具事件抖动、时长走字、事件序号不再触发 patch——忙会话的
  状态卡调用量从数千/天降到数十/天。
