# Deep Review 综合结论：v0.14.3 接管 pid 身份复核 + hook 哨兵

**VERDICT**: NEEDS_FIX
**轮次**：1 / 3（--plan-only，未自动 fix）
**类型**：mixed（ADR 0053 + 代码）

> 范围：fix/takeover-pid-sentinel 相对 main（commit b9f91fa，8 文件 +1035/-57）
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto; profile/model: 本机默认 gpt-5.6-sol xhigh）
> Cursor：composer-2.5 smoke 失败，跳过
> 维度：14 个 codex 并行（code 8 + design 6），全部 exit=0
> Phase 2 验证：6 条已派（多维共识 ≥0.9 免回证若干）；结果 5 VERIFIED / 1 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: b9f91faeb7bf9f59585d6b75659c57f81b007ccd
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-b9f91fa-1784431883.QIib
> plan-only：仅报告，未动文件

## 🔴🔴 顶级必修（多维共识，host 自查锚点坐实）

### A. [行为回归] 死 pid 缴械后接管降级为 manual_only
> **一句话**：终端正常退出后，飞书接管本该自动完成，现在反而变成"只能手动"。

- 共识：correctness/completeness/feasibility/consistency 4 维（0.99–1.0）；锚点自查坐实
- 问题：`_enrich_terminate_ref` 对死 pid 置 `allow_terminate=False`，而 `__init__.py:9383` 预检对未授权 ref 直接 `_complete_takeover_as_manual_only`。Ctrl+C 后的常见接管路径被本变更破坏——比修复前更糟。
- 修复：缴械与授权分离。死/不符 pid 标 `target_gone: true` 保留授权；接管预检对 `target_gone` 跳过 terminate 继续自动接管（等价 already_exited）。补全链路测试。

### B. [夺权口子] 无 captured_at 的 claim hook 仍可夺权；defer 队列 created_at 未回填
> **一句话**：升级前排队的旧事件没有时间戳，可以绕过新鲜度门槛抢走会话。

- 共识：8 维命中（errors 1.0 / design 1.0 / clarity 1.0 / risk 0.99 / consistency 0.99 / completeness 0.98 / correctness 0.99 / feasibility 1.0）
- 问题：门槛 `if hook_age is not None and ...` 对 age=None 放行；defer 队列外层有 `created_at` 却不回填 payload；测试还固化了该行为。另 concurrency 维指出：接管前 60s 内入队的 claim 也能在接管后夺权（无 acquired_at 比较）。
- 修复：排空 defer 队列时用条目 `created_at` 回填 `_walkcode_hook_captured_at`；orchestrator 持权时 age 未知 → 拒绝夺权（复活分支不动）；可加 captured_at ≥ writer_owner.acquired_at 约束。更新固化测试。

### C. [Critical, VERIFIED] `_ps_lstart_command` 探测失败与进程消失合并 → fail-open 杀 / fail-silent 缴械
> **一句话**：查进程这一步失败时，系统当成"进程没了"，可能照样杀掉被复用的进程号。

- 共识：errors(Critical 0.99)/security/design/feasibility/risk/observability/data 7 维；Phase 2 VERIFIED @ 3689-3822
- 问题：None 同时表示退出/超时/权限错/解析失败。`_ref_identity_matches` 对 None 返回 True 放行 kill；`_enrich_terminate_ref` 对 None 缴械好账本；`_pids_for_session` 异常吞成空列表→假 already_exited。
- 修复：探测改三态（`("gone",None) / ("ok",(lstart,cmd)) / ("error",exc)`）。error → 拒绝 kill + 不缴械 + degrade 日志；gone → already_exited / 缴械；sweep 失败与无匹配区分。补异常路径测试。

### D. [TOCTOU] 进程身份未贯穿 kill 状态机
> **一句话**：核对身份和真正下手之间有时间差，进程号刚好被复用就会误伤。

- 共识：correctness#3(0.98)/concurrency#2(0.96)/consistency#4(0.96)/design#1
- 问题：hook 捕获进程树只存 pid+command 不存 lstart（哨兵消费时补采的 lstart 会为同命令复用进程背书）；`_kill_one` SIGTERM→SIGKILL 前不再复核；sweep 拿到身份后只返回 pid。
- 修复：`_process_tree_entries` ps 加 lstart 一并捕获；kill 状态机每次发信号前复核 (pid,lstart,command)；sweep 返回身份三元组。

