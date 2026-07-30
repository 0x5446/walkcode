# ADR 0060: codex 事件监听改为常驻，不再把批次边界当流末尾

Date: 2026-07-30

Status: Accepted; implemented

## Context

线上事故（2026-07-30，personal profile / 飞书 Codex bot，会话
`tui-codex-ecc321cf294e`）：用户反馈"必须不断追问才能推进，不问就没有任何
消息更新"。取证：

- `outbox.pending = 0`，`dead` 里 16 条全是 7 月 25 日之前的老账，最后一次
  成功投递是 09:49:17 —— 飞书出站链路健康，不是投不出去；
- 会话状态 `lifecycle_state = "ERROR_RECOVERABLE"`，
  `last_progress_event = "turn.event_stream_incomplete"`，
  `last_progress_at = 09:49:24`；
- codex rollout（`019f89bb-a4c2-7781-bbfa-d3868fcaabdb`）显示同一个 turn 在
  **09:50:03 正常 `task_complete`**，`duration_ms = 4113641`（68 分钟），
  最终回复是一条要用户点的授权链接；
- 也就是说：agent 干完了活，walkcode 早 39 秒就不听了，那条回复从未进入
  outbox。

根因两层：

**第一层：批次边界被当成流末尾。**
`CodexStdioAppServerClient.events()` 是有界收集器——收满 `event_timeout`
（180 秒）就返回，不管 turn 是否结束。`CodexAppServerTransport.events()`
把这一批当成整条流返回 list，`_drain_events` 的 `async for` 迭代完就结束，
此时 `open_turn=True`，于是判定"中途断流"。一个 68 分钟的 turn 因此被切了
20 多次。

**第二层：ERROR_RECOVERABLE 没有自愈。**
全仓库没有任何 sweep/watchdog 扫这个状态，唯一复活路径是
`_ensure_writer_ready_for_submit`，只在下一次用户 submit 时触发。两层叠加
的可观察行为，正是用户描述的"必须追问才推进"：追问触发 resume，积压事件
一次性排出，看起来像"问一句动一下"。

**hook 不是第二道兜底。** codex TUI 会话同时配了 hooks.json，直觉上 Stop
hook 应该兜住那条最终回复。实测不成立（codex-cli 0.144.5，同一个
CODEX_HOME、同一份 hooks.json）：

| 模式 | user 级 hooks.json | plugin hooks |
|---|---|---|
| `codex exec` | 加载，输出 `hook: SessionStart Completed` | — |
| `codex app-server --stdio` | **完全不加载**，无任何 `source="user"` 的 hook 事件 | 执行 |

walkcode 经 app-server 提交的 turn 由 app-server 进程执行，不是 TUI 进程，
所以 walkcode 自己装的 hooks 一个都不会响。对 `codex_app_server` 会话，
事件流是**唯一**通路。

对照 Claude 侧：`_bridged_event_stream` 早已是常驻 async generator，退出
条件是 settle / ceiling / EOF，而且触顶退出前会主动补一个 TURN_COMPLETED，
注释写明就是为了"不让排水把流结束读成中途失败进而打 ERROR_RECOVERABLE"。
同一个陷阱 Claude 侧识别并绕开了，codex 侧没跟上。

## Decision

### 1. 监听跨批次续听（`CodexAppServerTransport.events()`）

改为 async generator（`_iter_transport_events` 早已支持 `__aiter__`，调用方
无需改动）：turn 还开着就重新进入收集器，批次边界降级为实现细节。

结束监听只有两种：

- `turn/completed` —— 回合真结束；
- 静默到 `event_silence_ceiling`（默认 3600 秒，与 Claude 的
  `background_wait_ceiling_seconds` 对齐）。

**HITL 请求不结束监听。** 卡片 yield 出去后循环继续等：答复经
`answer_request` 走同一条线回去，agent 的后续输出就出现在这条流上。
若在此 return，唯一的消费者就没了——决定写回去了、agent 也继续跑了，
但它之后产出的一切会滞留在队列里，直到某条无关的用户消息碰巧开启新排水。
那正是本 ADR 要消灭的静默丢失。（main 上的旧实现就是 return，deep-review
的 7 个维度独立判定它是 Critical。）

ceiling 触发时按是否在等人分两种收尾：

