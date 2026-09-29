# ADR 0067: 同一个 Claude TUI 进程切到别的会话时，旧话题不再能误杀终端

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

**问进程本身它现在跑的是哪个会话，不从 hook 推断。** Claude Code 维护
`<配置目录>/sessions/<pid>.json`，含当前 `sessionId` 与进程启动时间 `procStart`，
`/clear`、`/resume` 切换时随之更新。`claude_tui_current_session(pid, lstart)` 在
`$CLAUDE_CONFIG_DIR`、`~/.claude`、`~/.claude-profiles/*` 下找这个文件，**启动时间
必须与会话记录的一致**（pid 被复用时不会答成别的进程）；`sessionId`、`procStart`
必须是字符串且会话 id 非空。找不到、对不上、格式不对（含写到一半的 JSON）都答"未知"。

1. **接管不再误杀**（消除风险的根本一步）：`_takeover_requires_external_tui_termination`
   在要结束进程之前核对——进程当前跑的若是别的会话，就不结束它，直接在后台恢复
   旧会话（旧会话此时没有任何写者，不存在双写）。"未知"照旧结束，行为不变。
   **最后一道核对放在发信号的那一刻**：接管把"要结束的会话"随进程引用交给终止
   控制器（`expected_claude_session`），控制器在每次 `SIGTERM`、`SIGKILL` 之前重读
   `sessions/<pid>.json`，进程已换会话就不发信号、返回 `switched_away`（接管照常
   完成）。恢复旧会话、扫描进程、等待退出都要时间，用户可能在其中任何一刻切走。反方向（接管那几秒里恰好 `/resume` 回同一会话）不另做处理：该会话
   此时已归 WalkCode，终端再发 hook 即由残留哨兵（ADR 0053）结束，与"接管后终端
   仍在写"的所有情形同一处理。
2. **显示随之修正**：ADR 0066 的 30 秒退出检测对"进程还活着"的会话顺带核对；进程
   已换会话的，标为已分离（`EXTERNAL_DETACHED_*`，`stop_reason=external_tui_session_switched`），
   "运行中"与接管按钮消失，会话仍可导入、继续。带 `daemon_live` 的会话不动
   （daemon worker 可能独立存活，ADR 0048）。

只作用于 Claude（codex 共享 daemon 会话没有进程记录；codex 没有同类文件）。

## 否决的方案：从 hook 推断"进程当前跑哪个会话"

先后试过：收到另一会话 id 的新鲜 hook 就收尾同进程的旧会话；按捕获时间给认领、
收尾打戳；每会话记最大捕获时间、按"最晚者胜"结算；按进程记一张"最新 hook"的账。
六轮代码审查，每轮都找到新的时序漏洞（重复投递、延迟队列积压、晚到 hook 回拨时间戳、
同一会话换进程继续、胜者还没有话题、重启或账淘汰后丢失顺序），其中多条能让旧话题
重新拿到指向当前终端的接管按钮。根本原因：hook 是事后、可能乱序到达的间接证据，
要从它重建"现在"就得处理全部投递顺序与持久化；而进程自己的记录就是"现在"。

## Consequences

- 接管时多读一个小 JSON 文件；30 秒检测对每个活着的 Claude 会话各读一次，不调 `ps`。
- `/clear`、`/resume` 后约 30 秒旧话题变"已结束"；这 30 秒内即使点接管也不会结束终端。
- 依赖 Claude Code 的 `sessions/<pid>.json`（2.1.283 实测存在）。文件格式变了或不存在
  时一律"未知"，退回 ADR 0066 行为，不会误判。
- 回滚：去掉两处核对即回到 ADR 0066 行为。

## 审查记录

2026-09-29 第 2 版代码审查（~/.codex 额度用尽，改用 codex personal / Command Code
gpt-5.6-sol，correctness + concurrency + goalfit）：2 条（High/Warning），均已修——
接管判定跨多次 await 缓存到发信号时（→ 发信号前再核对）；`sessionId` 类型异常时
被当成有效会话（→ 只认非空字符串）。

2026-09-29 第 2 轮（同引擎，只审第 1 轮修复）：两维同一条 High——发信号前的核对仍早于
控制器内部的扫描与等待，`SIGKILL` 前也没核对。已把核对移进控制器、紧挨每次
`os.kill`。

2026-09-29 第 3 轮：correctness 1 条 Medium——旧会话 id 只认 `agent_session_id`，恢复
逻辑还接受 `claude_session_id`/`resume`/`session_id` 别名；两处核对改为共用
`_claude_resume_session_id`。concurrency 维度因网络超时未产出，随第 4 轮重跑。

