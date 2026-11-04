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
   - `/clear` `/new` → 在同一 cwd 下 `thread/start` 新线程，原地改写 handle，
     关闭旧线程（中断正在跑的 turn），模型选择带到新线程。编排器在 submit 返回后
     发现 `thread_id` 变了，就立即写回 `session.transport_ref`。不靠 drain 的完成
     事件，因为已挂着的 drain 会吞掉它。
   - `/review [重点]`、`/init` → 发 TUI 的同款提示词，走普通 turn。
     **不用 `review/start`**，因为它的 turn 不会结束，会把会话挂到静默上限（1 小时）。
2. **`/model` `/effort` 对 Codex 真正生效**：`set_model=True`，选择存在
   `_thread_overrides`，每次 `turn/start` 都带上。切换前先用 `model/list` 校验，
   因为覆盖值会被持久化，打错的名字会让这个线程之后每一轮都失败。`model/list`
   调不通时跳过校验。`/model` 的列表优先实时查 `model/list`，失败才退回本地文件。
3. **WalkCode 代答**：`/diff`（两个 agent 都支持，在会话目录跑 `git status` 和
   `git diff HEAD`）；Codex 的 `/effort`、`/mcp`（`mcpServerStatus/list`）。
   Claude 的 `/effort` `/mcp` 继续交给 Claude 自己处理。
4. **Codex 终端专用命令直接拒绝**（`/login` `/logout` `/quit` `/approvals`
   `/resume` ...）：回一句"聊天里用不了"，不再发给模型。
5. **菜单只列真能执行的命令**，Claude 的列表按上面的 headless 实测结果来定。
6. **执行成功但没有输出的命令**（Claude `/clear`、Codex `/compact`）回
   "✅ /cmd 已执行。"，不报 EMPTY_TURN_NOTICE。判断依据是重放暂存里最后一条提交
   是不是斜杠命令。

## Consequences

- Claude Code 或 Codex 升级以后，菜单可能和实际支持的命令对不上，要按上面的方法重测。
- `/review` 在 Codex 里是普通 turn，不进入 review 模式，界面上没有 review 模式特有的
  条目，但审查结果一样。等 `review/start` 能正常结束再考虑换回去。
- 压缩期间如果 TUI 正好在同一线程上开新 turn，那个 turn 可能被当成压缩 turn 认领。
  这需要共享 daemon 加上两边同时操作才会发生，暂时接受。
- `/status` 不加 Codex 额度显示：`account/rateLimits/read` 只对 ChatGPT 登录的账号有
  数据，API key 或代理 provider 读不到。
