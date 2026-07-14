# ADR 0052: headless worker 常驻事件泵——后台任务结果回推、活 worker 复用与真实回收

Date: 2026-07-14

Status: Accepted; supersedes ADR 0030 的 "IDLE ⇒ 下一条 inbound 必 resume" 语义
（弱化为 "IDLE + 无活泵 ⇒ resume；IDLE + 活泵 ⇒ 复用同一 worker"）

## Context

真实事故（2026-07-14，personal profile）：飞书发起的会话跑 deep-research
Workflow，Claude Code 在后台任务完成时于**原 worker 进程内自发续 turn**，
生成了完整的最终报告——但 walkcode 只在自己提交的 turn 期间 drain 事件
（`receive_response()` 到 ResultMessage 停），自发 turn 的输出没有任何
消费者，报告永远没有推回飞书话题。用户追问后，新 resume 的进程只能从
transcript 复述一份简要结论。

同一模型还叠加了三个缺陷：

1. **每 turn 一个新进程**：turn 完成 → IDLE → 下一条 inbound 在
   `_ensure_writer_ready_for_submit` 无条件 `transport.resume()` 起新
   claude 子进程（ADR 0030 的原始语义），旧 worker 里的后台任务随之
   被遗弃。
2. **worker 进程泄漏**：`ClaudeHeadlessTransport._clients` 只增不减，
   旧 client / 子进程永不回收（真机堆积 5+ 个进程）。
3. **shutdown 是 no-op**：`_call_client_control("shutdown")` 找不到真
   `ClaudeSDKClient` 的 shutdown 方法（它只有 `disconnect()`），
   external claim 的"杀旧 worker"实际从没杀过；`interrupt(reason)` 对
   无参的真 `interrupt()` 直接 TypeError。

SDK 侧事实（claude_agent_sdk 0.2.118，行为锚点，升级需 e2e 复验）：

- `receive_messages()` 迭代器跨任意多 turn 持续产出，只在 stdout EOF
  （进程死亡）时结束；`receive_response()` 在 ResultMessage 后停止。
- 内部 anyio 缓冲 100 条，满则背压（不丢消息）；**消息对并发消费者是
  分流不是复制**——每个 client 必须恰好一个流消费者。
- 已连接 client 上多次 `query()` 受支持（多 turn 同进程）。
- `disconnect()` 对子进程做有界升级 terminate → kill；SDK atexit 对
  存活子进程兜底 SIGTERM。

## Decision

**每个活的 claude_headless worker 配一个常驻事件泵（event pump）**，
turn 边界不再终结消费：

- Transport 新增 `open_event_stream(handle)`：优先 `receive_messages`
  的持久流（bridge 权限事件照旧合并浮出）；`events()` 的 per-turn 语义
  保持不变。`_active_stream_handles` 守卫强制单消费者——同 handle 二次
  打开响亮抛 `CapabilityUnsupported` 而不是静默分流。
- Orchestrator 维护 `_event_pumps: dict[session_id, _EventPumpEntry]`
  （**纯内存，绝不持久化**）；launch / resume / takeover-resume 三处
  handle 创建点之后立刻起泵。泵体复用 per-turn drain 的同一条事件管道
  （`_apply_agent_event`）：ownership fence（generation / transport 换
  即静退）、状态迁移、view → outbox → flush。
- **泵门控 = `persistent_event_stream` capability AND
  `defer_event_drain`**：serve 循环开泵；`serve --once` 与同步测试路径
  保持 per-turn drain（进程退出前必须把输出送完）。
- **复用活 worker**：`_ensure_writer_ready_for_submit` 在泵活时只
  `acquire_structured_writer`（同 transport_ref，不 resume）——下一 turn
  是同进程里的又一次 `query()`，后台任务继续存活。泵死才走 resume，且
  resume 前先 cancel 残泵 + shutdown 旧 handle（真回收）。
- **shutdown 改为真回收**：pop `_clients`/`_bridges` + `disconnect()`；
  对已消失的 worker 返回 `already_stopped`（真空成功），否则泵退出后
  close_session 会因 NOT_FOUND 永远关不掉。`interrupt` 加 TypeError
  降级到无参调用。
