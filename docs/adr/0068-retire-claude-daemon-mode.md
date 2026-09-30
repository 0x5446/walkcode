# ADR 0068: 退役 Claude daemon 模式，PreToolUse 阻塞 gate 独立成 ClaudeGateTransport

Date: 2026-09-30

Status: Accepted; implemented

Supersedes: ADR 0046（v1 reply/subscribe、v3 attach 按键注入；v2 的阻塞 gate
保留）、ADR 0048（全部）、ADR 0050 第 1、4 条里"daemon 作为显式 opt-in 保留"的
部分、ADR 0066 第 3 条（daemon socket 缺失计时）。ADR 0049 本来就没合入。

## Context

ADR 0046/0048 给 Claude 接了 Claude Code 的 daemon 控制面（`claude --bg`
worker、`reply` 直写、`subscribe` 状态、attach 按键注入、list 收编、daemon
spawn）。ADR 0050 因 attach 端双端并发渲染混乱把默认翻回单 master UI，daemon
退为显式 opt-in。

2026-09-29 核实的现状：

- 三个 Claude 实例的 env 都是 `WALKCODE_CLAUDE_SPAWN_MODE=headless`，wrapper
  是纯 TUI；daemon socket（`/tmp/cc-daemon-501/`）不存在；日志里 notify gate /
  按键注入事件为 0。
- 但 `WALKCODE_CLAUDE_DAEMON_MODE` 默认 `auto`，所以 `ClaudeDaemonTransport`
  仍被注册，watcher 和 drain 任务一直在跑；每条发给 TUI 会话的消息都先试一次
  `_try_external_daemon_reply`，失败了才走 takeover——日志里有 40 多条
  `claude_daemon_reply_failed ... fallback=takeover_prompt`。
- 生产上真正在用的只有一样东西：TUI 会话的**阻塞式 PreToolUse gate**
  （`walkcode native hook PreToolUse --gate`，profile settings.json 里配、
  timeout 1830）。它的决策投递却寄居在 `ClaudeDaemonTransport` 里
  （`approve_permission` / `answer_user_question` / `_deliver_gate_decision` /
  `on_gate_decision`），`Orchestrator._interaction_transport` 对 TUI 会话回落到
  `transports["claude_daemon"]`，gate drain 任务也只在 daemon transport 存在时
  才启动。
- 隐藏耦合：`gate_tui_hook` 在 `daemon_mode == "off"` 时直接返回 None。也就是
  说，操作者按文档设 `WALKCODE_CLAUDE_DAEMON_MODE=off` 关 daemon，会**静默关掉**
  TUI 的权限卡和 AskUserQuestion 卡。

## Decision

1. **gate 决策投递独立**：新增 `channel_native/claude_gate_transport.py` 的
   `ClaudeGateTransport`，只做一件事——把卡片决策写成
   `decisions/<rid>.json`（写前确认 pending 还在、write-once 成功，否则抛
   `GateDecisionFailed("stale_gate" | "already_resolved")`），成功后回调
   runtime 的 `_record_gate_decision` 学习会话级 always_allow。它不连接任何
   Claude 进程。claude 实例在 `_build_transports` 里固定注册到
   `transports["claude_gate"]`；`_interaction_transport` 对 TUI 会话路由到它；
   gate drain 任务以它是否存在为启动条件。
2. **gate 恒为阻塞模式**：`gate_tui_hook` 去掉 daemon_mode 判断和 notify 分支，
   只写 `mode=block` 的 pending 并阻塞等决策。`WALKCODE_CLAUDE_GATE_MODE` /
   `GATE_TIMEOUT` / `GATE_TOOLS` 语义不变。
3. **daemon 整体删除**：`claude_daemon.py`（client、transport、键位映射、
   observer attach、`claude --bg` spawn）、`ClaudeDaemonTransport` 注册、
   orchestrator 的 `daemon_spawner` / `_try_external_daemon_reply` /
   `consume_daemon_reply_echo`、runtime 的 subscribe watcher、state patch、
   settled 收尾、list 收编、`_claude_daemon_session_alive` 及 socket 缺失计时、
   notify gate 注册与探测、`daemon_live` 相关的状态卡/停止路径逻辑、健康卡
   "双端同步中"与卡片"先答先生效"注记、`degraded` 翻面、doctor 的
   `claude_daemon:` 行和 `describe()["claude_daemon"]`。

