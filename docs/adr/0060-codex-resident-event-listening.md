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

结束监听只有三种：

- `turn/completed` —— 回合真结束；
- HITL 请求 —— 回合被人挡住，**保持 open**（它确实没完成），答复经新的
  排水回来；
- 静默到 `event_silence_ceiling`（默认 3600 秒，与 Claude 的
  `background_wait_ceiling_seconds` 对齐）—— 先发告警，再补一个合成
  `TURN_COMPLETED`，让排水读到关闭的回合而不是中途失败。

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

### 3. 事件路由收紧

- `_notification_thread_id`：`params` 存在但为空时原实现直接 `return ""`，
  payload 分支是死代码，event_msg 形态即使带 threadId 也读不到；
- 无 threadId 的消息原按"谁先问归谁"分配。多个 TUI 会话共用一个
  app-server 进程，这会把一个会话的事件送进另一个会话的频道。改为只有
  单活跃 thread 时才认领，多 thread 时留在共享缓冲并记 degrade 日志。

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
- **未修**：`ERROR_RECOVERABLE` 仍然没有自愈 watchdog。本 ADR 消除的是
  codex 侧误判这一主要来源；真死场景（worker EOF、提交失败）继续依赖下一
  次用户输入恢复，与 Claude 侧现状一致。要根治需要单独的状态清扫器。

## Verification

- 单元：`tests/test_channel_native_codex.py` 新增
  `CodexPersistentListenTests`（跨批次续听、静默 ceiling 告警+合成关闭、
  HITL 驻留不合成、空批次节流）与 `CodexEventRoutingTests`（无 threadId 不
  串台、单 thread 仍可认领、payload threadId 提取、events 不阻塞并发
  request）；`tests/test_channel_native_core.py` 新增
  `HumanizeSecondsTests`。全量 982 passed。
- 真实环境：用改造后的 client 驱动真实 `codex app-server --stdio`
  （gpt-5.6-sol，临时 CODEX_HOME），thread/start 2.6 秒返回，turn 提交后
  单批收到 29 个事件直至 `turn/completed`。
- hook 对照实验：见上表，同一 CODEX_HOME 下 CLI 与 app-server 的 user
  hooks 加载行为差异。