### E. [配置失效] WALKCODE_TUI_HOOK_FRESH_SECONDS 在标准部署不生效；无哨兵止损开关
> **一句话**：文档里新加的配置开关实际是摆设，线上误杀时没有即时止损手段。

- 共识：design/completeness/consistency/risk 4 维（0.99–1.0）；锚点自查坐实（`_load_native_env` 合并到局部 dict）
- 修复：fresh_seconds 进 `ChannelNativeConfig` 从合并 env 解析，运行时读 config；加哨兵开关（如 0/负值=禁用哨兵补杀，仅通知），诊断输出显示生效值。

### F. [误杀内部进程] codex app-server daemon 被判为外部 TUI
> **一句话**：Codex 的后台服务进程会被当成用户终端，哨兵可能把自家服务杀掉。

- 共识：consistency#1(0.98)/extensibility#1(0.95)；锚点自查坐实（`("codex","app-server","daemon","start")` 真实存在，分类器只认 `--stdio`）
- 修复：`_command_is_codex_app_server_process` 放宽为任意 `codex app-server` 子命令；补 daemon 形态测试。

## 🔴 高置信必修（Phase 2 VERIFIED）

### G. 哨兵在 `_ingress_lock` 内同步 terminate，可阻塞频道入口 10s+（design#4, VERIFIED @2177/3319/3465）
- 修复：锁内只做身份快照+去重，terminate 移锁外异步执行（asyncio.create_task 或缩短 controller timeout），完成后回锁写通知。

### H. 哨兵通知 dedupe_key 仅 pid，同代次 pid 复用吞第二条通知（data#3, VERIFIED）
- 修复：dedupe_key 加 lstart（`f"{pid}:{lstart}"`）。

### I. 哨兵范围与 ADR/卡片文案不符：未接管过的 orchestrator 会话也适用（clarity#1, VERIFIED）
- 决策：保留现行为（orchestrator 持权 + 外部 TUI 活动 = 冲突，与是否接管过无关），改 ADR 措辞 + 卡片文案（"该会话由飞书驱动，检测到终端进程双写"）。

## 🟡 中置信 / 架构级（本轮记录，不阻塞）

- **J. handoff 中断后清理/通知不重放**（data#1, VERIFIED）：inbound ledger in_progress 重启判重删除。属崩溃一致性架构议题，量级大，归入 backlog（v0.14.4+）。
- **K. serve 停机窗口 resume 的合法 TUI 会被哨兵补杀**（security#3 1.0）：ADR 已明示取舍。可选缓解：拒绝陈旧 claim 时若 pid 实活，记 `pending_tui_claim`，哨兵对该 pid 交还而非杀。本轮可顺带实现（约 15 行）或维持文档化取舍。

## ❌ 已驳回

- security#2 hook payload 伪造（FALSE_POSITIVE）：能写 hook stdin 的攻击者已具备本机代码执行，无新增攻击面；且终止前有身份复核。仅作记录。

## 测试补齐清单（tests 维度 4 条，随修复落地）

1. 事故链端到端：死 pid 接管成功（不再 manual_only）→ 裸终端幸存 → 活动 hook 哨兵补杀 → 通知。
2. 同命令不同 lstart 的身份测试；真实 pgrep 覆盖 `--resume` 两种 argv 形态。
3. 新鲜度 60s 边界（冻结时钟）、停止态不触发哨兵、经 defer 队列回放的时间戳回填。
4. 飞书三种冲突卡渲染 + 连续两轮接管/交还通知不被幂等吞。

## 维度元信息

| 来源 | VERDICT | issues | exit |
|---|---|---|---|
| correctness | NEEDS_FIX | 3 | 0 |
| errors | NEEDS_FIX | 3 | 0 |
| security | NEEDS_FIX | 3 | 0 |
| concurrency | NEEDS_FIX | 2 | 0 |
| data | NEEDS_FIX | 3 | 0 |
| observability | NEEDS_FIX | 1 | 0 |
| design | NEEDS_FIX | 4 | 0 |
| tests | NEEDS_FIX | 4 | 0 |
| completeness | NEEDS_FIX | 3 | 0 |
| clarity | NEEDS_FIX | 2 | 0 |
| feasibility | NEEDS_FIX | 3 | 0 |
| consistency | NEEDS_FIX | 5 | 0 |
| extensibility | NEEDS_FIX | 1 | 0 |
| risk | NEEDS_FIX | 3 | 0 |

原始产物：RunDir 下 `dim-*.md` / `verify-*.md` / `meta.txt`。
