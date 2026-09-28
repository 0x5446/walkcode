# ADR 0064 — codex hook 按 turn id 归属，不看进程树

- 状态：Accepted
- 日期：2026-09-28
- 版本：v0.14.28
- 相关：[ADR 0053](0053-takeover-pid-identity-and-hook-sentinel.md)（残留 TUI 哨兵）、
  [ADR 0060](0060-codex-resident-event-listening.md)（codex 常驻监听；本 ADR 更正其 hook 结论）、
  [ADR 0041](0041-codex-unified-app-server-client-architecture.md)（共享 daemon 是目标架构）

## 背景：codex 0.157 让 TUI 和 walkcode 共用一个 daemon

2026-09-27 codex 自动升到 0.157.1。之后 `~/.codex` 下的 TUI 不再在自己进程里
跑会话，而是连到共享的 managed app-server daemon（TUI 底栏出现
"← for agents"；codex 日志里 `thread/start` 来自 `client_name="codex-tui"`、
`rpc.transport="unix_socket"`；rollout 的 `session_meta.source` 从 `cli` 变成
`vscode`）。walkcode 的 `auto` 模式本来就连这个 daemon。

hook 也改由 daemon 执行。症状：用户在 TUI 里发消息，飞书毫无反应。

### 根因

walkcode 原先靠"hook 的进程树里有没有 `codex app-server`"判断 hook 是不是
自己的会话发出的（`_tui_hook_is_walkcode_headless_transport` 的 codex 分支）。
共用 daemon 后，TUI 线程和 walkcode 线程的 hook 进程树**完全相同**——都只有
`codex app-server --listen unix:// --managed-daemon`。这条规则于是把所有 TUI
hook 当成"自己人"丢掉。

同一个根因的第二个表现：hook 记录"TUI 进程"的最后一级兜底是"把 hook 的上级
进程当 TUI"（`source: native_hook_parent_captured`，`allow_terminate: false`）。
在 daemon 下它记下的是 daemon 的 pid。daemon 永远活着，接管流程以为有个不许
结束的活 TUI，每次都落到"TUI process termination is not authorized"。

**问题不在规则写得不够细，而在进程形态已经表达不了"这是谁发起的"。**

### 真机证据（2026-09-28）

- 用 walkcode 自己的 `CodexManagedAppServerClient` 连真实 daemon 发线程：
  `thread/start` 后 5 秒内零个 hook；`turn/start` 返回后约 **1.3 秒**才触发
  SessionStart，随后 UserPromptSubmit、Stop。进程树字段只有 daemon。
- 临时 CODEX_HOME 的 TUI 实验：UserPromptSubmit、PreToolUse、PostToolUse、
  Stop 都带同一个 `turn_id`；**SessionStart 不带**，且在第一条消息提交时才触发。
- `codex exec --ephemeral`（deep-review 用的就是它）不写 rollout，hook 收到的
  `transcript_path` 键存在、值为 `null`。v0.14.27 的 exec 过滤只读 rollout 的
  `source`，因此对 deep-review 无效。

## 决策

1. **walkcode 自己的 hook 按 turn id 认。** `CodexAppServerTransport` 在
   `turn/start` 返回时把 turn id 记进一个有界表（`CODEX_STARTED_TURNS_LIMIT`，
   插入序淘汰最老的；回合结束也不删，晚到的 Stop 照样认得）。
   `process_tui_hook` 里 codex hook 的 `turn_id` 在表里就忽略，其余交给原有逻辑。
   记录发生在 turn/start 返回那一刻，而最早的 hook 在其后 1 秒以上，无竞争。
2. **不带 turn id 的 hook 不需要新规则。** walkcode 会话的 SessionStart 落到
   orchestrator 持有的会话上，已有的 `_tui_hook_is_unverified_walkcode_owned_session_hook`
   会忽略它。runtime 重启导致表丢失时，那些回合的活动类 hook 走哨兵分支；哨兵
   只对被识别为外部 TUI 的进程下手，daemon 不在其列，于是什么都不做。表因此
   不需要持久化。
