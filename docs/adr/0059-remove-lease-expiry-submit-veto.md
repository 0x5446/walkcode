# ADR 0059: 移除提交路径上的写者租约过期否决

Date: 2026-07-21

Status: Accepted; implemented

## Context

线上事故（2026-07-21，work profile / Lark）：一个 headless 会话的 turn 已
运行 39 分钟，用户在 thread 里发的追加消息没有任何反应——不贴表情回执、
不回提示，agent 也没收到。取证：

- 会话 `last_user_input_at` 停在上一条消息的时刻，说明新消息没走到提交；
- `writer_lease` 的 `heartbeat_at` 等于上一条消息提交时刻，`expires_at`
  为其 +30 秒（`lease_ttl=30`）——**turn 运行期间没有任何机制续租**，
  租约只在会话新建、IDLE 重取写者、takeover 三处刷新；
- `validate_submit` 对 ACTIVE 会话直接查租约（IDLE/ERROR_RECOVERABLE 才走
  `_ensure_writer_ready_for_submit` 重取），过期即返回 `LEASE_EXPIRED`；
- `LEASE_EXPIRED` 被设计为"非终局、不回提示"：赌渠道补推会在租约恢复后
  重试。但 Lark WS 入口 `on_message` 是 fire-and-forget（入队即返回，
  协议层永远算确认成功），**飞书不会补推**——消息就此永久静默丢失。

叠加结论：任何 turn 跑超过 30 秒后，频道发来的消息全部撞 `LEASE_EXPIRED`
被静默丢弃。mid-turn 注入本身是支持的（`client.submit` 可进流），拦截是
纯粹的误伤。

## Decision

废除 `validate_submit` 中的租约过期否决（`writer_lease is None or
expired` 分支），`LEASE_EXPIRED` 不再产生。理由：逐场景盘点后，这条检查
没有在保护任何真实存在的冲突——

- **同进程 orchestrator 写 headless 会话**（事故场景）：入站消息的串行
  来自 `ChannelNativeRuntime._ingress_lock`（serve 循环持锁逐条处理，
  Lark WS 与 Telegram 轮询同锁），不是"单进程 asyncio 天然串行"——
  协程会在 await 点交错。锁外唯一的提交入口是 ADR 0058 的
  `_replay_lost_turn` 自动重放，它有自己的 replay_guard 身份钉子
  （提交前两次复核暂存指针，旧重放自灭）和"自动重发失败"提示兜底。
  在这两道机制下，租约是自己防自己。
- **TUI 与频道争会话**：由归属类型检查（`external_tui` → readonly →
  daemon 注入/takeover 确认，ADR 0046）和代际围栏（takeover 时 generation
  递增，旧写者被 `STALE_GENERATION` 挡住）把守，与租约 TTL 无关。
- **worker 悄悄死了**：`submit_turn` 撞 `TransportUnavailable` 后自动
  fallback resume——"试着写一下"就是最准的判活，比墙钟 TTL 可靠。
- **并发 resume 双飞**：`ClaudeHeadlessTransport` 有每会话 `asyncio.Lock`
  包住 close-old → verify-dead → create-new 的原子阶梯（review R2 复现并
  修复过），不依赖租约。该锁只属于 claude_headless；其他 transport 的
  并发安全靠上面的入站串行。

`WriterLease` 结构与三处发放点保留，仅作账本记录（`heartbeat_at` 是
"最后一次取得/重取写权的时刻"——中途提交不刷新它；最近输入水位看
`last_user_input_at`）；不再用它否决提交。

## Alternatives considered

- **给运行中的 turn 续租心跳**：修的是"租约会误过期"，不修"租约在单进程
  里没有保护对象"。要新增心跳任务、处理心跳自身挂掉的误杀，换来的只是
  让一条冗余检查不再误伤——收益负。
- **主动判活（查 handle/进程存活后放行）**：判活结果在 await 窗口里就会
  过期，最准的判活就是提交本身（`TransportUnavailable` → resume 兜底
  已存在），再查一遍是重复。
- **保留否决 + 渠道可靠重投**：Lark WS 协议入口即收即确认，没有重投
  语义可依赖；把丢消息问题转嫁给"实现一个跨渠道重投队列"，规模远超
  本问题。
- **后来者 kill 前任（PID 账本）**：会把正在跑的 turn 杀掉换一条追加
  消息；PID 复用有误杀风险；kill→确认→认领仍需原子机制，绕回围栏本身。
  且与 ADR 0046（TUI 存活、daemon 注入）的方向相反。

