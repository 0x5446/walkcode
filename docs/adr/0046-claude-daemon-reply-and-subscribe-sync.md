# ADR 0046: Claude Daemon reply/subscribe 多端同步，取代 takeover 直写路径

Date: 2026-07-04

Status: Accepted; v1 implemented 2026-07-04 — reply 写路径、subscribe 状态
watcher、takeover 回落、`WALKCODE_CLAUDE_DAEMON_MODE` 门禁（细节与验证记录见
`docs/design/claude-daemon-multi-ui-sync.md`）；v2 implemented 2026-07-05 —
权限/AskUserQuestion 闭环改走 PreToolUse gate（`permission-response` 实测为
空壳、已弃用，见文末 v2 段）；v3 implemented 2026-07-06 — attach 按键注入实现
真双端（推翻 v2「双端同时可答不可行」结论，见文末 v3 段；键位映射与生产注入
路径均真机实测，单测 621 绿，work 实例 Live E2E 全场景通过：飞书答/终端答/
deny/always-allow 记忆自动注入/迟点击诚实翻卡）。
list 兜底建会话、Codex 持久订阅仍为后续步骤。

## Context

Claude Code ≥2.1.x 引入 daemon 架构：每个 `CLAUDE_CONFIG_DIR` 一个 supervisor，
TUI 会话是 daemon 管理的 worker，控制面协议已被逆向并实测验证
（`docs/design/daemon-appserver-protocol-reference.md`）：

- `subscribe`（免认证）推送 `state` patch（tempo/state/needs/detail）与 `settled`；
- `reply`（`daemon/control.key` 认证）把文本注入运行中会话，等价 TUI 手输回车；
- socket 路径可由 `sha256(CLAUDE_CONFIG_DIR)[:8]` 确定性推导。

现有 V3 对 TUI 会话只有只读 hooks 观察；飞书端要写入必须 takeover——
kill TUI 进程后用 headless SDK resume。该机制与 daemon worker 进程模型持续
冲突（c132da3 已在给 takeover 打 pgrep 扫尾补丁），且体验上牺牲了终端侧。

## Decision

1. 新增 `channel_native/claude_daemon.py`：`ClaudeDaemonClient`（ndjson unix
   socket 协议客户端）+ `ClaudeDaemonTransport`（`AgentTransport` 实现，
   `multi_client_observe/write=True`）。
2. 写路径：`Orchestrator.submit_user_input` 遇到 `EXTERNAL_TUI_READONLY` 时，
   先尝试 daemon `reply` 直写（`has` 确认 job 存活）；成功即返回，不做 writer
   所有权变更，TUI 保活。daemon 不可用才回落原 takeover 流程。
3. 读路径：runtime 维护任务为每个活跃 claude TUI 会话开一条 `subscribe` 长连接，
   `state` patch 驱动生命周期与健康卡（`needs` → WAITING_PERMISSION + 提醒卡），
   `stream`(ANSI) 忽略；内容渲染继续由 hooks 提供（文本干净、无需解析 ANSI）。
4. 配置 `WALKCODE_CLAUDE_DAEMON_MODE=auto|off`（默认 auto）；`ping` 校验
   `proto==1`，不符即整体降级回 hooks/takeover。
5. 本期不实现 `permission-response`（payload schema 未实测）与 `dispatch`
   新建会话；飞书新建会话仍走 headless SDK。

## Scope correction (2026-07-05)

真机验证发现：daemon 只托管 **background-agent 会话**（`claude --bg` / `/bg`
后 attach）。普通交互式 TUI 会话不是 daemon job，reply/subscribe 均不可用，
自动回落 hooks + takeover（回落路径已验证，日志可见
`claude_daemon_reply_failed`）。多端同步的使用前提是会话以 bg 形态运行。

## Consequences

- 同一 Claude 会话终端与飞书双端同时可读可写；takeover 从主路径退为兼容回落。
- hooks 不删除：仍负责会话发现与内容渲染，daemon 负责状态实时性与写入。
- 协议 experimental：版本升级可能破坏；`ping` 门禁 + hooks 回落是安全网。
- 每 profile 一个 daemon，与 5-wrapper/5-instance/5-bot 矩阵一一对应，无共享
  socket 的跨 profile 风险。
- 后续演进：list 兜底建会话、Codex 持久订阅（审批闭环已由 v2 的 PreToolUse gate 落地；`permission-response` 已弃用）。

## v2: PreToolUse gate 权限/AskUserQuestion 闭环（2026-07-05）

v1 落地后的真机复盘暴露：权限与 AskUserQuestion 在飞书端只有"去终端按"的
死胡同；同时存在回显冗余、空闲误报橙卡、活跃会话误标已结束等交互缺陷。

决策：

1. **daemon `permission-response` 弃用**。schema 实测为
   `{proto, op, short, requestId, allow, auth}`，但 handler 只校验 auth 即返回
   `{ok:true}`，不转发 worker（真机：发送后被 gate 的会话仍阻塞）。