- 等人（出过 HITL 卡片）：只发"在等你回应"的告警，**不合成
  `TURN_COMPLETED`** —— 回合确实没完成，ERROR_RECOVERABLE 是诚实状态；
- 等 agent：发告警 + 合成 `TURN_COMPLETED`，让排水读到关闭的回合而不是
  中途失败。

空批次之间设最小间隔，防止立即返回空的 client（关闭的传输、stub）把
ceiling 窗口空转成热循环。

### 2. 常驻 reader 接管读侧（`CodexStdioAppServerClient`）

原来 `events()` 和 `request()` 抢同一把锁，读侧一持就是 180 秒，于是
`turn/start` 和审批应答全排在后面——飞书发出的消息可能几分钟后才真正提交。

拆开：

- 常驻 reader task 独占读，全程运行，与有没有人在排水无关；
- `request()` 只锁写端，注册 future，由 reader 分发响应唤醒；
- `events()` 从本 thread 队列取，不碰锁，仍是有界收集（续听由上层负责）；
- reader 死亡时 `_fail_stream` 把错误重放给所有等待者，避免空队列上静默
  挂起；`_ensure_started` 发现进程活着但 reader 已死时成对重建。

附带收益：两次排水之间产生的事件进队列，不再无人接收。

**故障必须排在事件后面，且只对自己那条连接有效。** `_fail_stream` 不是直接
抛，而是往每个 thread 队列尾部放一个 `_StreamFailure` 哨兵，哨兵带连接代次
（`_connection_generation`，每次 `_start_reader()` 递增）：消费者先把真实
事件取完，再看到故障；代次对不上的哨兵直接丢弃——回合的终止事件会抢在哨兵
之前返回，把哨兵留在队列里，重连之后它会浮到下一个回合头上，让一条健康
连接上的回合无故失败。
拿着事件时收到哨兵 → 先交付这批，把错误存进 `_thread_failures`，下次
`events()` 进来第一件事就抛。两个方向都要守住：

- 直接抛会**越过**队列里已经到达的最终回复（就是要保的那条）；
- 只用一个全局 `_stream_error` 则会被下一次 `_start_reader()` 清掉——批次
  中途死亡的故障永远到不了排水层，坏掉的回合被当成"安静"，一路等到一小时
  ceiling 才合成正常结束。所以 `_start_reader()` 只清连接级
  `_stream_error`，**不清** `_thread_failures`。

### 3. 事件路由收紧

- `_notification_thread_id`：`params` 存在但为空时原实现直接 `return ""`，
  payload 分支是死代码，event_msg 形态即使带 threadId 也读不到；
- 无 threadId 的消息原按"谁先问归谁"分配。多个 TUI 会话共用一个
  app-server 进程，这会把一个会话的事件送进另一个会话的频道。改为只有
  单活跃 thread 时才认领，多 thread 时留在共享缓冲并记 degrade 日志。

"活跃"必须用 `_active_listeners`（进入 `events()` 时 +1，`finally` 时 -1），
**不能**用 `_thread_queues`。队列的生命周期比监听者长——它要存住两次排水
之间到达的事件，所以有内容时从不销毁。拿队列判活跃等于问"这个 thread 曾经
跑过吗"：只要进程先后服务过两个 thread，此后每条无 threadId 的消息都会永久
留在缓冲里没有认领者。那是又一条静默丢最终回复的路径（deep-review 7 个维度
独立命中）。改用真实监听者计数后，其他会话安静下来时缓冲里的消息可以被重新
认领；监听退出且队列已空时回收队列，避免无界增长。

### 4. Claude ceiling 告警文案

"已提交的消息在 3600 秒内没有得到任何响应"与代码不符：`pending_submits`
是"提交数减已核销 result 数"，回合完全可能流出过 delta/工具事件却等不到
result（worker 中途出问题、被判 injected turn），此时文案与用户亲眼看到的
输出直接矛盾。同一分支下方的 ABSORBED 判定专门检查 `not user_turn_traffic`，
本身就证明"有 pending 且有流量"是可达状态。

改为只陈述两件可验证的事：静默多久、没等到完成回执。秒数经
`_humanize_seconds` 渲染成人类时长。

## Consequences

