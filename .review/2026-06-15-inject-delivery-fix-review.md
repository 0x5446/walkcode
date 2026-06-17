# Deep Review 综合结论 — inject 投递确认修复

**VERDICT**: NEEDS_FIX（round 1）→ 已修复（round 2，见文末「Round 2」）
**轮次**：2 / 3
**类型**：code

> 范围：walkcode 未提交改动（git diff HEAD）—— `_sweep_pending_injects` 重写、常量调整、`_register_pending_inject` 加 `last_liveness_check`、`tests/test_inject_confirm.py`
> Review engine：codex-cli 0.139.0（host: claude；engine_source: auto；provider azure / gpt-5.5-test）
> Cursor：disabled（DNS 解析 api2.cursor.sh 失败，网络不通）
> 维度：8 个 codex 并行（correctness / concurrency / errors / data / security / observability / design / tests），全部 exit=0
> Phase 2 验证：核心竞态项为 4 维度高共识且 data 维度自带复现脚本 → SKIPPED_HIGH_CONFIDENCE；单维度项由主 agent 直接源码核验（已读 tty.py:327-339 确认 is_agent_alive 行为、server.py 确认探活循环结构），未另派独立回证进程
> Repo: /Users/alpha/Documents/workspace/walkcode
> HeadSHA: 5437259067024056a27a5a584ba87c4bca5f2cdc
> RunDir: /var/folders/00/.../deep-review-walkcode-5437259-1781461723.vJAm
> 规模：135 insert / 26 delete，3 文件（含 uv.lock 自动产物）

## 🔴🔴 顶级必修

### 1. [Warning] 锁外探活回收会误删条目 → 丢失迟到确认 → 重新引入误报
`src/walkcode/server.py` · `_sweep_pending_injects`（busy-未结束分支的探活回收，约 `for p in to_probe:` 那几行）

> **一句话**：偶发的探测失败或恰好同时结束的回合，会让排队消息被当成没送达，本次想消除的误报又冒出来。

- **Category**: Concurrency / ErrorHandling
- **Confidence**: correctness 0.86, errors 0.90, concurrency 0.82, data 0.86（4 维度命中，data 附复现）
- **来源**: correctness + errors + concurrency + data（高共识）

这一处其实是**两个叠加的缺陷**，后果相同（误删 pending → 回合结束后迟到的 `UserPromptSubmit` 无条目可匹配 → 报错的 `inject_timeout` 永不纠正）：

**(a) `is_agent_alive` 单次 `False` 二义**
`tty.py:327-339`：`tmux list-panes` 在 2s 超时、非零返回、任意异常时都走 `except: pass; return False`。即"探测失败"与"会话已死"折叠成同一个 `False`。`_sweep` 只要一次 `False` 就 `remove + failed_dead`。一次 tmux 抖动就会把仍在长回合排队的消息误判死亡删除——正是本次要消除的误删路径，换了个更小概率的触发口又回来了。

**(b) 重入锁未重读 `last_stop`（Stop 竞态）**
探活在 `_pending_lock` 外执行；这期间 `/hook` 的 Stop 可能到达（`_mark_session_idle` 写 `last_stop`），把该条从"回合未结束"推进到"回合已结束、待确认 GRACE"。但重入锁时只判 `if p in _pending_injects`，没有重新读 `_session_last_stop`，于是跳过 4s grace 直接 `failed_dead` 报 timeout；紧随其后的匹配 `UserPromptSubmit` 已无条目可确认 → 确认丢失。data 维度用"探活期间调 `_mark_session_idle` 后返回 False"的脚本复现了 `pending=0` 且后续 `_confirm_pending_inject` 无法补成功。

- **Fix**:
  1. 探活三态化：区分 `alive / dead / unknown`。`is_agent_alive` 异常/超时/非零 → `unknown`（或新增一个 `probe_agent(tty) -> {alive,dead,unknown}`）。只有**确认会话/ pane 不存在**（`validate_target` 报不存在）才算 `dead`；`unknown` 保留条目、记日志、按 `last_liveness_check` 节流重试。
  2. 重入锁后**重新读 `last_stop`**：若 `last_stop > p["injected_at"]`，不要按 dead 处理——交回"回合已结束"分支按 GRACE 判定（未过 grace 保留等待迟到确认，过了才 `failed_swallowed`）。
  3. 用对象身份查找当前条目（`next((q for q in _pending_injects if q is p), None)`）而非依赖 `in` 的相等比较。
  4. 补测：mock `is_agent_alive` 在探活期间先 `_mark_session_idle` 再返回 False → 断言随后 `_confirm_pending_inject` 成功、不发 timeout。

