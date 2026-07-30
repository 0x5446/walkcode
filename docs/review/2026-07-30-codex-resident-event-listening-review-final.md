# Deep Review 综合结论：codex 常驻事件监听

**VERDICT**: NEEDS_DISCUSSION（无 Critical 残留；8 条已知限制已记录并接受）
**轮次**：3 / 3
**类型**：mixed

> 范围：`fix/codex-persistent-event-stream` 相对 `main`（6 个 commit）
> Review engine：codex-cli 0.144.5（host: claude；engine_source: auto）
> Cursor：composer-2.5 smoke test 失败，skipped
> 维度：第 1 轮 14 个（8 code + 6 design，两批并行）；第 2 轮 5 个复核；
> 第 3 轮 2 个门禁（concurrency 超时未出结果，data 完成）
> Phase 2 验证：高共识项按 `HIGH_CONF_SKIP_VERIFY` 免回证（多为 ≥4 维度独立
> 命中、Confidence ≥ 0.98）；关键项由主 agent 直接读码核实
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: f247d47
> 规模：6 files / +1284 −95

## 已修复的 Critical（7 条）

### 第 1 轮（14 维度）

1. **`_thread_queues` 判活跃 → 无归属事件永久滞留**（7 维度独立命中，
   conf 0.99–1.0）。队列生命周期长于监听者，拿它判活跃等于问"这个 thread
   曾经跑过吗"。进程服务过两个 thread 后，每条无 threadId 消息都永久留在
   缓冲无认领者。→ 新增 `_active_listeners` 计数。**本分支引入的回归。**

2. **流故障绕过已到达事件 / 被重连抹掉**（4 维度，conf 0.98–0.99）。
   两个方向都坏：直接抛会越过队列里已到的最终回复；只用全局
   `_stream_error` 又会被下次 `_start_reader()` 清掉。→ `_StreamFailure`
   哨兵入队 + `_thread_failures` 携带。**本分支引入的回归。**

3. **HITL 卡片后结束监听**（7 维度，conf 0.99–1.0）。卡片 yield 后 return，
   唯一消费者消失；答复写回、agent 继续跑，产出全滞留。→ 续听不 return。
   *（main 上的既有行为，非本分支引入，但同属本次要消灭的丢消息类型。）*

### 第 2 轮（5 维度复核）

4. **陈旧故障哨兵拦住下一回合**（3 维度，conf 0.99）。turn/completed 抢在
   哨兵前返回，哨兵留在队列，重连后浮到下一回合头上。→ `_connection_generation`
   代次标记，代次不符即丢弃。**本分支引入。**

5. **`parked_on_human` 只置不清**（3 维度，conf 0.99–1.0）。答复后 agent
   恢复输出，标志仍为真；此后真正的 agent 卡死会被描述成"在等你回应"。
   → 收到任何非 HITL 事件即复位。**本分支引入。**

### 第 3 轮（门禁）

6. **待抛故障在重连之后才检查**（conf 0.99）。`_ensure_started()` 重连并
   递增代次，于是上一轮存下的错误代次"过期"、被 pop 却不抛，调用方拿到空
   批次——**直接抵消了第 2 轮的修复**。→ 检查移到 `_ensure_started()` 之前。
   **本分支引入。**

7. **取消监听吞掉已取出的事件**（conf 0.98）。`queue.get()` 是破坏性读取，
   handoff 取消排水时局部 `collected` 永久消失。→ `_thread_pushback` 按序
   存回。**本分支引入。**

## 已知限制（接受，记录在 ADR 0060）

1. 无 threadId 消息认领仍是启发式（根治需 turnId→threadId 映射）。缓解：
   实测 0.144.5 无 threadId 的只有元事件，内容事件都带 threadId。
2. 同一 thread 允许多个并发监听者抢同一队列（main 上同样存在，未收窄）。
3. 回合已提交但该 thread 尚无队列时流死亡，故障无处投递（codex 侧缺
   Claude 那样的 `pending_turn_lost` / ADR 0058 机制）。
4. HITL 卡片 10 分钟过期，等待上限沿用 1 小时 ceiling。
5. transport 无 `interrupt`，ceiling 放弃时服务端回合未真正取消。
6. client 是单事件循环资源（跨 `asyncio.run` 复用会 `RuntimeError`）。
7. `_convert_event` 返回 `None` 的未知事件被静默跳过，且会重置静默计时。
8. 共享缓冲与线程队列间无统一序号，无归属终止事件理论上可越过更早正文
   （当前 codex 版本下不可达）。
9. `ERROR_RECOVERABLE` 仍无自愈 watchdog（本次消除的是 codex 侧误判这一
   主要来源，真死场景仍依赖下次用户输入恢复，与 Claude 侧现状一致）。

## 验证

- 单元：991 passed, 24 subtests。新增 `CodexPersistentListenTests`、
  `CodexEventRoutingTests`、`CodexStreamFailureTests`、
  `CodexAbortedListenTests`、`HumanizeSecondsTests`。
- 真实环境：`codex app-server --stdio` / gpt-5.6-sol，常驻监听运行期间提交
  新回合耗时 **0.00 秒**（拆锁前排在最长 180 秒的读之后），事件流式送达
  5 条直至 `turn.completed`，监听干净退出。
- hook 对照实验：同一 CODEX_HOME 下 `codex exec` 加载 user hooks，
  `codex app-server --stdio` 只加载 plugin hooks——app-server 会话的事件流
  是唯一通路。

## 备注

第 3 轮 concurrency 维度因 10 分钟 Bash 上限被截断，未产出结果；data 维度
完成并贡献了第 6、7 两条。第 3 轮 data 另报的"共享缓冲终止事件越过队列正文"
经核实在当前 codex 版本不可达，列为已知限制 8。