- 长回合不再被误判断流，`ERROR_RECOVERABLE` 回到它本来的含义（提交失败或
  worker 真死），不再由"回合跑得久"触发；
- 放弃监听时用户一定收到告警，且回合被合成事件正常关闭；
- 飞书消息提交不再排在读侧后面；
- 多会话共用 app-server 进程时事件不再串台；
- 审批/提问答复后不再需要外部重新挂载排水，卡片往返在同一条监听里完成；
- **未修**：`ERROR_RECOVERABLE` 仍然没有自愈 watchdog。本 ADR 消除的是
  codex 侧误判这一主要来源；真死场景（worker EOF、提交失败）继续依赖下一
  次用户输入恢复，与 Claude 侧现状一致。要根治需要单独的状态清扫器。
- **已知限制（deep-review 提出，本次未修）**：
  - 无 threadId 消息的认领仍是启发式：多监听时进缓冲，等到只剩一个监听者
    再认领。归属并非确证，只是把"立刻可能错投"换成"更晚、更少可能错投"。
    根治要 turnId→threadId 映射。缓解事实：实测 0.144.5 的无 threadId 消息
    只有 `remoteControl/status/changed`、`thread/started` 这类元事件，内容
    事件（agentMessage/delta、turn/completed）都带 threadId。
  - 同一 thread 允许多个并发监听者，而队列是破坏性读取——句柄替换的窗口里
    两个排水会分走彼此的事件。main 上同样存在（两个并发 `events()` 抢同一
    条 wire），本次未收窄。
  - 流在"回合已提交、但该 thread 还没有队列"时死亡，故障无处投递，重连会
    清掉连接级错误。codex 侧缺少 Claude 那样的 `pending_turn_lost`
    （ADR 0058）机制，该回合只能等到 ceiling。
  - HITL 卡片默认 10 分钟过期，而等待上限沿用 1 小时 ceiling，卡片失效后
    监听还会空等一段时间才收尾。
  - 共享缓冲里的无归属终止事件可能越过本 thread 队列里更早的正文（缓冲与
    队列之间没有统一序号）。当前 codex 版本下不可达：无 threadId 的只有
    元事件，新旧两种事件格式不会在同一条流里混用。
  - `CodexAppServerTransport` 没有 `interrupt`，所以 ceiling 放弃监听时
    服务端回合并未真正取消。整整一小时零事件后它几乎肯定已经死了，但迟到
    事件会落在没有消费者的流上。要根治需要 app-server 侧的中断能力。
  - `asyncio.Queue` 绑定创建它的事件循环，client 因此是单事件循环资源。
    生产里 runtime 全程单循环，但跨 `asyncio.run` 复用同一个 client 会在
    第二次 `queue.get()` 抛 `RuntimeError`。当前靠约定保证，未加显式护栏。
  - `_convert_event` 返回 `None` 的未知事件仍被静默跳过，且会重置静默计时。
    协议升级新增用户可见事件时，它们会无痕丢失。

## Verification

- 单元：`tests/test_channel_native_codex.py` 新增三组——
  `CodexPersistentListenTests`（跨批次续听、静默 ceiling 告警+合成关闭、
  HITL 驻留不合成假完成、**答复后继续送达**、空批次节流）、
  `CodexEventRoutingTests`（并发双 thread 时无 threadId 不串台、
  **其他会话安静后缓冲消息仍被认领**、payload threadId 提取、events 不阻塞
  并发 request）、`CodexStreamFailureTests`（故障前已收事件优先交付、故障
  在下次调用抛出而非被重连吞掉、陈旧哨兵不拖累新连接上的回合、空手时立即
  抛、reader 死亡不泄漏 pending future）；`tests/test_channel_native_core.py` 新增 `HumanizeSecondsTests`。
  全量 991 passed。
- 真实环境：用改造后的 client + transport 驱动真实
  `codex app-server --stdio`（gpt-5.6-sol，临时 CODEX_HOME）。常驻监听运行
  期间提交新回合耗时 **0.00 秒**（拆锁前它会排在最长 180 秒的读之后），
  事件流式送达 5 条直至 `turn.completed`，监听干净退出。
- hook 对照实验：见上表，同一 CODEX_HOME 下 CLI 与 app-server 的 user
  hooks 加载行为差异。