## 🟡 中置信（建议修）

### 2. [Warning] 同一 tty 的多条 pending 各自探活 → 可被消息堆积放大成 tmux 子进程风暴
`src/walkcode/server.py` · `_sweep_pending_injects`（to_probe 循环）

> **一句话**：忙时连发很多条消息，过两分钟后每条都会单独去敲一次终端，可能拖累服务和终端。

- **Category**: Security（可用性 / DoS）
- **Confidence**: security 0.82（单维度，主 agent 源码核验为真：循环对每个条目各调一次 `is_agent_alive`）

飞书消息是外部输入。busy 会话里堆积的每条 pending，age>120s 后每 30s 各自 `is_agent_alive(p["tty"])`，每次 fork 一个 `tmux list-panes` 子进程。同一 tty 的 N 条消息 = N 倍探活。

- **Fix**: 按 `tty`（或 `session_id`）在一轮 sweep 内合并探活——同一终端只调一次 `is_agent_alive`，结果套用到该终端全部 pending；`last_liveness_check` 改为终端级节流，或一次探活后同步更新同 tty 所有条目。

### 3. [Suggestion→Warning] failed_dead 两条路径共用一条日志，超时终态缺归因
`src/walkcode/server.py` · `_sweep_pending_injects`（failed_dead 通知）

> **一句话**：报"未送达超时"时，日志看不出是终端真没了还是只是等太久触发了保底，排障容易走错方向。

- **Category**: Observability
- **Confidence**: observability 0.93（单维度，源码核验为真：leak-guard 与 liveness-dead 两路径汇入同一 `logger.warning("...session gone/stuck...")`）

- **Fix**: 入 `failed_dead` 时带原因与耗时，如 `(p, "leak_guard", age)` / `(p, "liveness_dead", age)`；终态日志输出 `reason=%s age=%.1fs tty=%s session=%s`，leak-guard 再带 `max=`。不要记录探活成功（避免每秒/30s 噪音）。

## 🟢 Suggestion（可选）

- **设计拆分（design 0.86）**：`_sweep_pending_injects` 近百行，状态机 + 锁 + 探活 + 通知 + legacy 兼容揉在一起。建议拆成"纯分流 → 锁外探活回收 → 通知"三层，`_sweep` 只编排。后续加失败原因不易让 `failed_swallowed`/`failed_dead` 规则漂移。
- **`_register_pending_inject` busy 快照与 injected_at 非原子（correctness 0.78，既有非本次新增）**：`busy` 在锁外读、`injected_at` 锁内取，Stop 夹在中间会让 `injected_at` 晚于该 Stop，清扫误判"回合未结束"。后果仅延迟报告（正常 confirm 不受影响）。建议在同一临界区内取同一时间戳并完成 busy 快照 + append。
- **失败通知先删后发（errors 0.85，既有模式）**：先 `remove` 再 `_reply`，若飞书回复异常/返回 None 则用户收不到终态且不重试。原代码即如此，非本次回归；如要根治可在通知确认成功后再清条目。
- **测试补强（tests 0.86/0.90/0.82）**：
  - 补 busy-未结束但存在**早于 injected_at 的旧 last_stop** 的用例（防把旧 Stop 误当本回合结束）。
  - 补探活节流用例：连续两次 sweep 断言 `is_agent_alive` 只被调一次。
  - 补探活期间 confirm 竞态用例（is_agent_alive side-effect 触发 `_confirm_pending_inject` 后返回 False → 只成功、不 timeout）。
  - busy-after-stop / dead / leakguard 三个失败分支统一断言完整 `self.replies` 顺序 + 失败表情来自 `_FAILURE_EMOJIS`，显式排除 swallowed/timeout 错误组合。

## 维度元信息