## Rollback

- **触发条件**：真机观察到同一会话出现交错双写（两个 worker 同时向一个
  transcript 写入），且来源不是 transport resume 屏障的已知缺口。
- **怎么回退**：不能只 `git revert` 本变更——那会恢复"静默丢消息"事故。
  安全回退 = 恢复过期检查 **加上** 配套之一：turn 运行期续租心跳，或
  LEASE_EXPIRED 改为终局拒绝并给用户可见的"请重发"提示。同时回退
  debug 脚本、部署手册、ADR 0029/0030 标注与相关测试（一并在本变更
  的 commit 范围内，反向应用即可枚举）。

本决定部分推翻 ADR 0030 的"Active or waiting sessions still require a
non-expired writer lease"——该前提在实现里从未配套心跳，30 秒后必然
过期，语义已从"写者活着"退化为"最近 30 秒内有人提交过"。

## Consequences

- 长 turn 中途发消息恢复正常：注入成功、贴表情回执、盖
  `last_user_input_at` 水位。
- `channel_native_debug.py state` 不再把"运行中会话租约过期"计为故障或
  告警（这是长 turn 的常态）；`expired_writer_leases` 计数保留为观测项。
- `_LARK_REJECTION_NOTES` 与入站台账的 `LEASE_EXPIRED` 相关注释同步更新；
  台账终局集合不变（`LEASE_EXPIRED` 本就不在其中，如今也不再产生）。
- 诊断路径（`diagnose_telegram_ingress`）复用 `validate_submit`，对超过
  TTL 的 ACTIVE 会话不再报"not currently submittable"。
- 残留风险（均为存量或独立事项，另行跟踪）：
  - **跨进程双写**：两个 runtime 误配同一 `WALKCODE_STATE_PATH` 时无
    互斥（`_ingress_lock`/`_session_locks` 均为进程内）。需要时引入
    启动期状态文件 flock，而不是恢复"只发不续"的检查。
  - **Codex mid-turn 语义**：`CodexAppServerTransport.submit_turn` 固定
    `turn/start`，ACTIVE 会话追加消息会误开新 turn 而非 `turn/steer`。
    该行为在本变更前的 30 秒租约窗口内即存在，本变更把窗口扩到无限。
    正确修复是显式的 mid-turn 注入 capability（Claude 用 `client.submit`
    进流，Codex 实现 steer 或终局拒绝并回提示）。
  - **resume 屏障缺口**：`_capture_worker_proc` 拿不到 pid 时不登记旧
    worker，settle 先注销句柄再异步断开——并发 resume 可能漏等旧进程。
  - **诊断与消费不一致**：`diagnose_telegram_ingress` 未模拟启动清扫
    语义，对上一进程遗留的 ACTIVE 会话预测偏乐观（改动前同样不一致，
    方向相反）。

## Revision R1（deep-review 采纳，同版修复）

发版前 14 维度交叉审查（codex engine）+ 5 条源码回证，无 Critical。
同版采纳修复：

- **resume 兜底失败不再裸抛**：`submit_user_input` 的
  `TransportUnavailable` 分支里恢复被拒时，原样 `raise` 会越过渠道的
  拒绝提示（Lark WS 只记日志不重投），"worker 已死且恢复失败"的那一条
  消息只有状态卡翻错误态、没有 per-message 提示。改为执行同样的回滚 +
  `ERROR_RECOVERABLE` 后 `return retry`，让 `resume_failed` /
  `missing_resume_ref` 的提示发出去；`_resume_writer_for_submit` 吞异常
  处补 `_log_degrade("writer_resume_failed")`；`missing_resume_ref`
  新增提示文案（不可恢复，指引开新任务）。
- **文档同步**：部署手册删除 `expired_writer_leases: 0` 门禁；
  ADR 0029/0030 正文逐条标注推翻；实现设计文档同步。
- 审查报告：`docs/review/2026-07-21-adr0059-lease-veto-removal-review.md`。

## Revision R2（v0.14.12 上线后回归：pending 计数泄漏误报）

**现象**（2026-07-22 线上，`sess-2f60082f32234dc3ad2914949108f313` 等
4 个会话）：会话已正常回复完毕、处于 IDLE，1 小时后仍收到
"⚠️ 已提交的消息在 3600 秒内没有得到任何响应" 的 ceiling 误报
（`headless_pending_turn_ceiling`，pending_turns=1）。

**根因**：mid-turn 提交在 Claude CLI 侧有两种真实去向，提交时无法区分：

