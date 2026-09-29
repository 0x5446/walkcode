# ADR 0067: 同一个 TUI 进程切到别的会话时，收尾旧会话

Date: 2026-09-29

Status: Accepted

## Context

ADR 0066 让 TUI 退出后 30 秒内收尾话题，判据是"记录的进程没了"。但在同一个
Claude 进程里 `/clear` 或 `/resume` 到别的会话时，进程还在，只是换了会话：旧话题
的进程记录（pid + 启动时间）与正在跑的新会话完全相同，于是一直"运行中"，带着
接管按钮。

这不只是状态不准。接管 external_tui 会话会先结束记录里的 TUI 进程，再在后台恢复
旧会话——在旧话题点接管，结束的是用户眼前正在用的那个终端，连同它正在跑的新会话。

## Decision

**一个 TUI 进程同一时间只跑一个会话。** 收到 TUI hook 时，若它带的进程身份
（hook 捕获时的 pid + 启动时间，二者都必须有）与另一个"未停止、写者为 external_tui"
的会话记录一致，而 hook 属于另一个会话 id，就把那个旧会话标为已分离
（`EXTERNAL_DETACHED_*`，`stop_reason=external_tui_session_switched`）并刷新状态卡：
"运行中"与接管按钮随之消失，会话仍可导入、继续。

- 在入站去重之后、建会话/认领之前做，覆盖所有观察类 hook（重复投递不会再动一次）。
  没有显式事件号的 hook，去重键最后用捕获时间兜底：同一会话切走又切回的两次
  SessionStart 不会被当成一次投递。`/clear`、`/resume` 都会触发
  SessionStart，旧话题当场收尾，不必等新会话的第一句话（新会话照 ADR 0066 在
  首句时才建话题）。
- **时序一律按 hook 捕获时间**（处理时间受延迟队列积压影响，不能用来排先后）：
  认领/新建/复活会话时，把那条 hook 的捕获时间记为 `claimed_captured_at`；只认
  新鲜 hook，且其捕获时间不早于待收尾会话的 `claimed_captured_at`——旧会话在切换
  前发出、晚到的 hook 不能收尾此后才被认领的新会话。
- **只认 hook 捕获时的进程身份**：显式 process/terminate ref 或捕获的进程树；缺失
  就跳过，从不在处理时重新 `ps`（重放的 hook，其 pid 此时可能已属于别的进程）。
- **旧会话不被晚到 hook 复活**：收尾时记下触发切换那条 hook 的捕获时间
  `switched_away_captured_at`；因切换被收尾的会话，拒绝捕获时间早于它的 hook 复活（否则接管按钮回来，误杀风险重现）；真正 `/resume` 回来发的是更新的
  hook，照常复活。
- 不动带 `daemon_live` 标记的会话（daemon worker 可能独立存活，ADR 0048）、不动
  已接管会话（写者不是 external_tui）、不动 codex 共享 daemon 会话（没有进程记录）。
- 误判可自愈：被收尾的会话再收到带活 TUI 进程的 hook（例如 `/resume` 回来）时，
  现有复活逻辑会把它恢复，而这条 hook 又会收尾刚才那个会话。

## Consequences

- 每个 hook 多一次内存遍历，不调用 `ps`。
- `/clear`、`/resume` 后旧话题立即变"已结束"，接管按钮失效，误杀终端的风险消失。
- 回滚：去掉这一步即回到 ADR 0066 行为（旧话题等进程退出再收尾）。

## 审查记录

2026-09-29 代码审查第 1 轮（~/.codex / gpt-6-sol，correctness + concurrency +
goalfit）：concurrency 报 1 条 Critical，三维共识同一根源——收尾在去重之前、只看
新鲜度不看先后：切换前发出、晚到的旧会话 hook 会收尾新会话，并借复活逻辑让旧话题
重新出现接管按钮；另有处理时 `ps` 取身份（pid 复用误判、违反"不调 ps"）。已按上文
"时序""只认捕获身份""不被晚到 hook 复活"三条修复，并补回归测试。

2026-09-29 代码审查第 2 轮：两维共识 1 条 Critical——两处守卫拿 hook 捕获时间比
处理时写入的时间（`acquired_at`、`last_progress_at`），队列积压时顺序颠倒，旧话题
仍留接管按钮；另 1 条 High——无事件号的 SessionStart 去重键不含时间，一小时内切回
被当重复丢弃。改为全程记录并比较捕获时间（`claimed_captured_at` /
`switched_away_captured_at`），去重键以捕获时间兜底。

