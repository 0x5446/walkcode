# Feishu/Lark Profile Deploy

WalkCode V3 的本地部署：{work, personal} × {claude, codex}，另有 work2-claude，共 5 个实例。
work/work2 使用各自公司飞书应用，personal 两个 bot 使用个人飞书应用；
2026-09-13 起全部使用 open.feishu.cn。设计决策见 ADR 0043（profile 拆分）、ADR 0044（Lark live
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

work 可复用已配好的公司飞书 bot；personal 使用下面列出的两个个人飞书 bot。

### 1.1 personal 的个人飞书应用与迁移记录

Lark 免费租户 API 额度为每月 10000 次调用，耗尽后（错误码 99991403）personal
两实例的出站消息全部失败。2026-09-13 用户决定彻底转回**个人飞书租户**，
下列应用现在是 personal 的正式配置，不再自动切回 Lark：

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

**历史：切回 Lark**（2026-09-12 执行，2026-09-13 已撤回）：把 6 个租户键
（`LARK_APP_ID`/`LARK_APP_SECRET`/`LARK_OPENAPI_DOMAIN`/`LARK_ALLOWED_CHAT_IDS`/
`LARK_ALLOWED_OPEN_IDS`/`WALKCODE_E2E_LARK_CHAT_ID`）从 `.env.lark-backup` 抄回
active env，其余键保留（飞书期新增的 `WALKCODE_CODEX_SANDBOX` 等不能被整文件
覆盖冲掉），再 `launchctl bootout`→`bootstrap` 两实例。

**2026-09-13 实机排障记录**：Lark 的 `Claude Code` 应用经 bot info API 确认为
`cli_aa8ff6ae7e781e18`，对应 `~/.walkcode/personal-claude.env`、
`com.walkcode.personal-claude`。其中 `WALKCODE_CLAUDE_CONFIG_DIR` 指向
`~/.claude-profiles/personal`，即 `claude-personal` 的配置。
11:39（UTC+8）的“你是什么模型”已进入该 profile 的 Claude transcript 并完成回复，
但根卡 create 和回复 reply 均返回 `99991403`，回复保留在 state 的 outbox.dead。
贴表情只表示收到；根卡失败后会退回以用户消息为根，若回复也失败，界面就没有话题。
重启、升级或重发消息不能恢复租户配额。完整计费流水不在本地 state/log 中，
不能据镜像消息数量推定精确消耗；恢复发送需要租户额度恢复，或另行决定渠道迁移。

**2026-09-13 转回个人飞书**：仅替换上述 6 个租户键；Claude config dir、Codex home、
provider、sandbox 等保持原值。两实例停机后完整备份 env/state/旧队列，再建立干净的
飞书渠道状态；2 个存活 Claude TUI、1 个 Codex TUI 及本次未送达回复所在会话迁入
新话题并重新授权各自应用的 owner。活跃会话的待处理 hook 随迁，旧 Lark 收件箱和
其他历史队列不向飞书重放。604 个历史会话留在完整备份中，agent transcript 未改动。
备份与迁移脚本：`~/.walkcode/backup-feishu-return-20260913-v0.14.26/`。
两套个人飞书应用的真实 agent、工具卡、话题回复、失败通知读回均已验证通过。

**只换 env 不够**：飞书期建立的 TUI 观察会话，`channel_binding` 里存的是飞书
chat/message id，切回后这些话题用 Lark bot 发消息会一路 230002（bot 不在那个
群）。停实例 → 对每个 pid 仍存活的会话在 Lark 群发一张新根卡 → 改
`channel_binding` 的 chat/thread/root/health + 挪 `binding_to_session` 索引 key +
补一条 Lark open_id 的 owner grant → 再起实例。已停会话不用管。迁移脚本存档在
`~/.walkcode/backup-lark-switch-*/migrate_live_tui_to_lark.py`。

**代价**：open.larksuite.com 从本机的 API 往返约 288ms，open.feishu.cn 约
105ms（2026-09-12 各 5 次采样中位数）。每次渠道调用贵 2.7 倍，重工具量会话更容易
把 hook 排水队列压出积压——实测确实压出来了，但这不是必然，取决于产出速率和
hook 构成（见 §7 已知边界）。

切换时若留下旧 bot 的 chat/message id，会产生 `230002 Bot/User can NOT be out
of the chat` 并丢失输出。不要靠等待旧会话结束消除错误；迁移活跃话题并隔离旧队列。

## 2. Agent Profile 配置目录（每 profile 一次）

`~/.local/bin` 下有五个 profile wrapper（独立可执行脚本，任何 shell 上下文都生效）：