## What survives

- `native hook PreToolUse --gate` 的全部行为：心跳过期弃权、超时弃权回落终端、
  headless worker 不拦、allow 规则/permission mode 豁免、会话级 always_allow、
  不可路由/发卡失败 10s 后 `pass`、过期 pending 回收、孤儿 decision 回收。
- v0.14.34 的卡片收尾：`_gate_block_cards` → `settle_timed_out_gate` →
  `retire_gate_card`（含投递中等待、编辑失败重试上限）。
- 迟点旧卡的诚实翻面：`stale_gate` → "已失效"，`already_resolved` → "已在终端
  处理"，文案与之前相同。
- headless 会话、takeover、终端 resume 夺回、TUI 退出扫描（ADR 0066 第 1、2
  条）、切走收尾（ADR 0067）不变。

## Migration notes

- **env**：`WALKCODE_CLAUDE_DAEMON_MODE` / `WALKCODE_CLAUDE_SPAWN_MODE` /
  `WALKCODE_CLAUDE_LIST_ADOPT` / `WALKCODE_CLAUDE_GATE_STYLE` 不再解析。现有
  env 文件里还有（如 `SPAWN_MODE=headless`），所以它们**不报错**：配置解析时
  每个进程在 stderr 打一行 `walkcode: ignoring retired env ...`，值是什么都
  不影响启动（包括旧解析器会拒绝的值）。可以随手从 env 删掉。
  `WALKCODE_CLAUDE_DAEMON_MODE=off` 以前会顺带关掉 TUI gate，现在不会——想关
  gate 用 `WALKCODE_CLAUDE_GATE_MODE=off`。
- **state**：`transport_kind == "claude_daemon"` 的会话加载时映射成
  `external_tui`；`transport_ref` 里的 `daemon_short` / `daemon_live`、binding
  的 `origin=daemon_spawn`、`last_progress_event` 里的 `external_tui.daemon_*`
  原样保留但不再有任何代码读取。副作用：以前 `daemon_live` 会藏掉 Take over
  按钮、让 TUI 进程退出不收尾，现在这类会话按普通 TUI 会话处理。
- **gate spool**：升级前的 hook 可能为 daemon 会话写过 `mode=notify` 的
  pending（没有 hook 在等它）。drain 看到就直接删掉，不发卡、不写 decision
  （trace `drop_legacy_notify_pending`）。升级瞬间还阻塞着的旧 hook 写的是
  `mode=block`，新 runtime 照常处理。
- **wrapper**：机器本地 wrapper 里的 `WALKCODE_NO_BG=1` 已无作用，可删。
- **doctor**：`native doctor` 不再输出 `claude_daemon:` 行。

## Consequences

- 发给 TUI 会话的消息直接出 takeover 卡，不再先撞一次 daemon、不再产生
  `claude_daemon_reply_failed` 日志。
- claude 实例少了一个常驻 watcher 任务，gate hook 热路径少了一次 daemon `has`
  探测（原 0.5s 预算）。
- "终端与飞书同时可答"（v3 dual）不复存在：gate 期间终端不弹原生框，飞书为主，
  超时才回落终端。这与 ADR 0050 之后的实际运行形态一致。
- 如果将来 Claude Code 的 attach 渲染问题解决、想重做双 UI，以
  `archive/adr-0049-notify-gate-tristate` 标签和本次删除前的 main（v0.14.35）
  为起点，基于当时的代码重做。

## Verification

- 单测：删除 `test_channel_native_claude_daemon.py`、
  `test_channel_native_daemon_spawn.py` 及 gate 测试里的 notify/注入/daemon
  patch 部分；阻塞 gate 与卡片收尾测试全部保留；新增无 daemon 端到端（hook 线程
  阻塞 → drain 发卡 → 经 `_interaction_transport` 点卡 → hook 返回 allow 且学到
  always_allow）、旧 `DAEMON_MODE=off` 不再关 gate、legacy notify pending 被
  丢弃、旧 env 键忽略且只提示一次、旧 state 加载与 takeover 直达等用例。