3. **进程树规则只保留为安全属性。** `_command_is_codex_tui_process` 继续排除
   `codex app-server`，保证哨兵和接管永远不会对 daemon 发信号。它不再用于识别
   "这是不是 walkcode 的会话"。
4. **daemon 里执行的 hook 记成共享 daemon 持有。** 进程树首项（hook 的上级）
   是 `codex app-server` 时，`terminate_ref` 记为
   `{"controller_kind": "shared_app_server"}`，不带 pid，不再走上级 pid 兜底。
   接管遇到这个标记时不要求结束任何进程：walkcode 在 daemon 上 resume 线程，
   与仍开着的 TUI 并存。真机验证：接管后上下文连贯，TUI 实时显示飞书发起的那一轮。
   用户已接受这个行为变化（以前的进程内 TUI 接管会被结束）。
5. **`transcript_path` 键存在而值为空，视为一次性运行，忽略。** 没有 rollout
   的线程本来就无从镜像。键缺失（老调用方、测试）保持原处理。

## 已知缺口（下一步）

- **接管后在 TUI 里继续打字，这些回合不镜像到飞书。** 它们的 turn id 不是
  walkcode 发起的，落到 orchestrator 会话上走哨兵分支，安静返回；数据不丢，
  两边始终是同一段对话，飞书下次提问时模型知道 TUI 里发生过什么。
- **daemon TUI 的退出检测不到。** 退出检测盯的是 terminate_ref 里的 pid；共享
  daemon 标记没有 pid，这类会话会一直显示"运行中"（此前记的是 daemon pid，同样
  永不退出，不算回退）。

两者的正确来源都是 daemon 事件流：接管后 walkcode 已作为客户端订阅了该线程，
TUI 发起的回合会推到同一条事件流。另立 ADR 设计。

## 被否决的方案

- **细化进程树规则**（例如区分 `--managed-daemon` 与 `--stdio`）：walkcode
  自己的线程同样跑在这个 daemon 上，进程层面没有任何可区分的信息。
- **rollout 的 `originator` 字段**：实测 walkcode 经 daemon 发起的线程
  `originator` 也是 `codex-tui`（walkcode 报的 `clientInfo.name="walkcode"` 没有
  写进去），不可靠。
- **沿 hook 渲染路径把接管后的 TUI 回合镜像进 orchestrator 会话**：试过，真机上
  PreToolUse 经 `_record_session_progress` 把会话翻成 `ACTIVE`，而 TUI 的 Stop
  不会把它改回 `IDLE`，飞书下一条消息可能被当成"回合进行中"卡住。要做干净就得在
  状态卡、工具进度卡、事件序号几处共享逻辑里区分"镜像来的"和"自己的"，同类 bug
  的温床。回归测试 `test_codex_unattributed_walkcode_hook_never_targets_the_daemon`
  断言这类 hook 不改动会话状态。

## 验证

- 单元：`tests/test_channel_native_codex.py`（turn id 跨回合结束和后端重启仍认得、
  有界淘汰）；`tests/test_channel_native_runtime.py`（daemon 进程树 + 未知 turn →
  建同步会话；+ walkcode 的 turn → 忽略；walkcode 会话的 SessionStart 不被认领；
  无法归属的 hook 不对 daemon 下手且不改会话状态；daemon 内 hook 记共享 daemon
  标记而非 pid；ephemeral exec 忽略）；`tests/test_channel_native_takeover_orchestrator.py`
  （共享 daemon 会话接管成功且零次结束进程）。每条都做过变异检查：撤掉对应改动
  测试必失败。测试数据按真实 `walkcode native hook` 入口补齐字段
  （`_walkcode_infer_tui_pid` 等），否则会漏检。
- 真机（bfjdfhnf-codex，分支代码前台运行）：真实 `~/.codex` TUI 回合同步到飞书；
  deep-review 同款 ephemeral exec 的 hook 进队列后被丢弃、会话数不变；飞书接管
  daemon TUI 会话成功、答出 TUI 里设定的口令、TUI 未被关闭；接管后飞书继续提问
  正常回复且 walkcode 自己的回合没有被重复同步成"终端输入"；接管后在 TUI 打字
  不改变会话状态（保持 IDLE）。
