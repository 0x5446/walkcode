# Deep Review 综合结论：ADR 0051 handoff pending-HITL 续接

**VERDICT**: NEEDS_FIX → 可行动项已全部就地修复并回归测试；两项设计权衡记入 ADR「已知并接受」
**轮次**：1 / 3（--plan-only 出报告，修复由主 agent 就地完成）
**类型**：mixed（14 维度）

> 范围：分支 worktree-adr-0051 相对 cb6a076 的增量（ADR 0051：认领卫生 + 隐形 continue 注入）
> Review engine：codex-cli 0.144.3（host: claude; engine_source: auto）
> Cursor：composer-2.5 smoke failed, skipped
> 维度：14 个 codex 并行，全部 exit=0
> Phase 2 验证：未单独派回证——全部可行动 finding 由主 agent 直接读源确认属实并修复（修复即验证），两项未修项为设计权衡而非事实争议
> Repo: /Users/alpha/workspace/walkcode/.claude/worktrees/tui-master-default
> HeadSHA: adbc1384e5dad999c20e3bb94e94def9f00d3455（审查基线；修复在其后增量 commit）
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-adr0051-adbc138-1783935366.WhKJ
> 规模：8 文件 / +442 −4（审查基线）

## 🔴 高共识（≥2 维度命中）——已全部修复

### 1. [Warning] settle 顺序错误 + shutdown 无超时（errors/security/concurrency/completeness/risk 五维命中，0.82-0.90）
> **一句话**：认领收口先无限等旧进程关闭再翻卡，旧进程卡死会把整个消息入口拖死。

- **修复**：stale 清扫与过期通知移到 shutdown 之前；shutdown 用 `asyncio.wait_for` 限 5s（`EXTERNAL_CLAIM_SHUTDOWN_TIMEOUT_SECONDS`），超时/拒绝/异常只落 degrade；顺带补查 `ControlResult.accepted`（feasibility 维度子项）。

### 2. [Warning] 注入后同步 drain 违反 defer 模式（errors/completeness/risk 三维命中，0.86-0.90）
> **一句话**：接管续接在入口锁里同步等模型输出，若续接弹出新卡，回答回调进不来会自锁。

- **修复**：复用 `submit_user_input` 策略——`defer_event_drain` 时改 `_start_background_event_drain` 立即返回；submit/drain 异常拆分为 `handoff_continue_submit_failed` / `handoff_continue_drain_failed`。

### 3. [Warning] awaiting-other 自由文本等待漏清（data/design/consistency/completeness/risk 五维命中，0.88-0.93）
> **一句话**：问答卡进入"其他"文本等待后再交接，旧等待会一直吞掉话题里的新消息。

- **修复**：新增 `InteractionStore.clear_awaiting_other_for_session`，takeover 与 claim 两个清扫路径都调用；`answer_awaiting_other` 遇 generation 不符也删除映射。补 takeover 后等待清空的回归测试。

### 4. [Warning] 认领后旧事件流可注册新 generation 幽灵卡（concurrency/completeness/risk 三维命中，0.82-0.88）
> **一句话**：交接换主后，旧进程残余输出仍可能生成看似有效的新卡片。

- **修复**：`_drain_events` 入口快照 generation/transport_kind，逐事件校验，所有权移动即停止并落 `event_drain_ownership_moved` degrade。

## 🟡 单维度——已修复

- [W 0.95, clarity] 注入文案写死「飞书端」（runtime 还有 telegram）→ 改渠道中立「聊天端」。
- [W 0.90, clarity] `hitl_stale` 渲染标题写死 "after takeover" 与认领方向混用 → 改 "after the session handoff"。
- [W 0.90, observability] 注入无成功/阶段信号 → `handoff_continue.submitted` 进度事件 + 分阶段 degrade + 日志带 takeover_id/stale_hitl_count。
- [W 0.88, tests] 降级分支缺回归 → 补 shutdown 抛错、submit 抛错两用例。
- [W 0.86, tests] 幂等键未测 → 捕获 idempotency_key 断言 `handoff_continue:` 前缀。
- [W 0.92, consistency] 「翻卡」措辞与实现（补发过期通知）不符 → ADR/部署文档改为准确表述，原地翻面记为后续项。

## 已知并接受（记入 ADR 0051，不阻塞）

- [W 0.86, feasibility+extensibility] **Codex 原生 pending 请求的认领收口缺失**：`CodexAppServerTransport` 无 `shutdown`，原生请求挂到 Codex 自身超时。fail-close decline / transport 能力抽象合并进 Codex 等价统一工作；相比之前无任何收口，非回归。
- [W 0.86, risk] **ACTIVE 中认领同样立即断老 worker**：有意语义——终端 resume 即所有权转移，不做"ACTIVE 时拒绝认领"（违背 ADR 0050 模型根设定）。

## 维度元信息

| 来源 | VERDICT | issues | 处置 |
|---|---|---|---|
| correctness | SAFE | 0 | — |
| errors | NEEDS_FIX | 2 | 已修复 |
| security | NEEDS_FIX | 1 | 已修复（同 #1）|
| concurrency | NEEDS_FIX | 2 | 已修复（#1/#4）|
| data | NEEDS_FIX | 1 | 已修复（#3）|
| observability | NEEDS_FIX | 1 | 已修复 |
| design | NEEDS_FIX | 1 | 已修复（#3）|
| tests | NEEDS_FIX | 2 | 已修复 |
| completeness | NEEDS_FIX | 4 | 已修复（#1/#2/#3/#4）|
| clarity | NEEDS_FIX | 2 | 已修复 |
| feasibility | NEEDS_FIX | 1 | ADR 已知并接受 |
| consistency | NEEDS_FIX | 2 | 1 修复（#3）/ 1 文档对齐 |
| extensibility | NEEDS_FIX | 1 | ADR 已知并接受 |
| risk | NEEDS_FIX | 4 | 3 修复 / 1 已知并接受 |

## 结论

无 Critical。多维度共识精准命中了四个真实缺陷（收口顺序、入口锁自锁、等待态残留、幽灵卡），全部修复并新增/强化 10 个测试用例，682 单测全绿。两项 transport 层设计权衡显式记入 ADR「已知并接受」。continue 注入保持默认 off，翻默认前按 ADR Verification 节做真机 Live E2E。
