# ADR 0065: 接管后把 TUI 在同一 codex 会话上的回合镜像到频道（在唯一读者处按 turn id 分流）

Date: 2026-09-28

Status: Proposed（第 3 版：按 2026-09-28 两轮方案审查修订，见文末"审查记录"）

## Context

ADR 0064 之后，codex 0.157 的 TUI 与 WalkCode 同是共享 app-server daemon 的
客户端：频道接管 TUI 会话时不再结束 TUI，两边在同一个 thread 上各自发起回合。
遗留的缺口是：接管后用户继续在 TUI 里发的回合，频道里看不到。

已核实的事实（2026-09-28 真机）：

- WalkCode `thread/resume` 过的 thread，TUI 发起回合的**完整事件流**会推到
  WalkCode 的连接上（4/4 对照实验）：`turn/started`、`item/*`、
  `turn/completed`。与 WalkCode 自己回合的事件同构。
- 回合级事件都带 turn id（`params.turnId`；`turn/*` 为 `params.turn.id`；
  `thread/tokenUsage/updated` 也带）。`thread/status/changed`、`hook/*`、账号级
  事件不带。
- `turn/start` 的响应先于该回合任何事件到达（5/5 轮实测）；二者走同一条连接，
  daemon 内部并发，**不作为保证**。
- `turn/completed` 不带回复正文；回复文本在 `item/completed`（agentMessage）与
  `item/agentMessage/delta` 里。
- WalkCode 客户端已有**始终运行的唯一读者**（ADR 0060 `_reader_loop` →
  `_dispatch`），两轮之间到达的事件也经过它，按 thread 进队列。
- 同一 thread 队列不能有第二个消费者（`queue.get()` 破坏性，会瓜分事件）。
- 渲染里只有 `_send_session_view` 不碰会话状态；`_upsert_tool_progress_view` /
  `_seal_tool_progress_burst` 共用 `channel_binding` 上唯一一张进度卡；
  `_record_session_progress` 会改 lifecycle / writer_lease；`_convert_event` 会
  登记待答审批、缓存模型。

## Decision

**不新增读者，也不新增常驻读取任务。** 在已有的唯一读者（客户端 `_dispatch`）处
按 turn id 分流，外来回合交给一个只发卡、不改会话状态的镜像器。

### 1. 分流点：客户端 `_dispatch`

transport 为自己订阅的 thread 在客户端注册一个外来回合判定与去处：

- `is_foreign(thread_id, turn_id)`：turn id 非空、不是 WalkCode 发起的
  （`started_turn()` 为假），且该 thread **当前没有在途的 `turn/start`**；
- `on_foreign(thread_id, raw)`：外来回合消息的去处（见第 2 节）。

`_dispatch` 收到带 thread id 的消息时，先问 `is_foreign`：是 → 交给
`on_foreign`，不进 thread 队列；否 → 照旧进 thread 队列。其余路由（无 thread
id 消息只在单活跃监听时认领、共享缓冲、故障哨兵与连接代次、`_active_listeners`
计数）**一行不改**。

- **不带 turn id 的消息**照旧进 thread 队列，由 WalkCode 的排水处理（它本来就
  处理这些）；ADR 0064 §7 已让它们不刷新静默计时器。镜像不需要它们。
- **在途 `turn/start` 窗口**（发出请求到响应返回之间）：新出现的 turn id 无法
  立即归属，照旧进 thread 队列，由排水按 turn id 分辨（ADR 0064 的过滤）。排水
  跳过的外来消息**改为转交 `on_foreign`**，不再丢弃——同一个去处，不会丢镜像，
  也不会误把自己的事件交出去。
- 注册按 **thread** 幂等（重复 resume、新 handle 只覆盖同一登记），随
  `close_session`（`thread/unsubscribe`）与 `restart_backend` 注销。没有常驻
  task，所以没有"多个读取任务"的问题。

### 2. 镜像器：有界、按回合聚合、只发卡

runtime 为每个接管后的会话（会话 id 以 `tui-` 开头即由 TUI 同步会话接管而来、
`writer_owner.kind == "orchestrator"`、`transport_kind == "codex_app_server"`、
未停止、24 小时内有活动）维护一个**有上限**的镜像队列（每会话
最多 N 条原始消息，满了丢最旧的并记一次 degrade 日志），由一个镜像任务串行消费；
`on_foreign` 只做非阻塞入队，不能阻塞读者。

