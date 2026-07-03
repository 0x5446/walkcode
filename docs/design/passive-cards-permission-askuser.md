# 被动交互卡实现规格：权限请求 + AskUserQuestion（claude headless）

目标：让 claude headless 会话在 turn 中途需要工具授权 / 提问时,飞书弹出交互卡,用户点击后决定回填给正在等待的 agent。对齐 V2 已验证的语义。

## 现状（已就绪的脚手架,不要重写）

- `ViewModelFactory.permission_prompt(ctx)` / `.ask_user_question_prompt(ctx)` 已产出带 token 的视图。
- `lark_cards.py` 已有 `_permission_card` / `_ask_user_question_card` 渲染(三按钮 / 单选·多选·Other)。
- `InteractionStore`（register_permission / register_ask_user_question / decide_from_token / _decide_ask_user_question / begin_awaiting_other / answer_awaiting_other）+ `HitlStore` 已存在。
- `Orchestrator._event_to_view` 已把 `AgentEventType.PERMISSION_REQUESTED` / `ASK_USER_REQUESTED` 转成卡片视图并注册 interaction+hitl。
- `Orchestrator._handle_callback_event` 已有 permission / ask_user_question 分支：permission → `transport.approve_permission(handle, rid, decision)`；ask_user → `transport.answer_user_question`。
- `ClaudeHeadlessTransport.approve_permission` / `answer_user_question` 已存在,但它们假设 client 有对应方法。

## 真正缺的（本任务核心）

**`ClaudeHeadlessTransport` 从没给 SDK 挂 `can_use_tool` 回调,所以 live claude 会话永远不会产生 PERMISSION_REQUESTED 事件**,工具被默认权限模式拒绝。而且现在 `events()` 是"把一轮消息收集成 list 再返回",无法在 turn 中途浮出权限请求。

## SDK 权限机制（已核实,claude_agent_sdk）

- `ClaudeAgentOptions.can_use_tool: Callable[[str, dict, ToolPermissionContext], Awaitable[PermissionResult]]`。
- SDK 在**独立 spawned task** 里调用 `can_use_tool`(query.py:_spawn_control_request_handler),read loop 不阻塞——所以回调内可以 `await` 一个 Future 阻塞等飞书点击,**SDK 层不会死锁**。
- 返回类型：`PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)` 或 `PermissionResultDeny(behavior="deny", message="", interrupt=False)`。
- `ToolPermissionContext` 字段：`tool_use_id`(非空,做去重键)、`suggestions`(list[PermissionUpdate],用于 always-allow)、`title`/`display_name`/`description`(优先用作提示文案)、`blocked_path`。
- `PermissionUpdate(type="addRules"|"setMode"|"addDirectories"..., rules/mode/directories/destination)`,`.to_dict()` 转协议格式。
- `can_use_tool` 要求 streaming 模式(我们就是 ClaudeSDKClient streaming),且不能与 permission_prompt_tool_name 并用。
- **AskUserQuestion**：claude 的 AskUserQuestion 是内置工具,headless 下它的“提问”也经由 can_use_tool / 工具调用路径体现。先按“它会作为一次 can_use_tool(tool_name=="AskUserQuestion") 或一次工具事件出现”来实现；若实测发现走的是普通 tool_use（answer 经 updated_input 回填),按实测调整。**这一条务必在实现时用 fake client 覆盖两种可能,并在报告里写清你假设的是哪种。**

## 设计要求

### 1. can_use_tool 桥接（transport 内）
- `_create_client` 的 `ClaudeAgentOptions` 加 `can_use_tool=self._permission_callback`(仅当 permission_mode 不是 bypass 时；若已设 acceptEdits 等,仍应挂回调,让非自动放行的工具走卡片)。
- `_permission_callback(tool_name, tool_input, ctx)`：
  1. 生成 rid（用 ctx.tool_use_id）。
  2. 造一个 `asyncio.Future` 存入 `self._pending_permissions[rid]`。
  3. 把一个 `AgentEvent(PERMISSION_REQUESTED, {...})` 放进一个**该 handle 的 asyncio.Queue**（见第 2 点），payload 带 tool_name/tool_input/rid/high_risk/suggestions/title。
  4. `await` 那个 Future，拿到决定后转成 `PermissionResultAllow/Deny` 返回给 SDK。
  5. 超时/异常保护：设一个上限（如与 stuck watchdog 对齐），超时默认 deny（fail-safe，别 fail-open 放行未授权工具）。
