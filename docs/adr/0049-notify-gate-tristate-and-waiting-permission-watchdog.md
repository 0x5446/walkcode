# ADR 0049: notify 探针三态化 + blocked 对话框诚实层 + WAITING_PERMISSION 看门狗

Date: 2026-07-10

Status: Accepted; implemented 2026-07-10（本 ADR 为 v0.12.0 上线 48h 观察期
两起真实事故的修复记录；`choose:` 类对话框的飞书交互层另行实施，见「后续」）。

## Context

v0.12.0（ADR 0048）发布后 48 小时，生产日志暴露两起独立事故，加一个
现场捞到的变体：

**事故一：探针盲区被当成"从未渲染"，飞书卡静默丢失（2026-07-09 11:09，work）**

- 终端 TUI 会话 `6f101d17`（daemon job）触发 Edit 权限对话框；
  PreToolUse gate 正确走了 NOTIFY 路径。
- 当时宿主机整体拥塞：飞书 API 2200 网关超时、binding refresh 连续 7 次
  爆 15s 预算、hook drain 3 次超时，daemon 控制 socket 请求也在超时。
- `notify_dialog_waiting` 把 `TransportUnavailable`（含 10s 请求超时，
  `claude_daemon.py:_request`）折叠成 `False`——注释原话："No job or probe
  outage: either way, no dialog is confirmed waiting"。
- drain 侧 `waiting=False` 且挂龄 > 30s 宽限 → `notify_dialog_never_rendered`
  → **销毁 pending**。而对话框真实在等：用户 11:17:31 才回终端批准。
  这 8 分钟正是飞书卡最有价值的窗口。
- v0.10.61 给 stop 守卫引入过探针三态（`job_alive` 的 True/False/None），
  但 notify 路径刻意折叠了三态——本 ADR 把三态打穿到 drain 决策。

**事故二：非 gate 对话框在 daemon 生会话上是死路（2026-07-09 11:55，work）**

- 飞书新建（daemon_spawn）会话 `tui-claude-9d120e0f8509` 的 worker 弹出
  模型降级确认，daemon 上报 `tempo=blocked, needs="choose: retry on
  fallback model or edit prompt"`。
- `choose:` 不是工具调用，不经过 PreToolUse gate → 无 notify gate →
  state patch 兜底发纯文字橙卡，文案"需要在终端里处理"。
- 但 daemon 生会话**没有终端**。会话卡在 WAITING_PERMISSION 6.5 小时，
  飞书侧无按钮、无提醒、无人知晓。
- 次日对话框已消失（job 转 adopted），walkcode 状态仍停留在
  WAITING_PERMISSION——状态卡陈旧，无对账路径。

**变体三：blocked + 空 needs（live 观察 job `fbb571e0`）**

daemon 可能上报 blocked 但 needs 为空/滞后。原实现把空 needs 当"已解除"，
会把仍在等待的会话翻回 EXTERNAL_OBSERVED_READONLY。

## Decision

设计原则两条：**没有 daemon 背书的证据就不销毁通知**；**任何 blocked
会话在飞书至少有一张诚实的卡**。

### A. 探针三态打穿到 drain（事故一根治）

1. `notify_dialog_waiting` → `notify_dialog_state`，返回三态：
   - `DIALOG_WAITING`：daemon 应答且对话框匹配在等；
   - `DIALOG_NOT_WAITING`：daemon 应答正常，无此对话框（job 缺失/未阻塞/
     needs 不匹配）——这才是 auto-approve 该丢卡的情形；
   - `DIALOG_UNKNOWN`：控制面不可达/超时。异常同样归 UNKNOWN（异常是
     故障，不是裁决）。
2. drain 策略：
   - WAITING → 交互卡（原逻辑）；
   - NOT_WAITING 超 30s 宽限 → 丢弃（原逻辑，防悬空按钮的初衷保留）；
   - UNKNOWN → **不计入 never_rendered 宽限**。连续盲区超
     `CLAUDE_GATE_PROBE_BLIND_NOTICE_SECONDS`（90s）发一张**无按钮**诚实
     提醒卡（无活按钮=无悬空按钮风险）；盲区挂龄超
     `CLAUDE_GATE_NOTIFY_PENDING_MAX_AGE_SECONDS`（900s）才回收 pending
     （notify pending 无 hook deadline，不设上限会泄漏 spool；之后由
     看门狗兜底）。