按 `(thread_id, turn_id)` 聚合，只在两个时点发卡，全部经 `_send_session_view`：

| 时点 | 频道里 |
|---|---|
| userMessage item 完成 | `tui_user_input`（"⌨️ 终端输入"，与 hook 镜像同款） |
| `turn/completed` | 一条汇总：终端执行的工具清单（按回合聚合，每行由第 3 节的 `_codex_tool_event` 生成）＋ 最终回复 |

- **最终回复** = 该回合最后一条完整的 agentMessage（`item/completed`）；更早的
  agentMessage 作为中间叙述随汇总一起发出。不使用 delta，不存在增量去重问题。
- **审批请求**：发 `tui_permission_notice`（"请到终端处理"），不发可答卡片、
  不登记 HITL、不进 `_convert_event`。
- 不使用 `_upsert_tool_progress_view` / `_seal_tool_progress_burst`：外来回合
  不碰 WalkCode 自己那张进度卡，两边不交错。
- 硬约束：不调用 `_record_session_progress`、`_convert_event`；不改
  `lifecycle_state` / `writer_lease` / `writer_owner` / `generation` /
  `last_event_seq`；幂等键命名空间 `mirror:{turn_id}:{kind}`。

### 3. 复用现有映射，不另写一套

codex item → 工具事件/摘要的映射已是无副作用的模块级函数
`_codex_tool_event(event_type, payload)`（ADR 0063 的按 schema 穷举映射，
不读写 transport 状态；`_convert_event` 的副作用在它之外）。镜像器直接调用它
生成工具清单的每一行，新增 item 类型、拒绝状态、文件摘要、未知类型日志只维护
一处。

### 4. hook 路径让位

共享 daemon 标记（`shared_app_server`）＋ WalkCode 持有的会话上，TUI 的活动类
hook 直接忽略（事件流是这类会话唯一的镜像来源），不再落到残留哨兵分支。未接管
的 TUI 会话（`external_tui`）仍走 hook 镜像，不变。

### 5. 订阅的恢复：随现有维护循环对账

runtime 的维护循环（与 TUI 绑定刷新同节奏）对账第 2 节定义的镜像对象，若当前
连接未订阅其 thread，则 `thread/resume` 订阅并注册分流；不再符合条件的会话注销
并收尾。只看接管会话、只看 24 小时内有活动的，是为了不把大量闲置会话全部加载进
daemon。失败只记日志、下一轮重试——覆盖启动、daemon 重启、连接重建，不需要单独的
"启动时一次性恢复"。

### 6. 实现规则（第二轮审查补齐）

1. **读者不能被拖垮**：`_dispatch` 里 `is_foreign` / `on_foreign` 整体包在
   try/except 里；任何异常记一次 degrade 日志，该消息按原路进 thread 队列（退回
   0064 行为），读者继续。`on_foreign` 只做 `put_nowait`。
2. **一个回合只走一条路**：在途 `turn/start` 窗口里进了 thread 队列的 turn id，
   在该连接上**粘住**这条路，后续消息也进队列、由排水按序转交 `on_foreign`；不会
   出现同一回合一半直达、一半经队列导致乱序。
3. **提交失败不滞留**：`turn/start` 失败时 transport 清掉在途标记，并把该 thread
   队列里已判为外来的消息立即转交 `on_foreign`（此刻没有排水在读，不存在第二个
   消费者）。
4. **按连接代次登记与对账**：分流登记记下客户端 `_connection_generation`；维护
   循环发现代次变化（重连、daemon 重启）或未登记，就重新 `thread/resume` 并登记。
   断开到重新订阅之间发生的 TUI 回合不镜像（对话本身不受影响），接受并在日志里
   记一次。
5. **故障收尾**：代次变化时，该连接上尚未结束的外来回合立即以"终端回合因连接
   中断未同步完"收尾发出，不留悬挂状态。
6. **聚合有上界**：每会话同时最多 4 个未结束的外来回合（超出则最旧的提前收尾）；
   每回合只保留最近 50 行工具记录与最后一条完整回复（超长截断并注明）；外来回合
   1 小时无新事件即按"未结束"收尾。原始消息队列每会话上限 500 条。
7. **丢了要说**：队列满时丢最旧的消息，并按所属回合计数；该回合的汇总里固定加
   一行"⚠️ 本回合有 N 条终端事件因过多未同步"（N 为实际丢弃数），同时记 degrade
   日志。整回合的消息都被丢掉时，按规则 6 的超时以"未结束"收尾，汇总同样带这行。
