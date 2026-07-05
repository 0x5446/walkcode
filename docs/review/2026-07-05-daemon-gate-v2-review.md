# Deep Review 综合结论 — PreToolUse gate v2 (commit 51cb7cb)

**VERDICT**: NEEDS_FIX（0 Critical / 全部 Warning）→ 关键项已当场修复，见文末处置表
**轮次**：1 / 3（--plan-only 出报告后由 host 手工修复并复验）
**类型**：mixed（8 code + 6 design 维度）

> 范围：commit 51cb7cb — feat(v3) Feishu-side permission/AskUserQuestion loop via PreToolUse gate (ADR 0046 v2)
> Review engine：codex 0.142.5（host: claude; engine_source: auto）
> Cursor：composer-2.5 smoke test failed, skipped
> 维度：14 个 codex 并行（全部成功）
> Phase 2 验证：8 条已派（另 5 簇高共识 conf≥0.9 免回证）；结果 8 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 51cb7cb (review 时点；修复在后继 commit)
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-51cb7cb-1783218533.ObqU
> 规模：4567 行 / 13 文件 ⚠️ 超过 3000 行提示阈值

## 🔴🔴 顶级必修（多维度共识 + 回证/免验）

### 1. [Warning] 发卡"入队即成功"，card_not_delivered 降级永不触发
> **一句话**：卡片实际发送失败时终端会白等到超时被拒，而不是十秒后回落终端原生流程。

- 位置：`channel_native_runtime.py` `drain_claude_gate_requests` / `__init__.py` `post_claude_gate_prompt`
- 来源：correctness+errors+design-smell+completeness+feasibility+risk（6 维度，conf 0.88–0.92）；SKIPPED_HIGH_CONFIDENCE
- 问题：`post_claude_gate_prompt` 在 outbox 入队后即返回 True；`OutboxDispatcher` 吞掉发送异常自行重试，drain 不再感知。永久发卡失败时 hook 等满 gate 超时后 deny。
- **处置：延后**（需要 outbox 投递状态回查接口，动出站契约）。缓解：outbox 有自动重试；gate trace 日志（本轮已加）使该场景可诊断。已记入设计文档后续项。

### 2. [Warning] 存活探测语义错误：job_ready 过严 + 异常折叠为"死亡"
> **一句话**：会话启动中或探测抖动时，终端退出会把还活着的会话误标结束。

- 位置：`channel_native_runtime.py` `_claude_daemon_session_alive`
- 来源：correctness+errors+completeness+feasibility（4 维度）；回证 VERIFIED
- **处置：已修复** — 新增 `ClaudeDaemonClient.job_alive()`（alive&&present，不要求 ready；探测失败返回 None=unknown）；守卫在 unknown 时按上次观察到的 `daemon_live` 保守判断，不再误停。

### 3. [Warning] 决策写入一致性：write_decision 返回值被忽略 + 过期回调产生孤儿
> **一句话**：过期或重复的卡片点击会被记成已生效，还可能污染"始终允许"记忆。

- 位置：`claude_daemon.py` `approve_permission`/`answer_user_question`；drain 孤儿清理缺失
- 来源：data+risk+concurrency+consistency（3-4 维度）；回证 VERIFIED（hook-pid 关联面）
- **处置：已修复** — `_deliver_gate_decision` 统一守卫：pending 缺失（hook 已放弃）→ 丢弃并 trace；write-once 落败 → 不触发 `on_gate_decision`（不再污染 always_allow）；drain 增加孤儿 decision 清理（兑现文档承诺）。卡片翻面显示与实际的错位窗口缩小到"点击瞬间 hook 恰好退出"级别。

### 4. [Warning] token TTL(600s) < gate 等待窗口(1800s)
> **一句话**：审批卡发出十分钟后点击会静默失效，终端继续等到半小时超时。

- 位置：`__init__.py` `InteractionStore` / `gate_tui_hook`
- 来源：completeness+feasibility（2 维度语义配对，conf 0.86/0.90）；host 事实核对确认
- **处置：已修复** — `register_permission/register_ask_user_question` 支持 per-request `ttl`；`create_callback_token` 取 `max(默认, ctx.expires_at)`；`post_claude_gate_prompt` 按 pending `deadline` 传入。新增回归测试。

### 5. [Warning] proto 版本门禁（probe）未接入运行路径
> **一句话**：守护进程协议升级漂移时不会按设计整体降级，而是靠事后报错。

- 位置：`channel_native_runtime.py` `_build_transports` / watcher
- 来源：consistency+feasibility（2 维度，conf 0.86/0.92）；SKIPPED_HIGH_CONFIDENCE
- **处置：延后**（v1 遗留承诺；需要带缓存的 probe 接线到 watcher 与直写路径）。缓解：协议不符时各 op 自然失败并回落 takeover/hooks，安全方向正确。已记后续项。

### 6. [Warning] rid 文件名清洗/截断可碰撞
> **一句话**：不同请求编号可能映射到同一个审批文件造成串线。

- 位置：`claude_gate.py` `_safe_rid_filename`
- 来源：security+design-smell（2 维度）；回证 VERIFIED（附注：实际 rid 为 Claude 生成的 toolu_ 短 ID，偶发碰撞不可行，但防护未落实）
- **处置：已修复（防御性）** — `read_pending`/`read_decision` 读回校验 JSON 内嵌 rid 与请求 rid 一致，碰撞文件按不存在处理。

