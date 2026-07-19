# ADR 0053: 接管 pid 身份复核 + 接管后 hook 哨兵——终结"接管完成但无回复"

Date: 2026-07-19

Status: Accepted; implemented

## Context

2026-07-19 01:16 实锤一起接管假成功事故（session 98951f59…）：

- 账本 terminate_ref 记录的 pid 24857（`claude --resume 98951f59`）早已被
  用户 Ctrl+C 杀死；当时 hook 正被复活门槛丢弃（v0.14.2 修复的 bug），pid
  停更；
- 接管流程对死 pid 发信号 → `_kill_one` 返回 `already_exited` → **按成功
  处理**，卡片报"接管完成"；
- 真正活着的 TUI 是一个裸 `claude`（终端里启动后用 /resume 进入会话），
  **argv 上没有任何 session id**——`--session-id` 正则和 pgrep 扫描都看不见
  它；
- 幸存 TUI 随后用一条（陈旧回放的）claim hook 抢回会话，刚 resume 出来的
  headless worker 被围栏挡掉，用户视角：接管完成了，飞书却再无回复。

对照组：2026-07-19 10:20 同一台机器同一套代码接管成功——唯一区别是 hook 流
正常、pid 新鲜（SessionStart 在 TUI 启动/resume 瞬间就会刷新 pid）。

核实过的机制事实（修复的前提）：

- terminate_ref 的 pid **每个 hook 都在刷新**（hook 进程自捕父进程树，serve
  端取第一个 basename 为 claude/codex 的祖先）；SessionStart 已接入且有
  claim 特权，"resume 瞬间更新 pid"早已成立。失败链的起点不是"不更新"，
  而是 hook 断流时的陈旧保留 + 死 pid 假成功 + 扫描失明。
- 能 resume 到某 session 的 claude 必然共享该 profile 的 settings.json，
  也就必然带着 walkcode hooks——**不存在"看得见 session 却不发 hook"的
  TUI**（session 文件和 hook 配置物理同目录）。hook 是发现幸存 TUI 的
  完备渠道。
- lsof 不可用作发现手段：claude 不长持 transcript 文件句柄（实测）。
- v0.14.2 曾试图"终止后复验会话仍存活即中止接管"，因裸 id 扫描误杀刚
  resume 的合法新 worker 被整体 revert（6c83ed9）。任何扫描/复验都必须把
  `_bundled` SDK worker 排除在外。

## Decision

接管从"必须一次杀干净"改为"**尽力杀 + 事后哨兵补杀**"，永不阻塞、最终收敛：

下述为**最终契约**（含二轮 deep-review 修正；历史演进见文末「修订」）。

1. **进程探测三态**（`_probe_process` → ok/gone/error）。所有杀进程/台账
   决策的地基：`ps` 超时、权限失败、输出无法解析一律是 **error**，绝不折叠成
   "进程已死"。zombie/defunct 视为 gone（pid 只在父进程回收前残留）。
2. **kill 前身份贯穿**（`_kill_one`）：terminate_ref 带 capture 时的
   `lstart`+`command`；SIGTERM 与 SIGKILL 前各 `_probe_process` 复核一次。
   探测 error → 拒绝发信号（`identity_probe_failed`）；pid 存活但身份不符
   （复用）→ 按 `already_exited` 跳过；`_wait_exited` 只把 gone 或身份变更
   当退出（error 不算），杜绝假成功。
3. **死/复用目标不降级、也不误杀**（`_enrich_terminate_ref` + `target_gone`）：
   记录时补 `recorded_at`；pid 已死或身份不符 → 标 `target_gone` 但**保留
   `allow_terminate`**。`_terminate_sync` 见 `target_gone` **完全跳过主 pid**
   （只继续会话扫描）。于是 Ctrl+C 死 TUI 走自动接管（`already_exited`），
   不落 manual_only；复用 pid 永不被信号。探测 error 时不动授权也不动身份。
4. **会话扫描三态 + 会话归属复验**（`_pids_for_session`）：pgrep 用
   `(session-id|resume)[= ]<id>`；退出码只接受 0/1（含负数在内的其余=error）；
   每个候选 `_probe_process` 后必须仍分类为外部 TUI **且**命令里的 session id
   仍等于本会话（防 pgrep→probe 间 pid 被别的会话复用）。`_bundled` SDK
   worker（含接管流程刚 resume 的新 worker）永不入选（教训 6c83ed9）。扫描
   error → 接管在**发任何信号前**返回 `session_scan_failed`（不先杀后败）。
