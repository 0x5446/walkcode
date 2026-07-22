# Deep Review 综合结论：pending_turns 吸收判别（PR #80, v0.14.13）

**VERDICT**: NEEDS_FIX → 三轮修复后收敛（终轮 Critical 共识已全部修复并回归）
**轮次**：3 / 3（MAX_ROUNDS）
**类型**：mixed（代码 + ADR）

> 范围：v0.14.12 移除 lease 否决后，mid-turn 吸收型提交泄漏 phantom
> `_pending_turns`，导致 1 小时 ceiling 误报与 worker EOF 误重放；本 PR
> 引入吸收候选记账与两结算点判别。
> Review engine：codex（codex-cli 0.144.5；host: claude; engine_source: auto）
> Cursor：disabled（composer-2.5 smoke failed）
> 维度：14 个 codex 并行（design 6 + code 8）× 3 轮，无维度失败
> Phase 2 验证：host 逐条源码回证（每条 Critical 均在代码上重放推演后定性）
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA（终轮审查对象）: a215457c707863a1fb50d5beec7c35d33c9e6b28（round 3 修复在其后一提交）
> RunDir: R1=/tmp…ijYf R2=/tmp…WIKI R3=/tmp…z7q7（deep-review-walkcode-*）
> plan-only：审查按 --plan-only 出报告，修复由 host 实施并逐轮重跑

## 三轮全维度 + 六轮对抗终验演进

| 轮 | HEAD | Critical 簇 | 处置 |
|---|---|---|---|
| 1 | 8e349d6 | 纯时间序谓词不成立：yield 挂起竞态、提交 await 窗口、两条预排队只回第一条、EOF 缺"排队 turn 已开启"反证 | 引入 floor + 候选 + EOF 年龄 + turn_open/user_turn_traffic 反证 |
| 2 | 07ee232 | 在途提交进候选且失败回滚不吊销；混合去向整体吊销丢证据；注入回合后年龄基准失真；EOF 观测时刻被慢投递污染；resumed 句柄日志会话标识漂移 | `_inflight_submits` 隔离；carry 合并；`last_turn_terminal_at` 年龄基准；EOF 观测基准保守化；walkcode_session_id |
| 3 | a215457 | carry 身份转移（回合间新提交继承旧候选，8 维度一致）；注入回合 SESSION_ERROR 绕过候选清零 | turn 开启一律扣一候选（最坏情况）；SESSION_ERROR 无条件吊销（含注入）；判定时距冻结入日志 |

终验（3 个独立 codex 怀疑者/轮，专攻"构造静默丢消息交错"）：

| 终验轮 | 收敛发现 | 处置 |
|---|---|---|
| 1 (dac64ea) | bare result 回合绕过候选扣减 | 核销点同样扣一 |
| 2 (7110841) | task 生命周期消息不进任何吸收时钟；事件循环停摆跨 30s | 任务活动时钟入年龄基准；停摆归入不可观测窗口文档化 |
| 3 (9f66b88) | task-only turn 的活动时钟会老化 | task_started 无开 turn 时置粘性 user_turn_traffic |
| 4 (c263759) | 注入预测认领 task_started；权限浮出亚秒归因窗 | 预测不再认领；浮出归因窗文档化（四重叠加，无可观测信号） |
| 5 (79986ed) | 非 task_started 子类型同样可为未观察 turn 开场 | 粘性推广到除 task_notification 外全部任务消息 |
| 6 (fbc93db) | 未识别消息类型（system status 等）/notification 不进时钟 | 年龄基准改用 last_msg_observed_at（任何流消息=活动）；常量 30s→300s（首 token 延迟与 worker 死亡在 API 故障窗相关） |

## 终态安全论证

吸收判别唯一的"抑制告警/重放"路径要求同时满足：候选数 ≥ 残留数、无
user_turn_traffic（含任务流量粘性证据）、无在途提交、最后提交早于
最近核销 result、距**最近一切流量**（任何已消费的流消息）≥ 300s
（EOF 侧用保守观测基准：等待瞬时返回时取上一条消息观测时刻）。候选
只在"非注入 TURN_COMPLETED 核销时按本回合已确认（非在途）mid-turn
提交数合并"时增加；在每次非注入 turn 开启/bare result 核销（各扣一，
最坏情况）与任意 SESSION_ERROR 终局（归零）时减少。判别失误的方向被
钉死为**多告警/多一次可见重放**（退化为 v0.14.12 行为），静默丢需要
同时落入下方声明窗口。

## 已声明接受的残留窗口（ADR 0059 R2 记录）

1. worker 在最近流量 300s 内死且提交确实已被吸收 → pending_turn_lost
   \+ 可能重放已回答消息（重复执行、可见）——优于静默丢。
2. 排队 turn 在最近一切流量之后整整 300s（EOF）/一个 ceiling 窗口
   （ceiling）内零可归因输出（未开场/CLI 挂死/worker 恰死于该窗）→
   漏报。需长时间完全零输出，观测日志留痕。
3. drain 恢复被延迟跨越守卫的任何形态（yield 挂起、事件循环停摆——
   EOF 真实到达时刻不可观测）且 worker 恰死于该窗。
4. 权限浮出归因窗：排队 turn 先于任何流消息通过控制通道请求权限 +
   上一回合 result 未消费 + 开场消息因 worker 猝死未到 + 300s 静默，
   四重叠加。
5. 注入回合吸收子场景：ceiling 噪音 / EOF 重放 = v0.14.12 既有行为。
6. `background_wait_ceiling_seconds=0` 下吸收清算不可达 = 该配置下
   既有 phantom 行为。

## 残留 Warning（不阻断，进 issue 跟踪）

- 混合去向的精确判别需要有序提交账本（本版为标量候选 + 最坏情况扣减，
  方向保守）；tests 维度建议的部分交错负例已补 3 项，其余依赖假 SDK
  client 阻塞点的极端交错留待账本化重构一并覆盖。
- codex transport 是否存在同类泄漏未在本 PR 范围（#75 关联）。

## 回归

`uv run python -m unittest discover -s tests -p "test_*.py"`：939 项全过
（本 PR 新增 21 项对抗回归测试，钉住三轮全维度 + 六轮终验的全部
Critical 场景）。