- **先 cancel 泵，再杀 worker**（close_session / external claim /
  serve 收尾）：cancel 路径只做注册表清理，worker 清理归 canceller；
  避免泵先看到 EOF 把主动关闭误报成 mid-turn 死亡。
- 泵内 TURN_COMPLETED 后重置文本去重游标（否则跨 turn 的相同文本会被
  误吞）并触发一次存盘（durable resume ref 是重启复活的关键资产）；
  流 EOF/异常且会话仍 in-flight 时落 ERROR_RECOVERABLE + 错误卡，与
  per-turn drain 的 incomplete-stream 语义对齐。

### 自发 turn 的交互语义

后台任务完成唤醒的自发 turn 走与前台 turn 完全相同的管道：文本推回
话题、工具进度卡、HITL 权限/提问卡都正常浮出（bridge per-client，泵
持续消费）。自发 turn 进行中 lifecycle ACTIVE 且 lease 为 None，channel
inbound 沿用现行 LEASE_EXPIRED + 飞书事件重投递机制，turn 完成后重试
进入"复用活 worker"分支。

## Consequences

- 每个活跃会话一个常驻 claude 进程（净改善：修复前每 turn 泄漏一个且
  永不回收）。**暂无 idle TTL 回收**，列为 follow-up：可由健康巡检对
  idle 超时的泵 cancel + shutdown，会话留 IDLE 依旧可 resume。
- 极端时序下，复用分支可能把用户消息 `query()` 进一个刚要自发续 turn
  的 worker：CLI 侧合并为同一会话流，事件不丢，仅两个 ResultMessage
  先后到达造成状态卡短暂抖动；其中先到的 ResultMessage 会清掉刚建立的
  writer lease（状态机没有 turn 身份，SDK 也不提供 turn id），后续
  inbound 走 LEASE_EXPIRED + 重投递自愈。接受；per-turn epoch 追踪列为
  follow-up。
- 泵从注册表摘除到 `_settle_dead_pump` 完成之间有一个短暂的"收尾窗口"，
  serve teardown 的 `stop_all_event_pumps` 看不到窗口内的泵；worker 回收
  由 settle 自身与 SDK atexit 双保险覆盖。接受。
- **能力硬门控（deep-review 后收紧）**：`persistent_event_stream` 仅在
  真实 SDK 的 `ClaudeSDKClient` 具备 `receive_messages` 时为真；
  client_factory 注入的 client 一律走 per-turn drain。持久流路径不允许
  回退到 `receive_response`（那会让泵每 turn 收尸健康 worker，比修复前
  更糟）——版本漂移时 fail closed 回旧行为。
- **回收保证的边界**：`disconnect()` 只覆盖当前 runtime 进程内注册的
  worker；断开失败或被有界取消时保留 client 引用供重试（shutdown 仍
  返回 accepted，避免卡死 close_session），runtime 崩溃遗留的孤儿进程
  由 SDK atexit SIGTERM 兜底。
- 泵是独立 asyncio task，**禁止触碰 runtime 的 `_ingress_lock`**（不可
  重入）；与旧 background drain 的并发面等价。
- 依赖 SDK 内部契约（缓冲背压、EOF 哨兵、单消费者分流）；SDK 升级时
  用 e2e gate 复验，本 ADR 记录 0.2.118 为验证锚点。
- takeover 的 blocked-input 同步 drain 在泵模式下被跳过，顺带消除了
  serve 模式下该 drain 持 ingress lock 遇 HITL 卡回调的死锁窗口。

## Verification

- 单测：`tests/test_channel_native_event_pump.py`——自发 turn 回推
  （事故回归）、IDLE+活泵不 resume、泵死后 resume 且旧 client 被
  disconnect、ACTIVE 中泵死落 ERROR_RECOVERABLE、claim 真杀进程、
  generation bump 泵静退、close_session 全链路（含 already_stopped）、
  interrupt 无 TypeError、双消费者 guard、去重重置、`--once` 无泵回归、
  重启形状、存盘点。
- 真机 e2e（personal profile）：后台任务（sleep 60 输出哨兵）完成后
  结果**自动**回推话题；同话题连发多条消息 `ps` 确认单 worker；终端
  resume 认领后 headless worker 进程消失；runtime 重启后 resume 延续
  上下文；Interrupt 无 TypeError。
