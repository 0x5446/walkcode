# ADR 0066: TUI 话题在第一句话时才建；TUI 退出按周期检测

Date: 2026-09-29

Status: Accepted; section 3 superseded by ADR 0068

## Context

2026-09-29 11:06，personal-claude 的飞书主窗口出现两张"claude: TUI <uuid>"卡片
（时长 0 秒、模型"—"），进程早已退出，卡片却一直"运行中"。排查全部实例后发现三个
独立缺口：

1. **话题建得太早**。`_tui_hook_can_create_session` 允许 `session-start`（以及
   `sync`）建会话，而建会话就会发状态卡、开话题。打开 TUI 不说话就退出、用
   `/resume` 切走、Claude 续接对话时换了新 session id 只触发一次 SessionStart
   ——这些都留下一个空话题，标题只能用 `TUI <uuid>` 顶上（Claude 在第一句话之前
   不写 transcript，事后也无从补标题）。实测 personal-claude 有 3 个这样的空话题
   （进程还活着，所以也不会被清）。
2. **退出只在启动时检查一次**。`_maybe_mark_stale_tui_process_detached` 只在
   `_refresh_loaded_tui_observed_bindings`（启动后第一轮完整扫描）里跑。三个
   Claude profile 都没配 SessionEnd hook，代码里 SessionEnd 也被归一成 `stop`
   （一轮结束），不会结束会话。于是 TUI 退出后话题一直"运行中"，直到下次重启。
   另外 `_process_ref_is_running` 只比对 pid：pid 被复用会把死会话当活的；`ps`
   偶发失败却被当成"已退出"。
3. **daemon 不在时，"worker 还活着"的旧标记永不过期**。`_claude_daemon_session_alive`
   在探测失败时回落到 `transport_ref.daemon_live`。本机已切单 master UI（ADR 0050），
   Claude daemon 不运行、socket 文件不存在，探测每次都失败，7 月留下的 4 个会话
   因此一直"运行中"，重启也清不掉。

## Decision

### 1. 第一句话才建话题

`session-start` / `sync` 不再建会话，只保留"认领已有会话"（`--resume` 回到已有
话题照旧）。建会话**只由 `user-prompt-submit` 触发**：`pre-tool` 等活动 hook 不带
用户原话，由它建出的话题同样只能用 `TUI <uuid>` 当标题，所以也不再建（它们照旧
在已有话题里处理）。建会话时先用 `compose_session_title` 从这句话算出标题，传给
根卡片——话题一出现就是这句话，不再先显示 uuid 再等刷新；算不出标题（例如只发了
附件）才回落到 `TUI <uuid>`。空会话不发任何消息。codex 0.157 的 SessionStart 本就
在第一轮才触发，行为不变。

代价：UserPromptSubmit hook 缺失或丢失时，这个 TUI 不会在频道出现。doctor 已把
它列为必配 hook。

### 2. 周期检测 TUI 进程退出

新增维护任务，每 30 秒一次：取所有"未停止、写者为 external_tui、带进程记录"的
会话，**合并成一次** `ps -o pid=,stat=,lstart= -p a,b,c`：

- pid 不在输出里、或是僵尸 → `gone`；
- pid 在但启动时间与记录不符 → pid 被复用，`gone`；
- pid 在且启动时间相符（或记录里没有启动时间）→ `alive`；
- `ps` 本身失败（超时、退出码 >1、输出解析不了）→ 全部 `unknown`，本轮跳过。

探测结果是三态，收尾路径直接用这份结果，**不再逐个重查进程**；`unknown` 一律
不改状态、不刷卡片。`ps` 在线程里跑，回到事件循环后、改状态前再核对一次：会话
仍未停止、写者仍是 external_tui、进程记录（pid + 启动时间）与探测时的快照一致
——期间有 hook 认领或复活过就跳过，下一轮再看。判定 `gone` 后仍保留现有的
daemon worker 保护（worker 还活着只记"已分离"）。

启动扫描改用同一个三态判定（单个会话的探测也只分三态，不再把 `ps` 失败当成
退出）。已停止的会话不再检查，不会重复处理；每轮之间固定等待，不会空转。

### 3. daemon socket 连续缺失 60 秒即"worker 不在"

> **已作废（ADR 0068）**：Claude daemon 模式退役后 `_claude_daemon_session_alive`
> 与 socket 缺失计时一并删除，TUI 进程消失即按普通 TUI 会话收尾。

探测失败（`job_alive` 返回未知）时，runtime 记下 daemon socket 文件**连续缺失**
的起始时间：缺失不到 60 秒仍按未知处理、沿用旧标记（覆盖 daemon 重启时短暂删掉
socket 的窗口）；连续缺失满 60 秒才判定 worker 不在。socket 文件一出现就清零。
文件在但连不上仍是未知。判定只影响"是否结束已退出 TUI 的会话"，已结束的会话
收到带活 TUI 进程的 hook 仍会被现有逻辑复活。

## 不在本次范围

- 同一个 Claude 进程里 `/clear` 或 `/resume` 到别的会话后，旧会话的进程仍活着，
  话题仍"运行中"。需要按 session id 判定"当前会话"，另议。
- 现存的空话题（进程仍活着的那几个）不补救：进程退出后由第 2 条自动收尾。

## Consequences

- 每 30 秒一次 `ps`（与会话数无关），1 秒超时。
- 上线后存量的死会话会在前一两轮里一起被标"已结束"，每个会话只刷一次状态卡
  （补丁，不发新消息）。
- 关掉 TUI 后约 30 秒话题变"已结束"，接管按钮随之失效。
- 打开 TUI 不说话不再产生话题；第一句话前的 TUI 在频道里不可见、不可接管
  （此时也没有可接管的内容）。
- 回滚：第 1 条恢复 `session-start` 建会话；第 2 条删除维护任务；第 3 条恢复
  回落旧标记。三者相互独立。

## 审查记录

2026-09-29 方案审查（~/.codex / gpt-6-sol，goalfit + feasibility）：6 条 Warning，
全部采纳——建话题时标题仍是 uuid、`pre-tool` 仍会先于首句建空话题（→ 只由
user-prompt-submit 建，标题传给根卡片）；收尾路径逐个重查进程、布尔接口表达不了
"未知"、重查失败会误判（→ 三态结果直接传给收尾，改前核对快照）；socket 短暂
消失会误判（→ 连续缺失 60 秒）。

2026-09-29 代码审查第 1 轮（同引擎，correctness + concurrency + goalfit）：三维同两条
Warning，均已修——快照核对放在等待 daemon 探测之前，探测期间被认领仍会误收尾（→
核对移到最后一次 await 之后、改状态之前，启动扫描同样受益）；daemon 恢复且探测
成功时缺失计时未清零（→ 每次探测都更新计时）。

2026-09-29 代码审查第 2 轮（只审第 1 轮修复）：两维同一条——核对只挡住"worker 不在"
分支，"worker 仍活着"分支仍会把旧进程的结论写进被新终端认领的会话；核对已移到
daemon 探测之后、两个分支之前。

