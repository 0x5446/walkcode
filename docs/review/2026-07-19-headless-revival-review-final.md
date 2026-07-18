# Deep Review 综合结论：TUI hook 会话复活（v0.14.2，PR #65）

**VERDICT**: SAFE to ship（门禁通过：无未解决 Critical；残留均为 Warning，已记录并归入 v0.14.3）
**类型**：code（8 维，codex/gpt-5.6-sol，xhigh）
**范围**：release/v0.14.2 相对 main，仅 `channel_native_runtime.py`（会话复活）+ 测试。

> 起因：2026-07-18 事故——被接管的会话在运行时重启后被 orphan 清扫标记 stopped、
> TUI 印记全被抹掉，此后飞书镜像永久断（活着的终端 hook 全被丢弃）。
> 注：本版原含"接管自重生 TUI"防护，deep-review 发现其有**正常接管回归风险**
> （先 resume 后 terminate + 裸 id 扫描会误杀刚 resume 的合法 worker），已整体
> revert（commit 6c83ed9），单独归入 v0.14.3 规划。

## 已交付（本版）

- **复活修复**（fd51502）：TUI hook 携带活进程身份即可复活被清扫的 stopped 会话，
  修"接管+重启后镜像永久断"。今早的实机症状已用手动 flip 临时恢复，本版是根因修复。
- **复活门槛硬化**（2af5e93，采纳 deep-review 首轮 Warning）：新增
  `_tui_hook_has_live_tui_process`，要求进程树条目里有**当前仍在运行**的匹配 pid
  （`LocalProcessController._pid_running`），命令字符串快照不再单独作为存活证据——
  防止过期延迟 hook 跨重启重放误复活已死会话。
- 回归测试：活 pid 复活 / 死 pid 不复活 / 无身份不复活；全量 752 绿。

## 收敛复审残留 Warning（0 Critical，记录在案，归入 v0.14.3）

| 主题（共识） | 现状与判断 |
|---|---|
| 印记支路绕过存活复验（5 维/0.99） | 门槛为 `有TUI印记 OR 有活进程`，硬化只加在后支。给前支也强制 liveness 会**回归正常 reclaim**（合法活会话但 hook 不带 pid 条目时不再复活）。今早事故会话被 sweep 清掉印记，只能走已硬化的后支，主症状已修。前支属既有行为。 |
| pid 复用误判（concurrency/security 0.99） | 死 pid 被无关进程复用 + 快照命令恰为 claude → 极窄窗口误复活。v0.14.3 用"复验活进程的真实命令"收口。 |
| 延迟 hook 积压时 ps 开销（errors 0.93） | 排空延迟队列时逐条 `ps` 可能卡 ingress。v0.14.3 改批量/缓存。 |
| 复活不清旧 HITL 交互（data/design 0.98） | 复活后旧确认卡未失效——既有行为，非本版引入。 |
| 拒绝复活后静默 complete 事件（observability 0.98） | 镜像中断缺可诊断日志。v0.14.3 补 `_log_degrade`。 |

## 判断

v0.14.2 相对 main 是明确净改进：主路径（被清扫会话 + 活 hook）复活已修且已硬化；
残留为窄边角或既有行为，且**可恢复**（误复活的会话下次真实信号或重启即自愈）。
更深的 TUI 观察/接管健壮性（印记支路、pid 复用、接管自重生 TUI）统一在 v0.14.3
先出方案再做，不在 hotfix 里连续热补进程竞态。
