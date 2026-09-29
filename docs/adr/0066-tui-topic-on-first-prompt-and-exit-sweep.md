# ADR 0066: TUI 话题在第一句话时才建；TUI 退出按周期检测

Date: 2026-09-29

Status: Proposed

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
话题照旧）。建会话只由 `user-prompt-submit` 与 `pre-tool` 触发；标题照旧取第一句
输入。空会话不发任何消息。codex 0.157 的 SessionStart 本就在第一轮才触发，行为
不变。

### 2. 周期检测 TUI 进程退出

新增维护任务，每 30 秒一次：取所有"未停止、写者为 external_tui、带进程记录"的
会话，**合并成一次** `ps -o pid=,stat=,lstart= -p a,b,c`：

- pid 不在输出里、或是僵尸 → 已退出；
- pid 在但启动时间与记录不符 → pid 被复用，按已退出处理；
- `ps` 本身失败（超时、退出码 >1、输出解析不了）→ 本轮跳过，不下结论。

判定已退出的会话走现有的 `_maybe_mark_stale_tui_process_detached` 路径（含
daemon worker 仍活着时只记"已分离"的保护），标"已结束"并刷新状态卡。同一套
判定也替换 `_process_ref_is_running`，启动扫描随之受益。已停止的会话不再检查，
不会重复处理；每轮之间固定等待，不会空转。

### 3. daemon socket 不存在即"worker 不在"

`job_alive` 连接失败时区分两种情况：socket 文件不存在 → daemon 没运行，worker
不可能在，返回 False；文件在但连不上（daemon 重启中）→ 仍返回 None（未知），
沿用旧标记。

## 不在本次范围

- 同一个 Claude 进程里 `/clear` 或 `/resume` 到别的会话后，旧会话的进程仍活着，
  话题仍"运行中"。需要按 session id 判定"当前会话"，另议。
- 现存的空话题（进程仍活着的那几个）不补救：进程退出后由第 2 条自动收尾。

## Consequences

- 每 30 秒一次 `ps`（与会话数无关），1 秒超时。
- 关掉 TUI 后约 30 秒话题变"已结束"，接管按钮随之失效。
- 打开 TUI 不说话不再产生话题；第一句话前的 TUI 在频道里不可见、不可接管
  （此时也没有可接管的内容）。
- 回滚：第 1 条恢复 `session-start` 建会话；第 2 条删除维护任务；第 3 条恢复
  回落旧标记。三者相互独立。
