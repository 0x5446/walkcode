# Deep Review 综合结论：headless 会话级持续监听（v0.14.0，PR #63）

**VERDICT**: SAFE（2 轮后收敛：Critical 全部修复，残留 Warning 均已修复或婉拒记录在案）
**轮次**：2 / 3
**类型**：mixed（code 8 维 + design 6 维）

> 范围：release/v0.14.0 相对 main 的全部改动（ADR 0052 会话级持续监听 + 后台任务账本 + settle/ceiling + 复用/泄漏修复）
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto）
> Cursor：unavailable, skipped（未安装/未登录）
> 维度：14 个 codex 并行（design 6 + code 8），两轮均 14/14 成功
> Phase 2 验证：round-1 派 10 条（另 6 组高共识 ≥0.9 免回证）；结果 10 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: round-1 @ 505aac2 → 修复 5f7a57f → round-2 @ 5f7a57f → 修复（本 commit）
> RunDir: /var/…/T/deep-review-walkcode-505aac2-1784272434.nY5R（round-1）、…-5f7a57f-1784274323.mlxl（round-2）
> 规模：~1190 行新增 / 8 文件（round-1 时点）

## Round-1（34 条 → 12 组，10 条回证全 VERIFIED，13 项修复 @ 5f7a57f）

| # | 发现（共识） | 处置 |
|---|---|---|
| 1 | 🔴🔴 [Critical] settle 决定后到注销间存在 await 窗口，并发提交写入将死 worker，消息丢失（completeness，回证 VERIFIED） | settle 决策点同步 `_unregister_handle`，竞态方拿 TransportUnavailable 走 resume 兜底 |
| 2 | 🔴🔴 [Critical] task_notification 先于注入回合到达 → 账本清空后按 5s grace 提前下班（risk+data，VERIFIED） | 回合间通知设有界注入等待窗（max(grace,30s)） |
| 3 | 🔴 ceiling 警告 TURN_DELTA 令流尾被判 `turn.event_stream_incomplete` → ERROR_RECOVERABLE（correctness+concurrency+consistency） | 警告后补合成空 TURN_COMPLETED |
| 4 | 🔴 EOF 时账本非空 → session.background_tasks 幽灵残留（observability+feasibility+consistency+risk） | EOF 分支清账 + 可见警告 + `_log_degrade` |
| 5 | 🔴 dict 形态 user 消息文本回流为 agent 发言（6 维度命中） | `_is_user_role_message` 支持 dict，两条转换路径共用守卫 |
| 6 | 🔴 shutdown 返回未接受 → close_session 不标 stopped（errors+completeness+tests，VERIFIED） | 幂等关闭：NOT_FOUND/CAPABILITY_DISABLED + 已断开 → 返回成功 |
| 7 | 🔴 submit 失败残留 `_last_submit_at` → 监听永不 settle（errors+consistency） | 失败还原/清除标记 |
| 8 | 🔴 流异常后坏 client 留在 `_clients` 被复用（errors+feasibility） | 流异常注销 handle 再抛出 |
| 9 | 🔴 HITL 等待无唤醒可无限挂（concurrency，VERIFIED） | pending 改 60s 有界复查 |
| 10 | 🔴 takeover 分支同步 drain 在 ingress 锁内可挂 1h（correctness，VERIFIED） | defer 模式走后台 drain |
| 11 | 🔴 events() 以 bridge 为闸，仅 receive_messages 的无桥 client 会挂死/丢后台回合（design+completeness+feasibility+extensibility，VERIFIED） | 按 receive_messages 能力选路，bridge 可空 |
| 12 | 🔴 ADR"绝不再丢消息"与 ceiling 放弃语义矛盾（clarity，VERIFIED） | ADR/.env 区分两种下班语义 |

## Round-2（24 条，1 Critical + 23 Warning，修复 @ 本 commit）

已修复：

- **[Critical] 裸终结态 task_updated 清账不设注入窗**（data 0.86）→ 注入窗泛化到"回合间任何清空账本的终结事件"
- **重启后幽灵后台任务**（7 维度命中：errors/concurrency/observability/design/completeness/consistency/risk）→ orphan sweep 清 IDLE 会话的 background_tasks（`background.abandoned_on_restart`）；close_session 同步清账
- **无 status 的 task_notification 不销账**（design/consistency/risk）→ notification 缺省视为 completed
- **HITL 答复后静默时间误计入 ceiling**（errors 0.92）→ 答复后 quiet_since 重算
- **EOF 带任务缺合成收尾、待答时 EOF 卡死 WAITING_***（concurrency 0.94 / completeness 0.95）→ EOF 补合成 TURN_COMPLETED；待答时 EOF 发可见 SESSION_ERROR
- **stopped/换代会话被旧 drain 尾事件改状态**（feasibility 0.92 / correctness 0.82 / design 0.82）→ drain 围栏加 status=stopped；runner 错误标记前检查 handle 归属
- **submit 后立刻 close → events() KeyError**（completeness 0.97）→ events() 改抛 TransportUnavailable
- **ERROR_RECOVERABLE 仍复用可疑 worker**（correctness 0.88）→ 复用仅限 IDLE
- **旧式单回合 client 被误复用**（tests 0.92）→ 复用改按 `handle_supports_reuse`（要求 receive_messages）+ 回归测试
- **shutdown 方法抛错跳过清理**（errors 0.78）→ try/finally
- **任务名未消毒进入系统警告**（security 0.82）→ `_safe_task_label`（压空白 + 截 40 字符）
- **文档缺注入窗说明**（clarity 0.92）→ ADR/.env 补充

婉拒（记录在案，不阻塞发版）：

- **extensibility 0.86**：`handle_supports_reuse` / `handle_is_live` 仍是鸭子类型，未纳入 AgentTransport 协议 / TransportCapabilities。理由：单实现阶段收益低，等第二个支持后台任务的 transport（如 codex）落地时一并抽象。
- **observability（状态卡失败的限频文本兜底）**：已补 `_log_degrade`（edit/send 两处）；文本兜底涉及限频器设计，独立小需求另行处理。

## 验证

- 全量单测：**735 通过**（新增回归 26 例，其中本次两轮补 18 例）
- 实机冒烟（真 SDK 0.2.120 + CLI 2.1.211，走 tap 代理）：**7/7 通过**，两轮修复后各复跑一次
- 已知限制：ceiling 触发即显式放弃迟到结果（有可见警告与降级日志，非静默）

## 原始产物

- Round-1 维度/回证：`/var/folders/.../deep-review-walkcode-505aac2-1784272434.nY5R/`
- Round-2 维度：`/var/folders/.../deep-review-walkcode-5f7a57f-1784274323.mlxl/`