2. **审批/问答闭环改走 PreToolUse 阻塞 hook（gate）**，完整复用 headless
   `can_use_tool` 闭环的上层：hook 与 SDK 回调返回结构一一对应
   （allow/deny ↔ `permissionDecision`，AskUserQuestion 答案 ↔
   `updatedInput={questions,answers}`，POC 于 bg worker 实测成立）。
   进程内 Future 换成 gate spool 文件 rendezvous：
   `pending/<rid>.json`（hook→runtime）+ `decisions/<rid>.json`
   （runtime→hook，write-once）+ `serve.heartbeat`（runtime 活性）。
   rid = `tool_use_id`。fail-safe：超时**弃权**回落终端原生弹窗（v2 初版为
   超时 deny，后修正——deny 会让两端都无法作答；弃权保证终端始终可用）；
   runtime 不在跑（心跳过期）时 hook 弃权，终端原生提示流不受影响。
3. **gate 判定保守**：AskUserQuestion 恒拦；权限 gate 只拦会原生提问的工具
   （默认 Bash/Edit/Write/MultiEdit/NotebookEdit + mcp__*，减去 allow 规则、
   acceptEdits、bypassPermissions/plan）；walkcode 自身 headless worker
   不拦（SDK 已进程内闭环）。dontAsk **不豁免**——其原生兜底是自动拒绝，
   飞书卡是该模式下放行的唯一通道（work E2E 实测修正）。
   `WALKCODE_CLAUDE_GATE_MODE/TIMEOUT/TOOLS` 可调。
4. **`always_allow` 语义降级**：hook 无法像 SDK `updated_permissions` 那样持久化
   规则，降为会话级、runtime 进程内记忆（重启即忘），文档明示。
5. **交互整改**：daemon `needs` 只在 `approve <Tool>` / `tempo=blocked` 时进
   WAITING_PERMISSION（空闲 needs 不再误报）；终端侧决策通过 needs 清空回传
   飞书；TUI 进程退出仅当 daemon job 不存活才标 STOPPED（`settled` 是权威）；
   daemon 直写可用时隐藏 Take over；daemon reply 注入的输入不再以 "TUI input"
   回显（改发简短回执）；idle 型英文 Notification 不透传。

取舍：被 gate 的工具审批以飞书为主（hook 阻塞在权限引擎之前，终端此时无原生
提示可按）；"双端任一侧审批"留作后续增强。逃生口：`WALKCODE_CLAUDE_GATE_MODE=off|ask_only`。
（该取舍已被 v3 推翻，见下。）

实现与验证：`channel_native/claude_gate.py`（spool，纯 stdlib）、
`native hook PreToolUse --gate`（hook 侧超时需配 1830s）、
`ClaudeDaemonTransport.approve_permission/answer_user_question` 写决策文件、
runtime drain pending→复用 `_event_to_view` 发卡。单测 537 全绿
（新增 34 例，见 `tests/test_channel_native_claude_gate.py`）。

## v3: attach 按键注入实现真双端（2026-07-06，Implemented + E2E 通过）

v2 接受的核心取舍——"审批期间终端被挡、双端同时可答不可行（阻塞 hook 挡在
原生 UI 之前是协议约束）"——被 2026-07-06 真机实测推翻：daemon `attach` op
支持多路接入，**第二个 attacher 注入的原始 PTY 字节能直接驱动原生对话框**
（AskUserQuestion 单选三次实测：数字键一击选中即确认，blocked→resolved；
注入发生在 raw PTY 字节层，不区分对话框类型）。原生对话框本身无自动超时，
无限等键盘输入。

决策（已实施，Step 0–4；实现记录见设计文档「交互闭环 v3 › 实现记录」）：

1. **gate 从"阻塞等决策"改为"捕获后立即弃权"**（pending 新增
   `mode=notify`）：hook 捕获完整结构化 tool_input 供发富卡片，随即弃权让
   原生对话框渲染。终端键盘与飞书卡片同时可答，先答先生效。
2. **飞书答案经 attach 注入按键**：`ClaudeDaemonClient.attach_send_keys` +
   键位映射（Step 0 已全部实测：单选/多选/多问题/Other/权限框；deny 采用
   ESC——No 项数字位随布局变、ESC 位置无关；未验证形态不注入、卡片降级为
   提示）。注入前校验 `needs` 匹配、注入后 3s 校验 `needs` 清空，失败按原因
   如实翻卡走终端、不盲重试。
3. **保守路由**：`dontAsk`（弃权=自动拒绝，无对话框可注入）与非 daemon 的
   普通 TUI 会话（无 attach 面）保留 v2 阻塞 gate；headless worker 照旧不拦。
   新增 `WALKCODE_CLAUDE_GATE_STYLE=dual|block`（默认 dual，block 为整体
   逃生口）。
4. **失败模式改善**：v2 gate 失效 = 两端皆盲等超时；v3 注入失效 = 回到纯
   终端作答。injection 是 best-effort，hook/block 路径保留为兜底。

完整机制、按键映射表、竞态防护与实施步骤见
`docs/design/claude-daemon-multi-ui-sync.md`「交互闭环 v3」。
`docs/review/2026-07-06-v0.10.57-*.md` §追加变更中"双端同时可答仍不可行"
一句作废（历史报告不改，以本 ADR 与设计文档为准）。
