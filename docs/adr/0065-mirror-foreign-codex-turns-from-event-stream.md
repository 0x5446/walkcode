# ADR 0065: 接管后把 TUI 在同一 codex 会话上的回合镜像到频道（事件流分流）

Date: 2026-09-28

Status: Proposed

## Context

ADR 0064 之后，codex 0.157 的 TUI 与 WalkCode 同是共享 app-server daemon 的
客户端：频道接管 TUI 会话时不再结束 TUI，两边在同一个 thread 上各自发起回合。
遗留的缺口是：接管后用户继续在 TUI 里发的回合，频道里看不到。

已核实的事实（2026-09-28 真机）：

- WalkCode `thread/resume` 过的 thread，TUI 发起回合的**完整事件流**会推到
  WalkCode 的连接上：`turn/started`、`item/*`（userMessage、agentMessage 及
  delta、commandExecution、fileChange…）、`turn/completed`。与 WalkCode 自己
  回合的事件同构。4/4 次对照实验稳定投递。
- 所有回合级事件都带 turn id（`params.turnId`，`turn/*` 为 `params.turn.id`）；
  会话级、账号级事件不带。
- WalkCode 只在自己的回合进行时读事件（`_drain_events` 每轮结束就退出）。两轮
  之间到达的事件留在客户端的 per-thread 队列里（无上限），下一次排水开始时按
  0064 的 turn id 过滤丢弃。
- 同一个 thread 队列不能有第二个消费者：`queue.get()` 是破坏性的，两个读者会
  瓜分事件，谁都拿不全，也可能吃掉对方的 `turn/completed` 或审批请求。
- 渲染里只有 `_send_session_view` / `_upsert_tool_progress_view` /
  `_seal_tool_progress_burst` 不碰生命周期；`_record_session_progress` 会把
  WalkCode 会话改成 ACTIVE / WAITING_*（hook 路径镜像的尝试就是这样把会话卡在
  ACTIVE 的）；`_convert_event` 会登记待答审批、缓存模型，也不能给别人的回合用。

## Decision

**一个读者，按 turn id 分流。** 不新增消费者，把 transport 变成每个已订阅
thread 的唯一读者，由它把事件分给两个去处。

### 1. transport：常驻分流（`CodexAppServerTransport`）

- WalkCode 订阅一个 thread 后（`thread/start` / `thread/resume` 成功），为它起
  一个常驻 pump task，独占 `client.events(thread_id)`，跨批次一直读，直到
  `close_session`（已有 `thread/unsubscribe`）或 `restart_backend`。
- pump 按 turn id 路由每条原始消息：
  - 属于 WalkCode 发起的回合（`started_turn()`，含已结束但晚到的）或不带 turn id
    → 放进该 thread 的本地队列；`events()` 改为从这个本地队列读，其余逻辑
    （续听、ceiling、HITL 不结束监听、`_convert_event`）不变。
  - 属于别的回合 → 交给注册的 `on_foreign_message(thread_id, raw)` 回调；没有
    回调时丢弃并记日志（即 0064 的现状）。
- 结果：0064 在排水里做的过滤上移到 pump，排水只会看到自己回合的消息；两轮之间
  到达的外来回合也有人读、能被镜像。

### 2. runtime：镜像渲染（只发卡，不改会话状态）

新增一个外来回合镜像器，按 `(thread_id, turn_id)` 聚合原始消息，直接构造视图
经 `_send_session_view` / `_upsert_tool_progress_view` 发出：

| 原始消息 | 频道里 |
|---|---|
| userMessage item 完成 | `tui_user_input`（"⌨️ 终端输入"，与 hook 镜像同款） |
| agentMessage 完成（非最终） | 叙述气泡 `turn_delta` |
| commandExecution / fileChange 等工具 item | 工具进度卡（复用 0063 的 item 映射，只取渲染部分） |
| 审批请求 | `tui_permission_notice`：提示"在终端处理"，**不**发可答卡片、**不**登记 HITL |
| `turn/completed` | `turn_completed`（最终回复） |

硬约束：不调用 `_record_session_progress`、`_convert_event`，不改
`lifecycle_state` / `writer_lease` / `writer_owner` / `generation`，不动
`last_event_seq`；幂等键用独立命名空间 `mirror:{turn_id}:{item_id|kind}`。
只对 WalkCode 持有（`writer_owner.kind == "orchestrator"`）且
`transport_kind == "codex_app_server"` 的会话生效。

### 3. hook 路径让位

共享 daemon 标记（`shared_app_server`）+ WalkCode 持有的会话上，TUI 的活动类
hook 直接忽略（事件流是这类会话唯一的镜像来源），不再落到残留哨兵分支，避免
重复与噪音。未接管的 TUI 会话（`external_tui`）仍走 hook 镜像，不变。

### 4. 重启后重新订阅

runtime 启动时，对 `status == running`、WalkCode 持有的 codex 会话执行一次
`thread/resume` 重新订阅（失败只记日志）。否则重启到下一条频道消息之间，TUI 的
回合不会被镜像。

## 不在本次范围

- **TUI 退出检测**：TUI 断开时 daemon 不推任何事件（WalkCode 仍订阅，thread
  不会被卸载），`thread/read` 也没有客户端列表。唯一办法是扫进程与 socket，
  正是 0064 要摆脱的做法。维持现状（这类会话一直显示运行中，不影响接管与对话），
  等 codex 提供客户端断开信号。
- **未接管的 daemon TUI 会话改用事件流**：WalkCode 没有订阅这些 thread，继续
  走 hook 镜像。

## 被否决的方案

- **hook 路径镜像**（复用 `_send_tui_hook_output`）：会经过
  `_record_session_progress`，实测把 WalkCode 会话卡在 ACTIVE，频道下一条消息
  可能被当成"还在忙"。
- **第二个事件读者**（空闲时另起一个 `events()` 调用）：与排水瓜分同一队列。
- **把外来回合当作 WalkCode 的回合走 `_drain_events`**：会改生命周期、登记可答
  审批，WalkCode 可能替 TUI 回答授权。

## Consequences

- 接管后 TUI 的回合完整出现在话题里，标注为终端输入；WalkCode 的回合状态不受
  影响。
- 每个已订阅 thread 多一个常驻 task；随 `close_session` / `restart_backend`
  回收。
- 排水不再直接读客户端，而是读 transport 的本地队列；0060 的续听、ceiling、
  故障哨兵语义需要在 pump 层保持（故障要能传到排水）。