3. hook 证据规则：PermissionRequest hook 到达即对话框渲染过的直接证据
   （记录 `(daemon_short, tool_name) → ts`）。盲区 + 有证据 → 直接发
   交互卡（点击前有 pre-injection 复验，卡片过期只会诚实降级，不会错注）；
   NOT_WAITING + 有证据 → 丢弃照旧但 trace 记
   `notify_dialog_settled_in_terminal`（不再谎报 never_rendered）。

### B. blocked 对话框诚实层（事故二止血 + 变体三）

1. `tui_permission_notice` 卡按会话形态分文案：daemon_spawn 会话给
   `claude attach <short>` 指引（"需要在终端里处理"对它是死路）；
   终端会话保留原文案。Telegram 渲染器同步。
2. `tempo=blocked` + 空 needs → 以占位 `BLOCKED_NEEDS_UNKNOWN` 进入
   blocking 分支（翻 WAITING_PERMISSION + 发卡），不再误判为"已解除"。

### C. WAITING_PERMISSION 看门狗（事故二两个方向的兜底）

挂在 daemon watch tick（`_sync_claude_daemon_watchers`）上，只消费
**查询成功的** job 列表（控制面故障根本到不了这里，"job 不在"天然是
daemon 背书的裁决）：

- **正向**：job 仍 blocked 且距上次进展/提醒超
  `WAITING_PERMISSION_REMINDER_SECONDS`（30min）→ 重发提醒卡（⏰ 标题）。
- **反向**：job 已不阻塞或已消失 → 会话对账回
  EXTERNAL_OBSERVED_READONLY，退掉 open notify gates，刷状态卡。
- 距上次进展不足 `WAITING_PERMISSION_RECONCILE_MIN_AGE_SECONDS`（60s）
  不动作——不和在途 patch 抢状态。

## Consequences

- 控制面抖动不再吞卡：最坏情形从"静默丢失"变为"90 秒后一张无按钮提醒
  + 恢复后正常"。
- daemon 生会话的非 gate 对话框：从"死路文案 + 永久卡死"变为"attach
  指引 + 每 30 分钟提醒 + 对话框消失后自动对账"。飞书侧一键选择仍缺——
  那是 `choose:` 交互层（后续 PR）的事。
- 盲区提醒卡与交互卡可能对同一对话框各出现一张（先盲后明）：接受，
  两张都诚实，交互卡可点。
- 新增 4 个常量均硬编码（90s/900s/60s/30min），不加配置面。
- in-memory 记账（盲区时钟、提醒时钟、渲染证据）重启即清零：只延迟
  提醒，不丢 pending，可接受。

## 实施与验证记录（2026-07-10）

- 代码：`claude_daemon.py`（三态探针）、`channel_native_runtime.py`
  （drain 策略、渲染证据、诚实层、看门狗）、`lark_cards.py` +
  `channel_native/__init__.py`（卡文案分形态）。
- 回归测试 10 个新增/1 个修正（`test_channel_native_claude_gate.py`、
  `test_channel_native_lark_cards.py`）：盲区持卡不丢、盲区提醒只发一次
  + 900s 回收、盲区+hook 证据发交互卡、NOT_WAITING 照常丢弃、blocked
  空 needs 不误判解除、看门狗三分支（对账/提醒去重/在途不抢）。全量
  682 tests passed。
- 旧测试 `test_notify_unroutable_pending_removed_without_decision` 原依赖
  "探针抛异常=当作没渲染"的混淆行为，已改为 daemon 背书的 job-absent
  裁决，语义不变。

## Deep-review 记录（2026-07-10，host=Claude → codex engine，14 维 + 重点回证）

首轮 14 维并行审查捞出一个多维度共识的 Critical 和若干 Warning，已全部修复：

