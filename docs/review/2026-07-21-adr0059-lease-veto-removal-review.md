# Deep Review 综合结论

**VERDICT**: NEEDS_FIX → 采纳项已同版修复（见文末「R1 处置结果」）
**轮次**：1 / 3（--plan-only 出报告后，经人工复核逐项裁定再修复）
**类型**：mixed（代码 + ADR）

> 范围：工作区未提交改动——ADR 0059 移除 validate_submit 的写者租约过期否决（修复飞书 mid-turn 消息静默丢失事故）
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto）
> Cursor：composer-2.5 smoke test 失败，跳过
> 维度：14 个 codex 并行（correctness / errors / security / concurrency / data / observability / design-smell / tests / completeness / clarity / feasibility / consistency / extensibility / risk），全部成功
> Phase 2 验证：5 条已派（另 5 簇高共识高自信免回证）；结果 5 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 400b8de5768c801cf56b6538d8cf5e32375f0162（基线 commit；审查对象为其上的未提交 diff）
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-400b8de-1784640775.Anaz
> 规模：+69/-40 行 / 9 文件
> --plan-only：只出报告，不动文件

核心结论：**方向正确**——security 与 concurrency 两个维度确认删除租约否决没有绕过授权、没有引入新的双写路径（现有入口锁、代际围栏、归属检查、transport resume 锁覆盖单进程部署形态）。但暴露/遗留 8 个 Warning，其中 3 个直接关系到"消息不再静默丢失"这个修复目标本身是否达成。

## 🔴🔴 顶级必修（多维度共识）

### 1. [Warning] src/walkcode/channel_native/__init__.py:8796-8802 (Symbol: submit_user_input)
> **一句话**：旧工作进程死掉且自动恢复也失败时，追加消息依然会被静默丢弃，用户收不到任何提示。

- **Category**: ErrorHandling / DataIntegrity
- **来源**: errors + data + observability + completeness + consistency（5 维度命中）
- **Confidence**: 0.98-0.99；Verification: SKIPPED_HIGH_CONFIDENCE
- **问题**: `except TransportUnavailable:` 分支里 `retry = await self._resume_writer_for_submit(...); if not retry.accepted: raise` —— 恢复失败时丢弃结构化拒因（`resume_failed` / `missing_resume_ref`）裸抛原异常。异常越过 `process_lark_event` 的 `_LARK_REJECTION_NOTES` 提示分支，飞书 WS 只记日志、不重投。移除租约否决后，mid-turn 消息成为这条路径的常客——修复目标（不静默丢消息）在该分支未达成。同时 `_resume_writer_for_submit` 的 `except Exception: return "resume_failed"` 吞掉真实恢复异常，无结构化日志。
- **修复**: 恢复被拒时执行既有回滚 + `ERROR_RECOVERABLE` + 刷状态卡后 `return retry`（复用现成的 `resume_failed` 提示文案）；为 `missing_resume_ref` 补一条提示；`_resume_writer_for_submit` 里 `_log_degrade` 记录真实异常；补飞书回归测试（首发抛 `TransportUnavailable`、恢复失败 → 应发"请重发"提示）。

### 2. [Warning] docs/channel-native-local-deploy.md:560-570 等 (Symbol: 运维门禁文档)
> **一句话**：部署手册和两份旧决策记录仍把正常的租约过期当故障，值班会按错误规则停掉消息消费。

- **Category**: Consistency / Observability
- **来源**: observability + completeness + clarity + consistency（4 维度命中）
- **Confidence**: 0.99-1.0；Verification: SKIPPED_HIGH_CONFIDENCE
- **问题**: `docs/channel-native-local-deploy.md:560-570` 仍要求 `sessions.expired_writer_leases: 0`、非零禁止 `serve --once`；ADR 0030 正文 44-45、68-69 行仍写 "Active or waiting sessions still require a non-expired writer lease"（只改了 Status 行）；ADR 0029:74-99、docs/design/channel-native-v3-implementation.md:58、1670、1697-1699 也保留旧门禁。文档与代码行为相反。
- **修复**: 部署手册删除零值门禁、改为观测项说明；ADR 0030 正文对应条目逐项改写或标注；ADR 0029 标注被 0059 部分推翻；实现设计文档同步。

### 3. [Warning] src/walkcode/channel_native/__init__.py:1807-1817 (Symbol: validate_submit → CodexAppServerTransport)
> **一句话**：放行没区分代理类型，代码代理长任务中途发消息会被当成新任务开跑或直接丢失。