### 7. [Warning] ADR Status 头与 v2 段自相矛盾
> **一句话**：架构记录开头仍把已废弃的审批通道写成待办，误导后续维护者。

- 位置：`docs/adr/0046-*.md` Status / Consequences
- 来源：clarity+consistency（2 维度，conf 0.95）；SKIPPED_HIGH_CONFIDENCE
- **处置：已修复** — Status 与 Consequences 均改写为"v2 已落地 PreToolUse gate；permission-response 已弃用"。

## 🔴 高置信必修（单维度 + 回证 VERIFIED）

### 8. always_allow 粒度过宽（(session, tool)，对 Bash 尤甚）— correctness 0.95, VERIFIED
**处置：接受为已知取舍**（会话级、进程内、重启即忘，文档/ADR 已明示）。后续增强：规则粒度（命令前缀/路径范围）与 PermissionPolicyStore 统一。

### 9. daemon socket 无 peer/属主校验，control.key 可能发给伪造服务 — security 0.82, VERIFIED
**处置：延后**（单用户 mac 部署风险低；socket 路径与目录由 Claude 官方 daemon 创建）。后续：连接前 stat 校验 + `LOCAL_PEERCRED`。

### 10. gate spool 权限依赖 umask（pending 含 tool_input/答案） — security 0.86, VERIFIED
**处置：已修复** — gate 目录强制 0700、JSON 文件 0600（`_ensure_private_dir`/`_write_private_file`）。

### 11. hook_pid 只写不读，hook 被杀后仍发卡 — data 0.86, VERIFIED
**处置：部分缓解**（#3 的 pending 守卫使此场景不再留孤儿/污染记忆；deadline 收尾已有）。pid 存活校验作为后续增强。

### 12. gate 降级路径零日志 — observability 0.90, VERIFIED
**处置：已修复** — `claude_gate.trace()`（stderr, flush）打点：abstain_heartbeat_stale / gate_open / gate_decision / gate_timeout_deny / pass_session_not_observed / pass_card_not_delivered / auto_allow_session / reap_expired_pending / reap_orphan_decision / decision_dropped_*。

### 13. daemon_live 单向置真，daemon 无 settled 消失时状态卡假"双端同步" — design-smell 0.86, VERIFIED
**处置：部分缓解 + 延后** — #2 的 unknown 保守语义只在探测失败时依赖该标记；确认 dead（alive=False）时停止路径可正常清理。完整的 watcher 全量对账（无 job → 清标记）作为后续增强。缓解：直写失败自动回落 takeover 提示，功能不断。

## 🟡 中置信（未回证）

- **缺 gate 端到端回调链单测**（tests 0.9）：发卡→取 token→callback→decision 文件。集成冒烟已在开发中手工跑过；已列入后续补测。
- **write_decision 并发 tmp 同名**（tests 0.86）：runtime 为 asyncio 单线程，写文件为同步块不可交错；多进程时 pid 不同。风险极低，记录备查。
- **_interaction_transport 缺扩展点 / always_allow 双语义**（extensibility）：Codex 对齐时统一抽象，已在设计文档后续方向。
- **注释与设计文档仍描述旧回显行为**（clarity 0.9）：**已修复**（docstring + 设计文档写路由段）。

## ⚠️ 冲突项
无。

## ❌ 已驳回
无（Phase 2 零误报）。

## 维度元信息

| 来源 | VERDICT | issues | exit |
|---|---|---|---|
| correctness | NEEDS_FIX | 4 | 0 |
| errors | NEEDS_FIX | 3 | 0 |
| security | NEEDS_FIX | 3 | 0 |
| concurrency | NEEDS_FIX | 1 | 0 |
| data | NEEDS_FIX | 2 | 0 |
| observability | NEEDS_FIX | 1 | 0 |
| design-smell | NEEDS_FIX | 3 | 0 |
| tests | NEEDS_FIX | 2 | 0 |
| completeness | NEEDS_FIX | 3 | 0 |
| clarity | NEEDS_FIX | 2 | 0 |
| feasibility | NEEDS_FIX | 4 | 0 |
| consistency | NEEDS_FIX | 3 | 0 |
| extensibility | NEEDS_FIX | 2 | 0 |
| risk | NEEDS_FIX | 2 | 0 |
| cursor-holistic | unavailable (smoke failed) | — | — |

## 处置汇总

已当场修复（同分支后继 commit，541 测试全绿）：#2 alive 三态、#3 决策一致性 + 孤儿清理、#4 token TTL、#6 rid 读回校验、#7 ADR 矛盾、#10 spool 权限、#12 gate 日志、注释/文档回显残留。

带 Warning 发版（无 Critical，按 PR 门禁记录）：#1 outbox 投递状态回查、#5 proto 门禁接线、#8 always_allow 粒度、#9 socket peer 校验、#11 hook_pid 校验、#13 daemon_live 对账、端到端回调单测。以上均已记入 `docs/design/claude-daemon-multi-ui-sync.md` 的取舍/后续段或本报告。

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`；回证：`$RUN_DIR/verify-{name}.md`（RunDir 见报告头）