1. **吸收**：被并入当前正在跑的 turn，一个 result 覆盖多条提交；
2. **排队（steering）**：当前 turn 的 result 之后立刻开自己的新 turn。

`_pending_turns` 的计数模型（2026-07-18 takeover 事故防护）按"每条提交
一个 result"记账。吸收型注入 2 提交只来 1 个 result，多出的计数永久
泄漏 → ceiling 误报；worker EOF 时还会误报 `pending_turn_lost` 触发
ADR 0058 自动重放，**重放的是一条已经被回答过的消息**（可能重复执行
副作用）。该泄漏机制在 v0.14.12 之前就存在，但旧租约否决把 >30s 的
mid-turn 提交全部拒掉，暴露面极小；本 ADR 移除否决后暴露面放大。

**为何不能在提交时判别**：吸收与排队在提交时刻不可区分。曾尝试
"turn 开着就不计数"，被 takeover 回归测试
`test_mid_turn_steering_submit_survives_current_turn_result` 否决——
排队型 steering 提交必须保留计数，否则 settle 会在 steering turn
排队期间关掉 worker（正是 2026-07-18 事故）。

**为何不能只比时间**（发版门禁审查 14 维度一致否决了纯时间谓词）：

- generator 在 yield 处挂起期间，旧 result 已被预取、新提交竞入，
  恢复后旧 result 会盖上晚于新提交的时间戳（时间归因不可靠是本文件
  既有结论，计数模型正是为此而生）；
- `_last_submit_monotonic` 在异步提交 await **之前**落章，等待窗口内
  排水处理旧 result 同样形成"result 晚于提交"的假象；
- 两条都在 turn 开启前排队的提交（各自应得自己的 turn），第一条的
  result 一样"晚于"第二条的提交时刻——纯时间谓词会把真排队消息
  误判成吸收并静默丢弃。

**修复（吸收候选 + 结算点判别，含第二轮审查修订）**：提交时保持保守
计数不变；新增 `_inflight_submits`（提交 await 期间的在途计数）；
drain loop 维护候选状态并只在结算点动手：

- `pending_at_turn_open`：**非注入** turn 开启时对计数拍快照（floor）。
  只有 floor **之上**、且已被客户端确认（**不在途**）的提交才是吸收
  候选；turn 开启前排队的提交永不进入候选（两条预排队只回第一条时，
  残留必须继续告警/重放）。
- `absorbable_pending`：每个非注入 `TURN_COMPLETED` result 核销时
  **合并**（`min(残留, 旧候选 + 本回合新增非在途提交数)`）。每个新的
  非注入 turn 开启时**扣一**（`max(0, min(旧候选, 开启时标记数) - 1)`）
  ——候选没有身份，按最坏情况假定开启的 turn 消费的就是一个候选
  （round 3：按"标记数-1"封顶会让回合间新提交继承旧候选而被静默清掉；
  扣一只会多告警、不会静默丢）。**bare result**（无开场流量、只有
  result 的非注入回合）在核销点做同样的扣一且不产生新候选（终验
  对抗面板：否则 bare result 绕过唯一扣减点，旧候选照样转移）。不
  整体清零——混合去向（一条吸收一条排队）不得丢失吸收证据
  （round 2）。**任意** turn（含注入回合）以 `SESSION_ERROR` 终局时
  归零（中止的回合证明不了任何吸收，CLI 出错也削弱"排队 turn 早该
  开启"的推断，round 3）。
- `last_turn_terminal_at`：**任意** turn 终局（含注入回合）刷新。
  吸收年龄从最近一次 turn 终局起算——排队消息在任何 turn 占用 worker
  期间都无法运行（round 2：候选后插入长注入回合再 EOF 的静默丢窗口）。
  **task 生命周期消息**（task_started/updated/…）同样刷新年龄基准：
  排队 turn 可能只以任务流量可见（终验面板：仅任务流量 + EOF 曾能
  静默清掉真实在跑的提交）。进一步地，`task_started` 在**无开着
  turn**时出现＝有一个未被观察到的 turn 正在运行（task 只能由运行中
  turn 的工具调用启动），按浮出权限事件同一归因规则设置**粘性**
  `user_turn_traffic`（注入预测存活时归注入回合）——只刷新可老化的
  时钟不够，30s 后仍会被清（终验面板第三轮）。
- `last_accounted_result_at`：最近一次已核销 `TURN_COMPLETED` 的排水
  消费时刻，只作 belt-and-braces 的新旧校验，绝不单独作数。