5. **接管后 hook 哨兵**（`_sentinel_terminate_remnant_tui`）：orchestrator
   持写权期间收到外部 TUI 的**活动 hook**（非 claim）＝有 TUI 幸存（裸
   `claude` 只有动起来才自曝，hook 是唯一发现渠道）。补杀条件：hook 新鲜
   （capture 戳在 `tui_hook_fresh_seconds` 内）+ 命令分类为外部 TUI；实际
   身份复核交给控制器（并发 + 每信号 1.5s 短超时，不长占入口锁）。杀成功发
   通知卡，杀失败/异常发告警卡（`remnant_detected`），去重键含 `lstart`。
   哨兵可经 `WALKCODE_TUI_SENTINEL_ENABLED=0` 关闭——关闭时**仍发检测告警**
   （notify-only），不静默。绝不在哨兵里翻转所有权。
6. **所有权翻转门槛**（claim 分支）：外部 TUI 的 claim hook（session-start/
   sync）要翻转 orchestrator 会话，须满足 `(fresh 或 有活 TUI 背书)` **且**
   `不早于当前 owner 获权时间`。"活 TUI 背书"用 `_probe_process` 复核**当前**
   身份（命令仍是 TUI + 匹配 capture 的 lstart/command），不是只看 pid 存活
   ——pid 复用不算背书。翻转后补发"终端已接回，飞书转只读"通知卡（静默围栏
   是 01:16 的用户可见面）。
7. **capture 戳**：hook 入口打 `_walkcode_hook_captured_at`；未来时间/NaN 视为
   未知（非"0 秒即新鲜"）；defer 队列回放按条目 `created_at` 回填，无法定戳
   或 item 非字典的畸形队列项归档不回放。
8. **配置**：`WALKCODE_TUI_HOOK_FRESH_SECONDS`（默认 60，须有限正数，拒 inf/
   nan）与 `WALKCODE_TUI_SENTINEL_ENABLED`（默认开）落在 `ChannelNativeConfig`，
   从合并 env（含 `WALKCODE_ENV_FILE`）解析——不再读 `os.environ`。

排序规则是**无条件**的：早于当前 owner 获权时间的 claim 一律拒绝翻转，即便
其对应终端仍活——那个幸存终端是哨兵该清理的对象，不是交还对象（否则接管前
入队的陈旧 claim 只要终端没死就能撤销已完成的接管）。用户在接管后**主动**
resume 终端会发出一条**新的** SessionStart，其 capture 时间晚于获权时间、
不 predates，正常交还——所以主动 resume 不受影响。不引入 pending-claim 状态机
（教训 6c83ed9）。

已知残留（不阻塞，`ps` 层面无法根治）：`_pids_for_session` 与
`_terminate_ref_session_id` 从 `ps` 给出的**已扁平化**命令串里提取 session id；
若某个无关 `claude` 进程的**提示词**里恰好逐字包含目标会话的 session id
（如 `claude "查一下 --resume <该会话UUID>"`），扫描可能把它纳入。`ps` 早已
剥掉引号，无法从其输出区分真 flag 与提示词文本；且这要求用户手敲另一会话的
精确 UUID 作为提示词，现实概率≈0，影响面仅限该会话接管时的一次清扫。记录
备查，不做为此引入不可靠的 argv 猜测。

## Consequences

- "接管完成但无回复"的四个成因（陈旧保留、死 pid 假成功、扫描失明、静默
  夺权）全部关闭；裸 `claude` 幸存者在下一次活动时被清理并通知。
- pid 复用不再可能误杀无关进程（lstart+command 双重身份）。
- 每个 hook 多一次 `ps`（terminate_ref 富化）；哨兵路径只在异常态触发。
- 新增环境变量 `WALKCODE_TUI_HOOK_FRESH_SECONDS`（默认 60）、
  `WALKCODE_TUI_SENTINEL_ENABLED`（默认开，止损开关）。

## 修订历史

- **首版**（commit b9f91fa）：身份复核只在 SIGTERM 前一次、死 pid 缴械
  `allow_terminate`、扫描/等待两态、claim 只按 freshness、配置读 `os.environ`。
- **一轮 deep-review 修**（9cec3a9）：探测三态雏形、`target_gone`、lstart 捕获、
  claim fresh-or-live、配置进 config、哨兵并发/开关/去重键、分类器放宽、文案。
- **二轮 deep-review 修**（本轮，上方契约即最终态）：codex 交叉审查发现一轮
  修复"方向对但管道没接全"——补齐：capture lstart 端到端且 enrich 只比较不
  覆盖；`target_gone` 真正被控制器消费（跳过主 pid）；`_wait_exited`/
  `_pids_for_session` 全三态、扫描失败先返回再杀、退出码只认 0/1、会话归属
  复验；`_tui_hook_has_live_tui_process` 复核当前身份而非只看 pid 存活；codex
  分类器改 argv 解析；哨兵关闭=notify-only、gather 异常也告警；drain 校验
  item 类型与 created_at；配置拒 inf/nan。

残留（记录，未阻塞）：`handoff` 中途崩溃后 inbound ledger 判重导致清理/通知
不重放，属崩溃一致性架构议题，归入 backlog。