5 wrapper ↔ 5 实例 ↔ 5 bot 对应（2026-07-04 定型）：

| wrapper | 路由 | walkcode 实例 | bot |
|---|---|---|---|
| `claude-work` | enterprise 订阅 OAuth | work-claude | 飞书 Claude Code |
| `claude-work2` | 公司 Claude llm-proxy（Vela key，`~/.claude-profiles/work2` 独立 profile） | work2-claude | 飞书 ccp |
| `claude-personal` | Vertex 直连 | personal-claude | 个人飞书 Claude Code |
| `codex-work` | 公司 Codex llm-proxy（Vela key） | work-codex | 飞书 Codex |
| `codex-personal` | Azure（本地 proxy） | personal-codex | 个人飞书 Codex |

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
runtime 实例。

裸命令读 `~/.claude`/`~/.codex`。2026-09-12 起这两份裸配置也装了 walkcode
hook，锚到 **personal** 两实例（`~/.claude/settings.json` 的 hooks 段、
`~/.codex/hooks.json` + `~/.codex/config.toml` 的 `[hooks.state]` 信任哈希），
所以忘了用 wrapper 也不会彻底断掉镜像。但它只是兜底：裸 claude 的
`CLAUDE_CONFIG_DIR` 是 `~/.claude` 而实例配的是
`~/.claude-profiles/personal`，接管/resume 会用后者，MCP 与权限设置对不上。
换 codex hooks.json 后必须同步换 `config.toml` 里 `[hooks.state."<绝对路径>:<事件>:0:0"]`
的 `trusted_hash`（key 含 hooks.json 绝对路径，哈希不对 codex 会静默不跑 hook）。
哈希算法没有公开、也不是整份文件的普通 sha256，**别手算**。两条可行路径：
① 若新 hooks.json 与某个 profile 的那份逐字节相同，直接把该 profile
`config.toml` 里的 `[hooks.state]` 整段搬过来，只改 key 里的绝对路径——哈希跟
内容走，不跟路径走（2026-09-12 裸 codex 就是这么接上的）；② 内容不同就用对应
`CODEX_HOME` 起一次 codex，在 `/hooks` 里逐项 review 并信任，由它自己写回。
改完必须真发一次事件验收：跑一条会触发 hook 的命令，确认队列目录多出文件或
频道收到卡片。**`walkcode native doctor` 不校验信任状态**，它只看 hooks.json
里的事件和命令，哈希错了它照样报正常。
Codex 的 managed app-server daemon 也按 CODEX_HOME 分家（每 profile 一个
daemon + socket）。

**裸配置锚死在 personal，等于放弃了工作/个人的租户隔离。** hook 不校验
`cwd`：在公司仓库里忘用 wrapper、直接敲 `claude`/`codex`，这次会话的 prompt、
回复、工具参数就镜像进个人飞书群，群里的白名单账号还能接管它。所以裸配置
只当兜底，公司仓库一律用 `claude-work` / `codex-work`。真要堵死，得在
`process_tui_hook` 里按工作区根校验 `cwd` 并拒绝跨租户，那是另一件事。

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
| LARK_APP_ID/SECRET | 公司 bot A | 公司 bot B | 个人飞书 Claude Code¹ | 个人飞书 Codex¹ |
| LARK_OPENAPI_DOMAIN | open.feishu.cn | open.feishu.cn | open.feishu.cn¹ | open.feishu.cn¹ |
| WALKCODE_CLAUDE_CONFIG_DIR | ~/.claude-profiles/work | — | ~/.claude-profiles/personal | — |
| WALKCODE_CODEX_HOME | — | ~/.codex-profiles/work | — | ~/.codex-profiles/personal |

¹ personal 两列已于 2026-09-13 正式切回个人飞书（见 1.1 节）；Lark 原值备份在
`personal-{claude,codex}.env.lark-backup`。

共同项：`WALKCODE_CHANNEL=lark`、`LARK_ALLOWED_CHAT_IDS`/`LARK_ALLOWED_OPEN_IDS`
白名单、`WALKCODE_CWD`、按需 `WALKCODE_WORKSPACE_ROOTS`（启用 `/repo`）。
状态路径和 codex socket 不用写，按 profile 自动推导。

codex 实例的沙箱默认**跟随 codex profile 自己的 `sandbox_mode`**，walkcode 不插手。
只有显式设 `WALKCODE_CODEX_SANDBOX=read-only|workspace-write|danger-full-access`
才会覆盖 profile 的设置，新建（`thread/start`）和恢复（`thread/resume`）两条路径都带上。
0.14.22 及之前这里硬兜底 `read-only`，会静默压过 profile 里的 `danger-full-access`——
表现是频道里起的每个线程都断网、写不了盘，而模型只能把它描述成"整台机器被锁死"，
看不出是 walkcode 干的。

