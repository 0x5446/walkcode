# 全项目清理第 1 批 — 线上隐患修复 — deep-review

- Repo: /Users/alpha/workspace/walkcode-batch1
- Branch: fix/live-hazards-batch1
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）；cursor 未登录跳过
- 维度: round 1 correctness / goalfit / maintainability / conventions / security / concurrency / data；round 2（增量）correctness / concurrency
- Phase 2: 未派独立回证进程，由宿主逐条读源码 + 旧版本对照测试核实（每条修复的测试都在修复前版本上失败）
- 门禁: 无 Critical
- VERDICT: SAFE（所有 Warning 已修复或有理由驳回）

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | 7a688f6 | deep-review-walkcode-batch1-7a688f6-1790683295.nWTc | 7 条 Warning + 1 Suggestion，security SAFE → 7a3c526 修 6 条，驳回 1 条 |
| 2 | 7a3c526 | deep-review-walkcode-batch1-7a3c526-1790685459.TEaU | 2 维共识 1 条 Warning：只取消排空任务不关管道，子进程继续写仍会积压 → 本提交关闭旧进程管道 |

## Round 1 处理

| 发现 | 处理 |
|---|---|
| R3：别的会话瞬时失败会让永久失败的会话跟着重发（correctness + goalfit 共识，已复现） | 修：`_rootless_heal_given_up` 按会话记忆，重启前不再发送 |
| R2：stderr 超过流上限的单行让 `readline()` 抛错，排空任务退出 | 修：改 `read()` 分块，单行截断 2000 字符 |
| 丢弃进程不回收排空任务，子进程占管道时每次重启泄漏一个 | 修：`_discard_process` 取消任务（round 2 补关管道） |
| 重试预算测试把阈值改成 1 仍能通过 | 修：断言前 9 次保留原位、恰好处理 10 次 |
| 设计文档 / ADR 0044 仍写旧行为 | 修：两处同步并标注被替代 |
| 依赖声明缺回归测试（Suggestion） | 修：`RuntimeDependencyTests` |
| 归档失败时删除源文件 | **驳回**：保留源文件会让队列重新被堵死，正是本批要修的问题；删除前已写 `tui_hook_archived_after_failures` 日志 |

## 验证

- 全量 1270 passed, 9 skipped（基线 1261）
- 每条修复的新测试都在修复前版本上失败（git stash 对照）
- 真机：真实 `codex app-server --stdio`（personal profile）握手后 stderr 未读字节 0、尾部 3 行；全局通知每 method 只记一次；restart 后排空任务回收、无残留进程
