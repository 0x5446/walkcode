# Deep Review 综合结论：ADR 0050 单 master UI 默认翻转

**VERDICT**: NEEDS_FIX → 文档/测试项已全部就地修复；两条 runtime Warning 均为 pre-existing，记录不阻塞
**轮次**：1 / 3（--plan-only，修复由主 agent 就地完成后未重跑整轮）
**类型**：mixed（14 维度）

> 范围：worktree 分支 worktree-tui-master-default 相对 main（ADR 0050：`WALKCODE_CLAUDE_SPAWN_MODE` 默认 daemon → headless）
> Review engine：codex-cli 0.144.3（host: claude; engine_source: auto）
> Cursor：composer-2.5 smoke failed, skipped
> 维度：14 个 codex 并行（correctness/errors/security/concurrency/data/observability/design/tests + completeness/clarity/feasibility/consistency/extensibility/risk），全部 exit=0
> Phase 2 验证：2 条已派（runtime 级）；结果 1 VERIFIED(pre-existing) / 1 UNVERIFIABLE；文档类发现由主 agent 直接读源核实，未派回证
> Repo: /Users/alpha/workspace/walkcode/.claude/worktrees/tui-master-default
> HeadSHA: 0d0ed6f6f3f9fb9cf711cb55c0dfdd66088a9c7f（审查基线；修复后另有增量 commit）
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-tui-master-default-0d0ed6f-1783928772.dJn0
> 规模：7 文件 / +133 −45（审查基线）
> --plan-only：skill 的 auto-fix 阶段未启用；下列"已修复"均为主 agent 审阅确认后手工修复并回归测试

## 🔴 高共识（≥2 维度命中）——已全部修复

### 1. [Warning] docs/lark-profile-deploy.md:247 验收清单仍把 wrapper 裸启动当 daemon-native 入口
> **一句话**：部署验收文档还按旧的双端模式写，照着验会把健康的新默认部署误判为坏。

- **来源**：correctness 0.91 + clarity 0.94 + consistency 0.95 + completeness 0.86（4 维度命中同一位置）
- **修复**：验收段改为显式 opt-in 前置条件（去掉 wrapper `WALKCODE_NO_BG=1` + 显式 `SPAWN_MODE=daemon` 或手动 `claude --bg`）。

### 2. [Warning] README.md:27 / README_EN.md:39 「wrapper 裸启动 = daemon-native」旧说法
> **一句话**：首页文档仍说终端裸启动就是双端模式，新用户会按错误预期使用。

- **来源**：correctness + consistency + risk 命中
- **修复**：改为「手动 `claude --bg` 或显式 opt-in；ADR 0050 起默认单 master UI」。

### 3. [Warning] .env.example:55-60 配置模板仍写 `daemon (default)` 并给可复制的旧值
> **一句话**：配置模板会引导新部署显式打开双端模式，绕过这次默认值回滚。

- **来源**：completeness 0.86 + risk 0.93
- **修复**：模板改写为 headless 默认 / daemon 显式 opt-in，示例值改 `headless`。

## 🟡 单维度——已修复

- [Warning, tests 0.88] describe/doctor 缺默认值断言 → 新增 `test_describe_surfaces_resolved_spawn_mode`（默认 headless + 显式 daemon 对照），并更正 `_describe_claude_daemon` 陈旧注释。
- [Suggestion, extensibility 0.8] multi-ui-sync 设计文档「已落地：daemon-native wrapper」仍写成当前态 → 标注为 ADR 0048 历史状态 + 更正「默认已切 daemon」旧说法。
- [Warning, consistency] docs/claude-tap-deploy.md:120 wrapper 注入说明按 bg 形态写 → 补 ADR 0050 默认纯 TUI 说明。

## 🟡 已记录、不阻塞（pre-existing，本 diff 未改动相关路径）

### A. [Warning, concurrency 0.86] headless 会话缺活动轮次提交门禁 — 回证 UNVERIFIABLE
`SessionRegistry.validate_submit` 只查 writer_lease，不查 ACTIVE/WAITING_*；第二条消息在上一轮 drain 未结束时会再次 `submit_turn`。回证结论：真实破坏取决于 claude_agent_sdk 对同一 client 并发 query 的排队语义，仓库内无定义与覆盖；该行为在 ADR 0048 之前 headless 长期作为默认时即如此，非本次翻转引入。**处置**：记录为独立 hardening 项（补 SDK 语义实测 + 必要时轮次门禁/队列）。

### B. [Warning, data 0.88] 旧 daemon_live 会话在 job 消亡后缺收敛路径 — 回证 VERIFIED（pre-existing）
`_sync_claude_daemon_watchers` 不对 list 中缺失的旧 daemon_live 会话做 stopped/清理收敛；startup reconcile 只覆盖有 process_ref 的 TUI 会话；此类会话 takeover 会降级 manual-only。回证确认路径存在，且默认值翻转未触碰这些代码——daemon 默认/显式 daemon 下同样存在。**处置**：已在 ADR 0050「已知并接受」记录；建议独立 PR 做 list 缺失收敛（job_alive 二次确认 → 标停 + 清 daemon_live + 保留 resume_ref）。

### C. [Warning, design 0.86] wrapper 回退不在仓库门禁内
doctor/upgrade 不扫描机器本地 wrapper 是否仍为 bg+attach 形态。**处置**：已在 ADR 0050「已知并接受」记录；当前唯一部署（本机）随本次变更同步改 wrapper。

## 维度元信息

| 来源 | VERDICT | issues | 备注 |
|---|---|---|---|
| correctness | NEEDS_FIX | 1 | 已修复 |
| errors | SAFE | 0 | — |
| security | SAFE | 0 | — |
| concurrency | NEEDS_FIX | 1 | pre-existing，记录 |
| data | NEEDS_FIX | 1 | pre-existing，记录 |
| observability | SAFE | 0 | — |
| design | NEEDS_FIX | 1 | ADR 已知并接受 |
| tests | NEEDS_FIX | 1 | 已修复 |
| completeness | NEEDS_FIX | 1 | 已修复 |
| clarity | NEEDS_FIX | 1 | 已修复 |
| feasibility | SAFE | 0 | 单 master 乒乓路径核实存在 |
| consistency | NEEDS_FIX | 1 | 已修复 |
| extensibility | SAFE | 1 (Sugg) | 已修复 |
| risk | NEEDS_FIX | 1 | 已修复 |

## 结论

无 Critical。核心代码变更（默认归一一行 + 门禁保持）14 维度均未发现问题；全部 NEEDS_FIX 集中在文档/模板/测试残留，已就地修复并全量 672 单测绿。两条 runtime Warning 经怀疑论回证确认为 pre-existing，不由本 PR 修，已分别记录处置方向。
