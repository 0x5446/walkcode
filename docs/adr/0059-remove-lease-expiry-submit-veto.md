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