8. **渲染契约**（不新增视图类型，文本即契约）：
   - 终端输入：现有 `tui_user_input` 视图，`input` = userMessage 的文本（与 hook
     镜像完全同款，频道里显示"⌨️ 终端输入"）。
   - 回合汇总：现有 `turn_completed` 视图，`message` 为下列各段按序、以空行分隔
     （空段省略）：
     1. 标题行 `⌨️ 终端回合`（收尾原因非正常结束时追加：`（未结束）` /
        `（连接中断，后续未同步）`）；
     2. `执行了：` ＋ 最多 20 行 `• <摘要>`，摘要取 `_codex_tool_event` 返回事件的
        payload 摘要（无摘要用工具名），超出写 `…另有 K 项`；
     3. 中间叙述：最后一条之前的 agentMessage，最多 3 条、每条截断到 300 字；
     4. 最终回复：最后一条 agentMessage，截断到 3500 字（超出注明已截断）；
     5. 规则 7 的丢失提示行（有丢失时）。
9. **审批提示逐条**：幂等键为 `mirror:{turn_id}:approval:{request_id}`，同一回合
   多次审批各发一条"请到终端处理"。
10. **未知类型照样告警**：镜像遇到 `_codex_tool_event` 不认识的 item 类型，调用
    transport 的 `_log_unhandled_event_type`（与排水共用同一个"每类型一次"集合）。
11. **可单独关闭**：`WALKCODE_CODEX_MIRROR=off` 时不登记分流，行为即 ADR 0064
    （外来回合由排水跳过、只记日志）。默认开启。

## 不在本次范围

- **TUI 退出检测**：TUI 断开时 daemon 不推任何事件（WalkCode 仍订阅，thread
  不会被卸载），`thread/read` 也没有客户端列表。唯一办法是扫进程与 socket，
  正是 0064 要摆脱的做法。维持现状（这类会话一直显示运行中，不影响接管与对话），
  等 codex 提供客户端断开信号。
- **未接管的 daemon TUI 会话改用事件流**：WalkCode 没有订阅这些 thread，继续
  走 hook 镜像。
- **外来回合的实时进度卡**：只在回合结束时发汇总，不做逐条实时更新。

## 被否决的方案

- **transport 常驻读取任务 + 本地队列**（本 ADR 第 1 版）：引入第二层队列、
  读取任务生命周期（重复 resume 会多起、故障要跨层传递、监听计数长期 >1 导致无
  thread id 消息无人认领）、无上限积压——审查 6 条确认问题里 4 条出自这一层。
- **hook 路径镜像**（复用 `_send_tui_hook_output`）：经过
  `_record_session_progress`，实测把 WalkCode 会话卡在 ACTIVE。
- **第二个事件读者**：与排水瓜分同一队列。
- **把外来回合当作 WalkCode 的回合走 `_drain_events`**：改生命周期、登记可答
  审批，WalkCode 可能替 TUI 回答授权。

## Consequences

- 接管后 TUI 的回合出现在话题里：终端输入一条、回合结束时一条汇总（工具清单＋
  最终回复）；WalkCode 回合的排水、状态与进度卡完全不受影响。
- 读者只多一次判定和一次非阻塞入队；每个接管会话多一个有界队列与一个镜像任务，
  随会话结束回收。
- 镜像队列溢出时丢最旧消息并记日志，汇总可能不完整，但不会拖垮读者或内存。

## 审查记录

2026-09-28 第 1 版方案审查（codex personal / gpt-5.6-sol，goalfit + consistency
+ feasibility，Phase 2 回证）：16 条 Warning 去重为 9 组，核实成立 6 组（队列
无上限、重启只恢复一次、故障传递未设计、重复 resume 多起读取任务、进度卡互相
串、最终回复来源未定义），1 组以实验核实未复现但仍加防护（`turn/start` 在途
窗口），2 组因额度未回证、由作者读码确认并处理（无 turn id 消息滞留、映射分叉——后者核实时发现 `_codex_tool_event` 已是纯函数，直接复用即可）。
第 2 版改为在唯一读者处分流。

2026-09-28 第 2 版方案审查（同引擎，三维度 + Phase 2 回证）：第 1 版问题 A–I
核对为已解决；新增 15 条 Warning 去重为 10 组，均为实现规则未写明，第 3 版以
第 6 节逐条补齐。