- **Critical（6 维命中：correctness/errors/security/design/completeness/feasibility）——
  同名工具并行 pending 的证据串台**：`_dialog_render_evidence` 原按
  `(daemon_short, tool_name)` 记账，两个并行 `Edit` pending 时，一个
  PermissionRequest hook 会让另一个 pending 也被判「已渲染」发出交互卡；
  而点击前 `_gate_dialog_matches` 的 permission 分支也只比工具名，可能把
  按键打到当前那个 `Edit` 对话框——用户以为批第二个，实际批了第一个。
  修法：证据改用 hook 的 `tool_use_id`（== gate rid）做精确键 `(short, rid)`；
  tool 名键仅在「该 tool 在本轮只有唯一 live pending」时才信（drain 开头
  做一次 same-tool 普查）。
- **Warning——看门狗单次空列表误对账**：反向对账原来一次 not-blocked 就清
  WAITING_PERMISSION，daemon 重启窗口的瞬时空列表会误伤。修法：新增
  per-session `_waiting_not_blocked_since`，not-blocked 必须**持续**跨
  `WAITING_PERMISSION_RECONCILE_MIN_AGE_SECONDS` 才清；匹配加 short 兜底
  （resume 漂移 uuid 时仍能识别为仍阻塞）；提醒/清理分支都在 `_ingress_lock`
  内复核 status/lifecycle/writer_owner 后才动手。
- **Warning——占位 needs 不升级**：blocked+空 needs 发了 `BLOCKED_NEEDS_UNKNOWN`
  占位后，真实 needs 到达时因 `already_waiting` 被抑制，用户一直看占位。
  修法：`placeholder_upgrade` 例外放行。
- **Warning——盲区提醒/hook 提醒绕过诚实层**：`_send_gate_blind_notice` 和
  `_send_tui_hook_output` 手写 view，daemon_spawn 会话拿不到 attach 指引，
  退回死路文案。修法：全部改走 `_tui_permission_notice_view`（新增
  `probe_blind`/`tool_name` 参数）。
- **Warning——rid 复用继承旧盲区状态**：drain 末尾按 live_rids 清理会把本轮
  刚删的 rid 当 live，跨轮复用同 rid 的新 pending 继承旧盲区时钟。修法：
  新增 `_forget_gate_probe_state(rid)`，在每个 remove_pending/register 分支
  即时清理。
- **Warning（observability）——盲区/看门狗留痕不足**：补
  `notify_dialog_probe_blind`、`notify_dialog_blind_notice_skipped`（带 reason）、
  `waiting_permission_reminder`、`waiting_permission_reconciled`、
  `waiting_permission_watchdog_unmatched` 等 gate trace。

第二轮回证：5 条修复里 4 条 FIXED，1 条 INCOMPLETE——看门狗反向清理的锁内
复核只查了 session 状态，没复查 `last_progress_at`。竞态：reconcile 锁外算好
「该清理」后 await 拿锁，若等锁期间 blocked patch 回来（它保持
WAITING_PERMISSION 但 bump `last_progress_at`），仅查 lifecycle 会放行，用陈旧
not-blocked 快照错清。修法：锁内加 `last_progress_at` dwell 复核——任何 patch
都会 bump 它，dwell 不再成立就退让。补一条竞态回归测试。

第三轮回证：全部 FIXED，无残留 Critical。测试从 682 增至 692（+10 回归：
同名并行证据、tool 键唯一性、证据新鲜度边界、rid 复用、盲区跳过留痕、
占位升级、看门狗持续 dwell/short 兜底/控制面故障不触发对账/锁内 progress 复核）。

## 后续

- `choose:`（及未来未知格式）对话框的飞书交互层：`_gate_dialog_matches`
  增加 choose 分支 + 通用数字按钮 + digit+Enter 注入（选项语义不解析，
  needs 原文锚定 + 点击前复验）。独立 PR。
- 事故一的诱因（binding refresh / hook drain 在飞书慢时反复整批超时）
  未在本 ADR 处理——它是拥塞放大器，不是丢卡根本原因。
