# ADR 0052: headless 会话级持续监听——后台子 agent 回合不再丢失

Date: 2026-07-17

Status: Accepted; implemented

## Context

2026-07-17 凌晨实锤一起静默丢失事故（sess-eb3f9212…，深度调研任务）：

- 01:07 headless 会话第一回合结束（模型说"后台调研已派出，稍等"），
  orchestrator 收到 `turn.completed` 后置 IDLE，事件 drain 随之退出；
- 01:09–01:20 三路 run_in_background 子 agent 陆续完成，CLI 注入
  task-notification 自动开新回合，生成了全部阶段性总结与 6500 字最终计划
  ——这些回合的消息**再无消费者**，一条都没到飞书；
- 01:21 模型调 AskUserQuestion，`can_use_tool` 回调的事件死在 permission
  bridge 队列里（HITL 只在 drain 循环内注册），无卡片、无回答，CLI 子进程
  挂死整夜；
- 用户次日在飞书看到的是：状态卡停在 空闲/turn.completed，一夜无消息。

根本原因是架构假设过时：`_bridged_event_stream` 用 SDK 的
`receive_response`（按定义在第一个 ResultMessage 终止），整个编排是
"一次 submit ↔ 一趟 drain"；而 CLI 的后台子 agent 可以在回合结束后自发开启
新回合。次生问题：每次 IDLE submit 走 `resume()` 新起子进程，旧 client 永远
留在 `_clients`（进程泄漏）；TaskNotificationMessage 在 `_convert_sdk_message`
里无分支被静默丢弃。

协议侧调研结论（SDK 0.2.120 + CLI 2.1.211 实测，官方文档交叉验证）：

- **不存在**单一的"会话级一切结束"官方信号：`result` 只是回合边界，字段里
  没有 pending 任务信息；SessionEnd 只在进程退出时触发且不进 SDK 流。
- CLI 在 headless 流里发完整的任务生命周期事件：`task_started` /
  `task_progress` / `task_notification` / `task_updated`（终结状态
  completed/failed/stopped/killed），SDK 有 typed 解析；另有未见于文档的
  `background_tasks_changed`（携带仍在跑的后台任务全量列表，清零时发空表）。
- 两个坑：终结**不一定**发 task_notification（可能只有终结态的
  task_updated）；同一 task_id 可被 SendMessage 唤醒而**多次通知**（销账必须
  按状态，不能数次数）。
- 官方一次性 `-p` 模式的语义与本决策同向：进程会等后台子 agent 完成才退出
  （默认封顶 10 分钟，`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`）。

## Decision

**监听不依赖判断，"下班"依赖组合判定 + 硬超时。**

1. **会话级监听**：`_bridged_event_stream` 底层换 `receive_messages`，
   ResultMessage 只标记回合边界（session 翻 IDLE、状态卡照常），流本身跨
   回合存活；后台子 agent 开启的新回合照常转 outbox / 注册 HITL。仅支持
   `receive_response` 的旧式（测试）client 保持单回合行为。
2. **后台任务账本**：`task_started` 记账；`task_notification` **或**
   `task_updated` 带终结状态销账；running/pending 的 task_updated 重新记账
   （唤醒场景）；`background_tasks_changed` 作为权威全量清单校准账本。账本
   节拍以新事件类型 `background.tasks` 上浮：刷新 liveness、驱动状态卡
   "后台: N 个任务进行中"，不产生频道文本。
3. **settle（下班）四条件 + 静默宽限**：无进行中回合（含"已 submit 未上流"
   的竞态保护）∧ 账本空 ∧ bridge 无 pending HITL ∧ 静默满
   `WALKCODE_CLAUDE_SETTLE_GRACE`（默认 5s）→ 关闭并注销 worker client，
   下条消息经 `--resume` 满上下文重连。EOF（进程死亡）同样触发清理。
   补充窗口：回合间任何**清空账本的终结事件**（notification、裸终结态
   task_updated、空表 background_tasks_changed）都预示 CLI 会注入后续回合，
   settle 需额外等一个有界注入窗（内置 30s，取与 grace 的较大值）；有 HITL
   待答时等待按 60s 有界复查而非无限挂起，答复后静默计时从答复时刻重算。
4. **硬超时**：账本非空但流上零流量达 `WALKCODE_CLAUDE_BG_WAIT_CEILING`
   （默认 3600s，0 关闭）→ 先发可见警告（"仍有 N 个后台任务无进展，停止
   等待"）再 settle。绝不静默放弃。同一上限也约束**已提交但零响应的用户
   回合**（从提交时刻起算，v0.14.1）：到点发"消息未得到响应"警告后关闭，
   不做后台专属预算理解。
5. **活 worker 复用**：IDLE/ERROR_RECOVERABLE submit 时若 handle 的 client
   仍在（监听中），直接在原 client 上 `query`，不再 fork 第二个 `--resume`
   进程；每个 handle 至多一个 drain。settle 与 submit 的竞态由
   TransportUnavailable → 一次 resume 兜底重试覆盖。
6. **泄漏修复**：`resume()` 先关旧 client；`shutdown()` 真正 disconnect；
   user-role 消息（含注入的 task-notification 回合）永不回流为 agent 文本。

## Consequences

- 深度调研类"派后台任务→稍等"的会话，后续所有回合实时到达飞书；深夜
  AskUserQuestion 正常出卡可答，不再挂死进程。
- 稳态下每会话至多一个 headless 进程，settle 后归零；此前每次 resume 泄漏
  一个进程的问题一并消除。
- 两种下班语义的代价刻意区分开：**settle 判定**（账本已空）方向的误判是
  安全侧——多听只是进程多活一会儿，不丢消息；**硬超时 ceiling**（账本非空
  但零流量到点）则是显式放弃——监听关闭后迟到的后台结果不再自动送达，
  用户会先收到可见警告。调低 `WALKCODE_CLAUDE_BG_WAIT_CEILING` 会更早释放
  进程，但也提高后台结果被放弃的风险。
- 状态卡在空闲但有后台任务时显示"后台: N 个任务进行中"，用户可区分
  "真闲"与"后台在跑"。
- 回归风险集中在 settle 判定与复用竞态，均有专项测试
  （tests/test_channel_native_headless_persistent_drain.py，15 例）。

关联：ADR 0050（单 master UI）、ADR 0051（handoff pending HITL）、
ADR 0047（tap 代理，排查本事故的证据链来源之一）。