| 维度 | VERDICT | issues | exit |
|---|---|---|---|
| correctness | NEEDS_FIX | 2 | 0 |
| concurrency | NEEDS_FIX | 1 | 0 |
| errors | NEEDS_FIX | 2 | 0 |
| data | NEEDS_FIX | 1（含复现） | 0 |
| security | NEEDS_FIX | 1 | 0 |
| observability | NEEDS_FIX | 1 | 0 |
| design | NEEDS_FIX | 1 | 0 |
| tests | NEEDS_FIX | 3 | 0 |
| cursor-holistic | disabled | — | — |

## 总评

本次修复**方向正确**：消除了"长回合命中固定 600s 必然误报"这个主症状，迟到确认在正常路径下能补 ✅，测试也覆盖了主分支。但**没有完全达成"彻底"目标**——新引入的锁外探活回收把"固定超时误删"换成了"探活偶发误判 + Stop 竞态"两条概率更低、但同样会误删 pending 并丢失迟到确认的路径（顶级必修 #1）。要称得上彻底，建议至少修 #1（探活三态 + 重入锁重读 last_stop + 身份比较），#2/#3 一并处理成本很低。

## Round 2 — 修复与复核

针对 round 1 的三条 finding 全部修复，并在 round 2 复核中采纳了两条新的细化。测试全绿（**249 passed**，新增 14 个用例）。

**已修复 #1（顶级，锁外探活回收误删 → 丢失迟到确认）**
- 新增 `tty.probe_agent_liveness(tty) -> 'alive'|'dead'|'unknown'` 三态探活：tmux `list-panes` 超时/异常/读空 → `unknown`（不判死）；agent 进程在前台 → `alive`；pane 回落到 shell → `dead`。**解决 (a) `False` 二义**。
- `_sweep_pending_injects` 探活回收重写：非 `dead`（含 `unknown`）清零 `dead_probes` 继续等；`dead` 时重入 `_pending_lock`、用对象身份 `next(q is p)` 定位、**重读 `last_stop`**，若 `last_stop > injected_at` 交回 busy-after-stop 的 grace 路径并清零；需 **连续 `_INJECT_DEAD_PROBES`(=2) 次 dead** 才回收报 `inject_timeout`。**解决 (b) Stop 竞态 + 单次误判**。

**已修复 #2（DoS）**：探活按 tty 去重（`probe_cache`），同一会话一轮只 fork 一次 tmux。

**已修复 #3（观测）**：`failed_dead` 改为 `(entry, reason, age)`，`leak_guard` 与 `liveness_dead` 分别打日志、带 `age`（leak-guard 再带 `max`）。

**Round 2 复核新采纳**
- `errors` 维度（0.86）：`probe_agent_liveness` 原把所有 `returncode != 0` 当 dead，非零还可能是 tmux 临时故障/权限。已改为按 stderr 区分——`can't find / no server / no such / not found` → `dead`，其余非零 → `unknown`。
- `tests` 维度（0.95/0.9/0.85）：补 `tests/test_probe_liveness.py`（probe 三态映射 8 例，直接 patch `tty.subprocess.run`）+ `test_unknown_resets_dead_streak`（dead→1 再 unknown→0）+ `test_throttle_skips_probe_within_interval`（节流不重复 fork）。

**Round 2 引擎情况**：4 维度（concurrency / correctness / errors / tests）派 codex 复核；`errors`/`tests` 正常产出（见上），`concurrency`/`correctness` 两进程卡在 read-only sandbox 里反复试跑 pytest（徒劳）约 25 分钟无产出，已主动停止。这两维度在 round 1 已深入审过同一段代码并提出 #1，本轮修复正是按其 round-1 的 Fix 建议落地（重入锁重读 `last_stop` + 对象身份比较），故不再重跑。

**残留（未改，低优先）**
- `_sweep_pending_injects` 仍偏长（design 维度 Suggestion）：职责清晰、注释充分，暂不拆分。
- `_register_pending_inject` 的 busy 快照与 `injected_at` 非同一临界区、失败通知先删后发：均为**既有行为**（非本次回归），影响小，暂留。

## 原始报告
- Round 1 各维度：`/var/folders/00/.../deep-review-walkcode-5437259-1781461723.vJAm/dim-{name}.md`
- Round 2（errors/tests）：`/var/folders/00/.../deep-review-walkcode-5437259-r2-1781478703.m6GA/dim-{errors,tests}.md`