- `approve_permission(handle, rid, decision)`：解析 decision（action=allow/allow_once/always_allow/deny），resolve `self._pending_permissions[rid]` 那个 Future。always_allow 时把 ctx.suggestions 或兜底 addRules 放进 PermissionResultAllow.updated_permissions；并**同时写 profile 的 settings.json permissions.allow**（对齐 V2 _add_permission_rule；用 self.config_dir/settings 指向的文件；写不了就跳过不报错）。

### 2. events() 改成能中途浮出（transport 内,最难的一步）
- 现在 `events()` 收集 receive_response 到 turn 结束。改成：drain 循环里,除了 SDK 消息,还并发消费第 1 点那个 permission Queue,一旦有 PERMISSION_REQUESTED 就把它 yield/返回给上层,让 orchestrator 立即发卡；然后继续 drain（此时 SDK 那边 can_use_tool 正阻塞等 Future）。
- 关键：drain 不能因为 can_use_tool 阻塞而卡死收不到后续消息。用 `asyncio.wait([sdk_next_task, queue_get_task], FIRST_COMPLETED)` 之类的并发等待,两个来源都能推进。turn 完成（Result 消息）或权限事件都能让 `events()` 返回一批。
- orchestrator 侧 `_drain_events` 是循环调 `transport.events(handle)` 的（确认现有循环语义）；确保权限事件返回后,orchestrator 发完卡会再次调 events() 继续等——这样点击 → approve_permission resolve Future → SDK 继续 → 下一批 events 里出现工具结果 / turn 完成。
- **不要引入忙轮询**；用 asyncio 事件/Future/Queue。

### 3. 对齐 V2 的边界（见 `git show main:` 报告,以下必须覆盖）
- **write-once**：一个 rid 的决定只接受一次,双击/重放/重复回调只回显既有结论,不覆盖（InteractionStore.decide_from_token 已有 ALREADY_DECIDED,确认够用）。
- **去重**：permission 用 (session_id, tool_use_id)；AskUserQuestion 无 tool_use_id 不去重。HitlStore 已按 (session, transport_kind, transport_request_id) 幂等,复用它。
- **AskUserQuestion 三模式**：单选立即定案 / 多选 toggle+submit / Other 走“话题内下一条文本回复”回收（InteractionStore.begin_awaiting_other / answer_awaiting_other 已有,确认 lark 侧 process_lark_event 的普通文本会喂给它——这条要接通并测）。多问题时逐题推进。答案回填格式 `{questions, answers:{问题文本: label}}`,多选逗号 join。
- **超时**：agent 侧回调超时默认 deny；watchdog/interrupt 场景要能把等待中的 Future 收掉,别泄漏。
- **fail-safe 而非 fail-open**：拿不到用户决定时,权限默认 deny（与 V2 hook 的 fail-open 相反——V2 fail-open 是因为回落到终端原生 prompt；我们没有终端回落,放行=越权,所以默认 deny）。

## 约束（重要）

- **你独占这两个文件**：`src/walkcode/channel_native/__init__.py`、`src/walkcode/channel_native/lark_cards.py`,以及你新增的测试文件。主会话在并行改 `channel_native_runtime.py` 和 env,不要碰它。
- **不要重启任何 launchd 服务,不要 git commit**。做完把工作树留给主会话 review + 真机验证。
- 全程加/改单测,用 fake SDK client（模拟 can_use_tool 触发、模拟 Result 消息）覆盖：权限 allow/deny/always_allow、去重、write-once、超时默认 deny、AskUserQuestion 三模式 + Other 文本回收。跑 `uv run --with pytest python -m pytest tests/ -q` 必须全绿。
- 完成后报告：你对 AskUserQuestion 走 can_use_tool 还是普通 tool_use 的假设、events() 并发改造的具体做法、新增/改动的测试清单、任何拿不准需要主会话真机验证的点。
