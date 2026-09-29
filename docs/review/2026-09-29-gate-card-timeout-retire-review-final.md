# 阻塞 gate 超时转终端后收卡 — deep-review

- Repo: /Users/alpha/workspace/walkcode-gatecard
- Branch: fix/gate-card-timeout-flip（基于 chore/dead-code-batch2）
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）；cursor 未登录跳过
- 门禁: 无 Critical
- VERDICT: SAFE（两轮全部 Warning 已修；已达 MAX_ROUNDS=2，最后一条修复以回归测试 + 旧版本对照验证）

## 根本原因（用户实报）

TUI 会话调 AskUserQuestion，阻塞模式 gate 先发飞书卡片，hook 等飞书决策最多 gate_timeout（personal/work 未配置，默认 1800 秒）。超时后 hook 放行，终端弹出原生对话框，用户在终端作答；卡片只会被点击回调翻转，而 outbox 发送后丢掉了 message_id，系统无法主动改卡 → 卡片永远留着可点按钮。自 2026-07-06（超时改为转终端，b930720）起存在。

同时把 personal-claude / work-claude 的 `WALKCODE_CLAUDE_GATE_TIMEOUT` 设为 30（与 work2 对齐，本机 env，不在 diff 内）。

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | ccc39d2 | deep-review-walkcode-gatecard-ccc39d2-1790696246.fbiY | 6 维：卡片仍在 outbox 重试时 hook 超时即漏收（3 维共识，复现）；edit_view 返回 False 被当成功且无日志（3 维）；pending 读失败误判为 hook 已返回（data）；notify 隔离缺测试（maintainability）→ 713d445 修；TUI hook 排水 10 次归档（PreExisting，第 1 批既有决策）不改 |
| 2 | 713d445 | deep-review-walkcode-gatecard-713d445-1790696669.0EWm | concurrency SAFE；correctness 1 条：等待投递也消耗改卡重试额度，限流退避超过 120 轮后仍会漏收 → 本提交拆出 "queued" 状态，不计入额度（outbox 自身有界） |

## 最终结构

- outbox 持久化已发送消息的 `message_id`；`sent_message_id` / `is_pending` 按幂等键查询
- `Orchestrator.settle_timed_out_gate`：hook 返回而 HITL 仍 pending → 标 stale，交互记为 terminal（旧 token 失效）
- `Orchestrator.retire_gate_card`：done / gone / queued / retry；runtime `_gate_cards_retiring` 仅对 retry 计数（上限 120）
- 收卡前确认 pending 文件确实不存在

## 已知限制

runtime 重启期间 hook 结束：阻塞卡片跟踪为内存态，该卡不会被收（HITL 之后到期变 expired）。概率低，未处理。

## 验证

- 全量 1259 passed, 9 skipped
- 每条修复的回归测试均在修复前版本上失败；notify 隔离测试经变异验证（登记挪出分支即失败）