- **Category**: Feasibility / Concurrency
- **来源**: feasibility + risk（2 维度命中）
- **Confidence**: 0.90-0.99；Verification: SKIPPED_HIGH_CONFIDENCE
- **问题**: 事故证据只验证了 `claude_headless` 支持 mid-turn 注入（`client.submit` 进流）。但删除否决对所有 transport 生效：`CodexAppServerTransport` 声明 `resume_active_turn=False`，其 `submit_turn` 固定调 `turn/start`——ACTIVE 会话超过 TTL 后的追加消息会误开新 turn（协议应为 `turn/steer`），被拒则包装成 `TransportUnavailable` → resume → 重试同一个 `turn/start`，最终异常上抛 + 飞书不重投。
- **修复**: 把"mid-turn 可注入"建为显式 transport capability；Codex 路径要么实现 `turn/steer`（记录 `turnId`），要么在 ACTIVE 时终局拒绝并回提示。短期最小修：仅对具备该 capability 的 transport 放行 ACTIVE 提交，其余按原语义拒绝但必须回用户提示。

## 🔴 高置信必修（单维度 + 回证 VERIFIED）

### 4. [Warning] src/walkcode/channel_native_runtime.py:4438 (Symbol: _summarize_submit_gate)
> **一句话**：冷启动诊断说消息能提交，实际消费时会先清扫旧会话，消息可能被确认却没送达。

- **来源**: correctness 0.98；回证 VERIFIED @ 4438
- **问题**: `diagnose_telegram_ingress` 在新进程直接对持久化 ACTIVE 会话调 `validate_submit` → 现在报 `submit_would_accept=True`；真实 `serve --once` 会先 `_settle_orphan_headless_sessions_once()` 清扫为 STOPPED，无 `agent_session_id` 时不可复活 → `SESSION_STOPPED` 确认 offset，消息不送达。诊断与消费行为不一致。
- **修复**: `_summarize_submit_gate` 对上一进程遗留的 ACTIVE headless 会话按"清扫后语义"预判：有耐久 resume ref 且 transport 支持恢复才报可提交，否则报 `missing_resume_ref` / `SESSION_STOPPED`。

### 5. [Warning] tests/test_channel_native_core.py:636-682 (Symbol: test_generation_gates_submits_but_lease_expiry_does_not)
> **一句话**：新测试没有复现真实事故的完整链路，同类问题再回归时测试仍然全绿。

- **来源**: tests 0.99；回证 VERIFIED
- **问题**: 测试直调 `submit_user_input` + 宽松 fake transport（任何 handle 都收）；未保持首回合 open、未走 `process_lark_event`（表情回执/台账/提示）、未覆盖死 handle 的 `TransportUnavailable → resume` 成败分支。
- **修复**: 增加飞书端到端回归：受控流式 transport 保持首回合未结束 → 时钟越过 TTL → `process_lark_event` 发第二条 → 断言仅提交一次、有 `reactMessage`、水位推进、台账完成；再覆盖死 handle 分支。

### 6. [Warning] src/walkcode/channel_native/__init__.py:6019-6046 (Symbol: _capture_worker_proc)
> **一句话**：记录旧进程身份失败时，恢复屏障会漏等旧进程死掉，可能出现两个进程同时写一个会话。

- **来源**: feasibility 0.96；回证 VERIFIED @ 6020-6046, 6452, 6618, 6789-6795
- **问题**: 拿不到 pid / 探测失败时不登记 `_session_last_worker`（`if not pid: return`）；settle 路径先 `_unregister_handle` 再异步断开。并发 resume 此时看不到旧句柄，直接创建新进程——ADR 0059 依赖的"原子屏障"在该路径失守。属存量缺陷，但本次改动加重了对该屏障的依赖。
- **修复**: 捕获失败也登记会话的最近 worker 与未知状态；settle 清理与 resume 共用会话锁；无法确认退出时返回 `TransportUnavailable` 拒绝拉新。

### 7. [Warning] src/walkcode/channel_native/__init__.py:1807-1816 (Symbol: validate_submit — 跨进程前提)
> **一句话**：两个服务误配同一份状态文件时可以同时写一个会话，现在没有任何机制阻止。

- **来源**: risk 0.94；回证 VERIFIED @ 1797-1817, 8465-8528
- **问题**: "单进程"是安全前提但未被强制：`_ingress_lock` / `_session_locks` 都是进程内的，状态保存仅 `os.replace`，启动无跨进程独占锁。ADR 0059 已把它列为残留风险，reviewer 认为应该硬化。属存量风险，本次改动移除了（名义上的）最后一道跨进程檐子。
- **修复**: 启动时按规范化 `WALKCODE_STATE_PATH` 取独占 flock 并持有到退出，冲突则拒绝启动；补双进程竞争测试。