- EOF 观测基准 `eof_observation_basis`：流等待真实阻塞后返回 → EOF
  到达时刻就是现在；等待**瞬时返回**（EOF 在 generator 因 yield 挂起
  期间已缓冲）→ 到达时刻不可知，保守取上一条消息的观测时刻
  （round 2：>30s 的慢投递挂起不得把新 EOF 洗成旧 EOF）。

两个结算点的共同条件：`absorbable_pending ≥ 残留`、
`not user_turn_traffic`、**无在途提交**、最后提交早于最近核销
result、距最近 turn 终局至少 `_ABSORBED_MIN_RESULT_AGE_SECONDS`
（30s，刻意独立于可配置的 ceiling——调短 ceiling 不得削弱数据安全
语义）。差异：

- **ceiling 到点**（等满一个 ceiling 窗口、无开着的 turn）：满足即
  静默清零、正常 settle，只记 `headless_pending_turns_absorbed`
  （含 walkcode session_id、absorbable_pending、判定时冻结的
  absorption_age_seconds / result_age_seconds / submit_age_seconds），
  不发告警。排队型 steering turn 会在 result 后数秒内开启，整个
  ceiling 窗口的沉默是强证据。
- **worker EOF**（无窗口等待，随时可能发生）：额外要求
  `not turn_open`，年龄用 EOF 观测基准计算。不满足则保留
  `pending_turn_lost` 路径（可见错误 + ADR 0058 重放决策，即 R2 之前
  行为）。判定吸收时只记 `headless_pending_turns_absorbed_at_eof`。

**已知残留窗口**（接受并记录）：

- worker 在最近 turn 终局 30 秒**内**死掉且 mid-turn 提交确实已被
  吸收：走 `pending_turn_lost` 并可能重放一条已回答的消息（重复执行、
  用户可见）——宁可如此，不可静默丢消息（Lark ingress 永不重投）。
- worker 在终局 30 秒**外**死掉且 CLI 把 mid-turn 提交排了队却始终
  没开 turn（CLI 挂死类故障）：会被判吸收而漏报——概率极低，
  observability 日志（walkcode session_id + 时距字段）留痕可查。
- drain 恢复被延迟跨越 30s 守卫的窄窗（yield 挂起、事件循环被同步
  回调停摆等——EOF 真实到达时刻在该状态下本质不可观测）且 worker 恰
  死于该窗：可能误判吸收。需要整个进程已处于病态（30s+ 停摆会同时
  打断心跳与其他会话），接受；观测日志留痕。
- 提交落在**注入回合**开着的窗口内且被其吸收：注入 result 刻意不提供
  吸收证据（takeover 语义），ceiling 侧仍会误报告警（噪音）；EOF 侧
  按 `pending_turn_lost` + `traffic_seen=False` 自动重放，可能重复
  执行——与 R2 之前行为一致，非本次回归，记入 issue 跟踪。
- `background_wait_ceiling_seconds=0`（合法配置，语义"永远等"）下
  吸收清算不可达，phantom 会一直挂住监听与 worker——与 R2 之前该配置
  下 phantom 的行为一致，非本次回归，记入 issue 跟踪。

回归测试（`tests/test_channel_native_headless_persistent_drain.py`）：
`test_absorbed_mid_turn_submit_settles_silently_at_ceiling`、
`test_absorbed_mid_turn_submit_does_not_report_pending_lost_at_eof`、
`test_absorbed_leftover_with_fresh_result_still_reports_lost_at_eof`、
`test_pre_queued_second_submit_still_alarms_at_ceiling`、
`test_pre_queued_second_submit_reports_pending_lost_at_eof`、
`test_started_steering_turn_death_reports_pending_lost_with_traffic`、
`test_submit_behind_injected_turn_result_still_alarms_at_ceiling`、
`test_inflight_submit_failure_does_not_pollute_absorption_candidates`、
`test_mixed_absorbed_and_steering_submits_settle_silently`、
`test_queued_submit_behind_injected_turn_reports_lost_at_eof`、
`test_buffered_eof_behind_slow_delivery_reports_lost`、
`test_between_turns_submit_does_not_inherit_stale_candidate`、
`test_injected_turn_session_error_revokes_absorption_candidates`、
`test_short_ceiling_does_not_bypass_absorption_age_guard`、
`test_bare_result_steering_turn_deducts_stale_candidate`、
`test_task_only_traffic_blocks_absorbed_classification_at_eof`、
`test_task_only_turn_still_alarms_at_ceiling`。
