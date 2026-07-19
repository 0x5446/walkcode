# ADR 0054: 频道消息复活被清扫的 headless 会话——接管减 kill

Date: 2026-07-19

Status: Accepted; implemented

## Context

每次 runtime 重启（发版升级、崩溃恢复），孤儿清扫把所有 headless 会话标为
`stopped`（stop_reason=`runtime_restart`）。此后用户在飞书话题里发消息，
一律死路："⚪ 这条消息没有提交：会话已结束。到根会话发新消息即可开新任务。"

实锤事故：2026-07-19 v0.14.3 升级重启后，用户 17:58 给活跃话题发消息撞上
死路，只能到终端手动 `claude --resume <id>` 续命。当天发版三次，即三次
全量会话死亡。而 transcript 和 resume 凭据（agent_session_id / thread_id）
在账本里完好——死路纯属没有代码路径，不是能力缺失。

对照：外部 TUI 形态的 stopped 会话早就有出路（接管提示卡，authorize →
resume → terminate → submit）。headless 会话需要的只是同一条链路去掉
terminate——没有 TUI，无可 kill。

## Decision

`submit_user_input` 入口处，满足**全部**条件即复活后继续正常提交：

1. `status == "stopped"` 且 `stop_reason ∈ {"runtime_restart", "revive_failed"}`
   ——**只复活非自愿停止**。显式 `close_session` 的会话保持"拒绝后续
   submit"的既有契约（有回归测试钉住）。
2. 非外部 TUI 接管候选——带 TUI 印记的 stopped 会话继续走既有的
   接管提示（用户知情同意），不被静默复活抢走。
3. `_durable_resume_ref` 非空（claude_headless 要 agent_session_id，
   codex_app_server 要 thread_id）且 transport 支持 `resume_after_complete`。

复活动作（`SessionRegistry.revive_stopped_structured_session`）：
generation +1（围栏一切残留 drain）、status=running、lifecycle=IDLE、
清空 writer/lease/background_tasks。随后自然落入既有的
`_ensure_writer_ready_for_submit` → `_resume_writer_for_submit`：resume 出
新 worker、取写权、提交用户消息。本次 submit 使用复活后的新 generation。

**失败回滚**（`mark_revive_failed`）：resume 未产出 worker 时立即回退
`stopped` + `stop_reason="revive_failed"`，不留"账面 running 实际无人服务"
的幻活记录。`revive_failed` 在允许名单里——下一条消息自动重试。

## Consequences

- 发版/重启不再杀死频道对话：用户像什么都没发生一样继续聊，第一条消息
  自动拉起新 worker（代价是该消息的首响应多一次 resume 延迟）。
- 显式关闭语义不变；TUI 接管语义不变（ADR 0051/0053 不受影响）。
- 复活记 degrade 日志 `session_revived_by_channel`，可观测。
- 残留：`外部 TUI 印记 + TUI 进程已死`的 stopped 会话仍走接管提示（多一次
  点击）；因 v0.14.3 的 `target_gone` 该接管已是纯自动清杀，后续可评估把
  这类也并入静默复活。
