# ADR 0071: agent 原生斜杠命令在频道里真正执行

Date: 2026-11-04

Status: Accepted

## Context

用户在飞书里发 `/model`，看到 "Model switching: not available"，列表里只有一个模型。
排查后发现问题不止 `/model`：

- **Codex**：`CodexAppServerTransport.capabilities()` 写死 `set_model=False`，
  `set_model()` 直接抛错。模型列表只读 `$CODEX_HOME/models_cache.json`，work profile
  里没有这个文件。更大的问题是，除了 WalkCode 自己的命令，其余斜杠命令
  （`/compact` `/review` `/new` `/clear` `/init` ...）都被当成普通文字发给
  app-server。Codex TUI 的斜杠命令全在客户端实现，app-server 不认，模型只是把
  "/compact" 当成一句话来回答。菜单里却列着这些命令。
- **Claude**：命令原样交给 Claude Code，headless 模式确实会执行一部分。但菜单是照抄
  TUI 的：`/vim` `/login` `/resume` `/permissions` `/memory` `/help` 等在 headless 下
  只会回 "isn't available in this environment"，而能用的 `/context` `/usage`
  `/effort` 没列出来。`/clear` 的结果为空，WalkCode 会报
  EMPTY_TURN_NOTICE（"代理没有返回任何内容"），让人以为上游坏了。

实测依据（2026-11-04，codex-cli 0.166.0，Claude Code 2.1.317）：

- app-server 有 `model/list`；`turn/start` 的 `model` / `effort` 覆盖值对之后所有
  turn 生效，并写进 rollout。重启 app-server 后 `thread/resume` 回报的仍是切换后的
  模型。
- `thread/compact/start` 的响应是空的，压缩 turn 的 id 要等它的 `turn/started`
  才出现，大约 4 秒后完成。
- `review/start` 把审查结果（`exitedReviewMode`）推出来以后，turn 一直不结束，
  8 分钟内都没有 `turn/completed`。
- `claude -p "/<name>"` 逐个跑过菜单里的命令，结果按"能用 / not available"分类。

## Decision

1. **Codex 的命令在 transport 层翻译**，仍走普通提交流程，进度卡和结束通知不用另写：
   - `/compact` → `thread/compact/start`。线程在 drain 认领压缩 turn 之前一直留在
     `_pending_starts`（未知 turn 暂缓），所以 TUI 镜像不会把这个 turn 抢走；
     drain 收到它的 `turn/started` 后再认领。
   - `/review [重点]`、`/init` 见下一条。
2. **`/clear` `/new` 不是 turn，由 WalkCode 处理**（`Orchestrator.start_new_agent_thread`
   → `CodexAppServerTransport.start_new_thread`）：在同一 cwd 下 `thread/start` 新线程，
   关闭旧线程，把新的 `handle_id` 和 `thread_id` 一起写进 `session.transport_ref`。
   换 `handle_id` 是因为 drain 按它去重和设防，旧线程的 drain 认不了新线程。
   回合进行中直接拒绝（同 `/reload`）：否则要么旧回合被截断、没有回复，要么中断失败，
   旧任务和新线程同时改文件。模型沿用 `_thread_overrides`，没有时用会话持久化的
   `session.model`，避免 runtime 重启后丢失选择。
   新线程在跑第一个 turn 之前没有 rollout，`thread/resume` 会报 "no rollout found"
   （0.166 实测，同一个 server 上也一样）。所以 transport 记着本次进程里还没跑过 turn 的
   线程，恢复时直接复用；重启后遇到这个错误就新开一个线程，反正里面本来就没有对话。
3. `/review [重点]`、`/init` → 发 TUI 的同款提示词，走普通 turn。重点可以写在同一行，也可以换行写。
     **不用 `review/start`**，因为它的 turn 不会结束，会把会话挂到静默上限（1 小时）。
4. **`/model` `/effort` 对 Codex 真正生效**：`set_model=True`，选择存在
   `_thread_overrides`，每次 `turn/start` 都带上。切换前先用 `model/list` 校验，
   因为覆盖值会被持久化，打错的名字会让这个线程之后每一轮都失败。`model/list`
   调不通时：模型名跳过校验；推理强度退回到已知的取值集合
   （none/minimal/low/medium/high/xhigh/max）。`/model` 的列表优先实时查 `model/list`，失败才退回本地文件。
5. **WalkCode 代答**：`/diff`（两个 agent 都支持：已暂存、未暂存和未跟踪文件的内容都显示；
   还没有提交的仓库改为对比暂存区；git 带 `core.fsmonitor=false`、`--no-ext-diff`、
   `--no-textconv`，仓库配置不能借 `/diff` 执行程序）；Codex 的 `/effort`、`/mcp`（`mcpServerStatus/list`）。
   Claude 的 `/effort` `/mcp` 继续交给 Claude 自己处理。`/diff` 和 `/mcp` 会返回本机代码和
   服务器状态，只有会话主人或管理员能用（和 `/model`、`/reload` 同一道门槛）。
6. **Codex 终端专用命令直接拒绝**（`/login` `/logout` `/quit` `/approvals`
   `/resume` ...）：回一句"聊天里用不了"，不再发给模型。
7. **菜单只列真能执行的命令**，Claude 的列表按上面的 headless 实测结果来定。
8. **执行成功但没有输出的命令**（Claude 的 `/clear` `/compact`、Codex 的 `/compact`，
   列在 `_SILENT_COMMANDS`，按 transport 区分）回 "✅ /cmd 已执行。"，不报
   EMPTY_TURN_NOTICE。判断依据是重放暂存里最后一条提交。其他斜杠输入结果为空时，
   照旧报 EMPTY_TURN_NOTICE。

## Consequences

- Claude Code 或 Codex 升级以后，菜单可能和实际支持的命令对不上，要按上面的方法重测。
- `/review` 在 Codex 里是普通 turn，不进入 review 模式，界面上没有 review 模式特有的
  条目，但审查结果一样。等 `review/start` 能正常结束再考虑换回去。
- 压缩期间如果 TUI 正好在同一线程上开新 turn，那个 turn 可能被当成压缩 turn 认领。
  这需要共享 daemon 加上两边同时操作才会发生，暂时接受。
- `/status` 不加 Codex 额度显示：`account/rateLimits/read` 只对 ChatGPT 登录的账号有
  数据，API key 或代理 provider 读不到。
