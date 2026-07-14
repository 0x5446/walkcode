# Deep Review 综合结论：ADR 0052 headless 常驻事件泵

**VERDICT**: NEEDS_FIX →（整改后）已全部处置
**轮次**：1 / 3（--plan-only 出报告，整改由实现者随后应用并复测）
**类型**：mixed

> 范围：未提交工作区改动——ADR 0052（headless 常驻事件泵：后台任务结果回推、活 worker 复用、进程回收）
> Review engine：codex codex-cli 0.144.3（host: claude; engine_source: auto）
> Cursor：composer-2.5 smoke test failed, skipped
> 维度：14 个 codex 并行（code 8 + design 6），全部 exit=0
> Phase 2 验证：以"实现者逐条源码回读 + 整改 + 716 测试全绿 + 真实 SDK 冒烟复跑"替代 codex 回证（全部 finding 为 Warning，无 Critical）
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 0fea6da8bed839b13df47b34bb00d894c42eae80（工作区未提交改动）
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-0fea6da-1784044658.mvMy
> 规模：约 612 插入 / 8 文件

## 共识簇与处置

### 🔴🔴 Cluster A — shutdown 先 pop 再 disconnect（5 维度：errors 0.95 / concurrency 0.88 / security 0.86 / correctness 0.74 / feasibility 0.82）
> **一句话**：断开失败或被认领超时取消时，旧进程失去唯一可重试的引用。
- **处置**：已修。改为 disconnect 完成后才 pop `_clients`；`CancelledError` 保留注册并上抛；普通异常保留 client 供重试但仍返回 accepted（不卡死 close_session）。回归测试 `test_shutdown_failure_keeps_client_for_retry`。

### 🔴🔴 Cluster C — persistent 路径静默回退 receive_response（4 维度：extensibility 0.88 / design 0.86 / feasibility 0.86 / risk 0.86）
> **一句话**：旧版依赖缺持久流时，泵会每回合把健康进程当死进程回收，比修复前更糟。
- **处置**：已修。`persistent_event_stream` 仅在真实 SDK `ClaudeSDKClient` 具备 `receive_messages` 时为真（client_factory 注入一律 False）；持久路径去掉回退，缺能力响亮抛错。回归测试 `test_no_receive_messages_client_disables_pump`。

### 🔴 Cluster D — `_settle_dead_pump` 顺序与 handle fence（correctness 0.86 / consistency 0.87）
> **一句话**：死进程收尾窗口内消息可能打进死句柄；晚归的收尾可能误伤新进程状态。
- **处置**：已修。状态落盘（ERROR_RECOVERABLE + 清租约）改为收尾第一步、同步完成；所有 fence 增加 handle_id 比对，旧句柄只回收不改状态。

### 🔴 Cluster E — ERROR_RECOVERABLE 误复用活泵（data 0.84）
> **一句话**：刚报错的进程不该继续接新消息。
- **处置**：已修。复用分支收窄为仅 IDLE；ERROR_RECOVERABLE 走"取消残泵 + 回收旧 worker + resume 新进程"。回归测试 `test_error_recoverable_resumes_instead_of_reusing_pump`。

### 🟡 Cluster G — 空闲死亡无日志（observability 0.88）
- **处置**：已修。泵退出统一 `_log_degrade("event_pump_stream_closed", ...)` 带 session/handle/generation/lifecycle/cursor 字段。

### 🟡 Cluster H — claim 路径 cancel 泵无界等待（consistency 0.82）
- **处置**：已修。`_cancel_event_pump` 加 5s 有界等待 + 超时 degrade 日志。

### 🟡 Cluster I — 文档回收承诺过满（clarity 0.86）
- **处置**：已修。README / ARCHITECTURE 收窄口径：disconnect 只覆盖当前 runtime 进程内注册的 worker，崩溃遗留靠 SDK atexit 兜底。

### Cluster B — `_apply_agent_event` fence 单次检查 + 幂等键读可变 generation（concurrency 0.74 / data 0.88 / completeness 0.86）
- **判定**：与改动前 `_drain_events` 行为逐行等价（同粒度 fence、同幂等键来源），属存量而非本次引入。
- **处置**：顺手硬化——状态卡刷新 await 后二次 fence；幂等键改用 cursor 快照的 generation。

### Cluster F — 重叠 turn 清租约（design 0.74 / risk 0.78）
- **判定**：SDK 无 turn id，per-turn epoch 追踪成本高；LEASE_EXPIRED + 重投递可自愈。
- **处置**：接受，已写入 ADR 0052 Consequences，per-turn epoch 列为 follow-up。

### 其余单维度项
- feasibility#3（settle 窗口躲过 stop_all）：接受，已写入 ADR Consequences（settle 自身 + atexit 双保险）。
- tests#1（takeover 泵模式集成测试）：follow-up（需完整 runtime harness）。
- tests#2（自发 turn 中 inbound）：已补 `test_inbound_during_self_initiated_turn_is_lease_blocked`。
- tests#3（serve 收尾顺序）：follow-up。

## 整改后验证

- `uv run --with pytest python -m pytest tests/` → **716 passed**
- 负验证：关闭泵门控后事故回归测试确定性失败（证明测试覆盖 bug）
- 真实 SDK 现场冒烟（真 claude 进程 + 后台 sleep 任务）整改前后各跑一次，均 PASS：
  turn 结束后零输入，后台任务完成的自发 turn 输出自动到达 channel，worker 被回收

## 残留 Issues（follow-up，非阻塞）

1. idle worker TTL 回收（ADR 0052 已记录）
2. per-turn epoch 追踪（重叠 turn 租约语义）
3. takeover 泵模式集成测试、serve 收尾顺序测试
