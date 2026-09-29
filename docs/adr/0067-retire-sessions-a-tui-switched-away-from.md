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

- 在建会话/认领之前做，覆盖所有观察类 hook。`/clear`、`/resume` 都会触发
  SessionStart，旧话题当场收尾，不必等新会话的第一句话（新会话照 ADR 0066 在
  首句时才建话题）。
- 只认新鲜 hook（捕获时间在 `tui_hook_fresh_seconds` 内，与残留哨兵同一标准）：
  延迟队列重放的旧 hook 不能收尾此后才出现的会话。
- 不动带 `daemon_live` 标记的会话（daemon worker 可能独立存活，ADR 0048）、不动
  已接管会话（写者不是 external_tui）、不动 codex 共享 daemon 会话（没有进程记录）。
- 误判可自愈：被收尾的会话再收到带活 TUI 进程的 hook（例如 `/resume` 回来）时，
  现有复活逻辑会把它恢复，而这条 hook 又会收尾刚才那个会话。

## Consequences

- 每个 hook 多一次内存遍历，不调用 `ps`。
- `/clear`、`/resume` 后旧话题立即变"已结束"，接管按钮失效，误杀终端的风险消失。
- 回滚：去掉这一步即回到 ADR 0066 行为（旧话题等进程退出再收尾）。