最终生效的沙箱以 app-server 在 `thread/start` / `thread/resume` 响应里回显的为准，
不要按环境变量推断：profile 指错、`CODEX_HOME` 写错、config.toml 被改都会让两者不一致。
walkcode 会把回显值记在 `CodexAppServerTransport.effective_sandbox`；显式覆盖没被服务端
采纳时打 `walkcode degrade=codex_sandbox_override_ignored`。profile 完全没写
`sandbox_mode` 时 app-server 回落 `read-only`（fail-closed，不是 workspace-write）。

**白名单闸**：最终生效沙箱是 `danger-full-access` 而 `LARK_ALLOWED_CHAT_IDS` /
`LARK_ALLOWED_OPEN_IDS`（Telegram 对应 `TELEGRAM_ALLOWED_CHAT_IDS` /
`TELEGRAM_ALLOWED_ACTOR_IDS`）全为空时，walkcode 拒绝起线程并报错。这两个白名单留空
等于放行所有人，叠上无沙箱、`approval_policy=never` 就是任何人都能远程在这台机器上执行
任意命令。确实要这么跑就显式设
`WALKCODE_CODEX_ALLOW_UNRESTRICTED_WITHOUT_ALLOWLIST=1`。

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
takeover 方向默认 `WALKCODE_HANDOFF_CONTINUE=auto`——接管后悬空的提问
自动以新卡重现（注入对话题不可见；重问由模型执行，措辞可能与原问略有
出入）。不想要自动续接设 `WALKCODE_HANDOFF_CONTINUE=off`。

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
- `/status`（**Session** 显示 agent 自己的 id，**WalkCode** 才是账本 key）、
  `/sessions`、`/model`；
- `/reload`（或 `/restart`）→ 后端重启、会话保留，下一条消息在同一个话题里
  复活并带上当前配置（新加的 MCP 这时才生效）；
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
personal-claude / personal-codex（验证个人飞书身份隔离）。

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
- **Lark 租户下，重工具量会话的 hook 镜像会滞后。** 排水每批上限 25 条、
  单批 30s 超时，**整批跑完或超时后再 sleep 1s** 才起下一批
  （`_drain_deferred_tui_hooks_forever`，`TUI_HOOK_DRAIN_*` 硬编码无 env 开关）——
  不是每秒定时发一批，所以最慢的批间隔接近 31s。
  每条 hook 的渠道开销也不是定值：工具类 hook 一条就可能刷状态卡 + 补叙述消息 +
  upsert 工具进度（`_send_tui_hook_output`），有的 hook 则一次调用都不发。
  open.larksuite.com 一次 API 往返约 288ms（feishu.cn 约 105ms，口径同 §1.1），
  同样的会话在 Lark 下排水慢一截。产出长期高于实际排水能力时队列就持续增长——
  2026-09-12 实测重工具量会话 2 分钟 +94 条，滞后涨到 8 分钟。
  **别把某次实测的吞吐当成代码保证的阈值**，它随 hook 构成和卡片去重命中率变。
  积压本身落在磁盘上，`_deferred_tui_hook_paths` 把 300s 内的新 hook 排在更旧的
  积压之前。**但这只是优先级，不是实时保证**：recent 桶内部仍是先进先出，
  持续过载时新 hook 同样要排在本桶已有积压之后，延迟可逼近 300s；被挤出窗口的
  旧 hook 要等产出降到排水能力以下才补齐。hook 没有过期丢弃机制。
  **也别把磁盘队列理解成"一条都不会丢"**：工具进度卡按设计不走 outbox 重试，
  发送失败只记一条 `tool_progress_send_failed`（只有 `PermanentDeliveryError`
  才会顺带把绑定标成投递异常，网络抖动这类瞬时错误连标记都没有），hook 仍算
  accepted、队列文件照删（`channel_native/__init__.py` 的 `send_view` 失败分支
  + `channel_native_runtime.py` 排水里的 `result.accepted → path.unlink()`）。
  队列保的是积压不丢，不是渠道故障不丢。
  盯两个数就够：队列目录 `<state 文件名>.tui-hooks.d`（含 `.json`，例如
  `personal-claude-state.json.tui-hooks.d`）的文件数，和最老文件名前缀
  （纳秒时间戳）换算出的滞后。