### 8. [Warning] docs/adr/0059-remove-lease-expiry-submit-veto.md:27-63 (Symbol: Decision/Consequences)
> **一句话**：决策记录缺备选方案和安全回滚章节，出事时不能简单回退——回退会重新引入丢消息。

- **来源**: completeness 0.97；回证 VERIFIED @ 51-63
- **问题**: 无"备选方案"（续租心跳 / 主动判活 / 可靠重投的取舍）；无回滚章节——`git revert` 会恢复静默丢消息，不是可用回滚，需说明触发条件与配套（先有续租或可见可重试的拒绝，才能恢复过期检查）。
- **修复**: ADR 补两节：Alternatives considered、Rollback（触发条件 + 代码与文档同步回退范围 + 前置条件）。

## 🟡 建议级（多维度共识，Suggestion）

### 9. docs/adr/0059:33-45 两处表述
- **heartbeat_at 语义写错**（design-smell + clarity + consistency，3 维度）：ADR 称其为"最后一次提交时刻"，实际只在取得写者时赋值，中途提交不刷新。改为"最后一次取得/重取写权的时刻"，最近输入看 `last_user_input_at`。
- **"单进程 asyncio 天然串行"过宽**（clarity + extensibility，2 维度）：真实串行来自 `_ingress_lock`；`_replay_lost_turn` 在锁外调 `submit_user_input`；`_session_lock` 只属于 `ClaudeHeadlessTransport`。按入口和传输类型重写该节；未来新增并发入口需走统一串行层或按会话加锁。

## ❌ 已驳回

无（0 FALSE_POSITIVE）。

## 维度元信息

| 来源 | VERDICT | issues | exit |
|---|---|---|---|
| dim-correctness | NEEDS_FIX | 1 | 0 |
| dim-errors | NEEDS_FIX | 1 | 0 |
| dim-security | SAFE | 0 | 0 |
| dim-concurrency | SAFE | 0 | 0 |
| dim-data | NEEDS_FIX | 1 | 0 |
| dim-observability | NEEDS_FIX | 2 | 0 |
| dim-design-smell | SAFE | 1 (Sug) | 0 |
| dim-tests | NEEDS_FIX | 1 | 0 |
| dim-completeness | NEEDS_FIX | 3 | 0 |
| dim-clarity | NEEDS_FIX | 3 | 0 |
| dim-feasibility | NEEDS_FIX | 2 | 0 |
| dim-consistency | NEEDS_FIX | 3 | 0 |
| dim-extensibility | SAFE | 1 (Sug) | 0 |
| dim-risk | NEEDS_FIX | 2 | 0 |
| cursor-holistic | (unavailable) | — | smoke failed |

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`
- 各回证：`$RUN_DIR/verify-{1..5}.md`
- 元信息：`$RUN_DIR/meta.txt`（RunDir 见报告头）

## R1 处置结果（人工复核后逐项裁定）

人工复核修正了两处定性：#1 的裸 `raise` 是存量代码（改动前该场景走
LEASE_EXPIRED 连状态卡都不动，新行为已严格更好），"修复目标未达成"
言重；#3 的 codex `turn/start` 在改动前 30 秒租约窗口内同样发生，属
存量行为、窗口被本改动扩大。

| # | 裁定 | 动作 |
|---|---|---|
| 1 resume 失败静默丢 | 采纳（收窄） | 已修：`return retry` + `writer_resume_failed` degrade 日志 + `missing_resume_ref` 提示文案；测试 ×3 |
| 2 文档门禁未同步 | 采纳 | 已修：deploy 手册、ADR 0029/0030 正文、设计文档 4 处 |
| 3 codex turn/steer | 存量，独立行为变更 | Issue #75；ADR 0059 残留风险已记 |
| 4 诊断与消费不一致 | 存量（方向翻转） | Issue #78 |
| 5 测试未钉事故链路 | 部分采纳 | 已补：死 handle 两分支（resume_failed / missing_resume_ref）+ lark note 断言 |
| 6 resume 屏障缺口 | 存量 | Issue #76 |
| 7 跨进程 flock | 存量 | Issue #77 |
| 8 ADR 缺备选/回滚 | 采纳 | 已修：Alternatives considered + Rollback 章节 |
| 9 ADR 措辞两处 | 采纳 | 已修：heartbeat_at 语义、串行来源改为 `_ingress_lock` 并注明 `_replay_lost_turn` 锁外守卫 |

修复后全量测试：919 全绿（新增 3 个）。
