# ADR 0061 — 回合永不静默收尾；提交给 agent 的输入永不为空

- 状态：Accepted
- 日期：2026-08-07
- 版本：v0.14.20
- 相关：[ADR 0055](0055-mid-turn-narration-on-progress-card.md)（narration 进工具卡）、
  [ADR 0060](0060-codex-resident-event-listening.md)（codex 常驻跨回合监听）

## 背景：一条空消息让整条 thread 报废五个小时

2026-08-07 事故链，四层各自"优雅降级"，叠起来是彻底静默：

1. 飞书一条**纯图片消息**（无文字）进来。`CodexAppServerTransport.submit_turn`
   直接用 `turn.text`，既没拼附件路径也没拦空串 → codex 收到
   `{"type": "text", "text": ""}`，图片被丢弃。
2. 这条空 user message 永久留在 codex thread 历史里。codex 每个回合重放整段历史。
3. codex-relay 把 Responses 转 Chat Completions 时原样保留空 content，
   上游 Command Code 返回 `400 user message must have content`
   （`param=messages.510.content`）。**每个后续回合都 400**。
4. relay 回 HTTP 200 + `response.failed`；codex 重试 6 次后发
   `task_complete{last_agent_message: null}`，**不产生任何 error 事件**。
5. `Orchestrator._drain_events` 里 `if not visible_text: continue`
   把这个空完成事件丢掉 → 飞书话题里什么都没有。

用户看到的：4:06 一条"现在跑本地 upgrade"，然后连续 5 小时静默，没有任何错误。

## 决定

### 1. 提交给 agent 的文本永不为空

`_compose_turn_text()`（`channel_native/__init__.py` 模块级，三条 transport 共用）
是唯一入口：附件的本地绝对路径拼进 prompt；文本与附件都为空时退化为
`EMPTY_TURN_PLACEHOLDER`。`CodexAppServerTransport.submit_turn` 在拼完环境上下文后
再兜一次底。

空串不是"没内容"，是**会把 thread 写坏的毒丸**——它进历史后不可自愈，只能新开会话。

### 2. 整轮零输出必须在频道里说出来

`_drain_events` 跟踪 `turn_produced_output`：本回合有没有任何东西到达频道
（正文气泡、工具进度卡、narration 都算）。回合结束时若为假，投递
`EMPTY_TURN_NOTICE` 并记 `_log_degrade("turn_completed_without_output")`。

配套两条，缺一不可（都是事故的同类静默）：

- 可见性判断用 `visible_text.strip()`：纯空白的正文既不该当作输出，也不该
  变成一个空气泡。
- 去重水位 `last_visible_text` **按回合作用域**，回合结束即清空。
  它原本活满整条排水循环，而 codex 的监听器是跨回合常驻的（ADR 0060）——
  连续两个回合回答同一句话时，第二句会被当成重复直接丢掉。

### 3. upgrade 只装 Release，解析不到就停

`upgrade.sh` / `walkcode upgrade` 按序尝试三个来源，全部产出必须匹配
`^v[0-9]+\.[0-9]+\.[0-9]+$`（结果会进 `uv tool install` 的 shell 命令）：

1. `gh api repos/<repo>/releases/latest`（已认证，唯一知道 GitHub 认定哪个是 latest 的来源）
2. 匿名 releases API
3. `github.com/<repo>/releases/latest` 的 302 跳转（无 API 无限流）

**不用 `git ls-remote` 列 tag**：`release.sh` 先推 tag 再建 Release，
`gh release create` 失败会留下没有 Release 的 tag，装上它就违反了
AGENTS.md 的"upgrade 拉的是 Release"。

三源全失败 → 直接失败退出。此前这里静默回落到"从 main 装"，
等于把"升级到发布版"变成"装 main 上的任意代码"。
需要装 main 时显式设 `WALKCODE_ALLOW_MAIN=1`。

## 影响

- 频道里会多出 ⚠️ 提示。判据是"整轮零输出"，不是"完成事件为空"——
  正常回合（正文走 delta、完成事件为空）不受影响。
- 纯附件消息现在能送达 agent（以本地路径形式）。codex 侧沙箱是
  `WALKCODE_CODEX_SANDBOX` 配置的值，路径可读性由它决定。
- 匿名 API 被限流（403）时 upgrade 仍可用；GitHub 全挂时 upgrade 会失败而不是乱装。

## 验证

- `tests/test_channel_native_silent_turn.py`：零输出、有 delta、有工具卡、
  纯空白、同一回合内重复、连续两回合同文、**以及经
  `CodexAppServerTransport` 真实事件转换**（`event_msg/task_complete` 与
  JSON-RPC `turn/completed` 两种线格式）。
  只用 `FakeAgentTransport` 不足以证明协议路径——见
  `docs/review/.review-learnings.md` 2026-08-03 条。
- `tests/test_channel_native_codex.py`：附件路径进 prompt、空文本兜底、
  带环境上下文时仍非空。
- `tests/test_upgrade.py` / `tests/test_release_scripts.py`：三源顺序、
  跳转解析、非 semver 拒绝、无 tag 时拒装、`WALKCODE_ALLOW_MAIN` 覆盖。
- relay 侧另在 fork（`0x5446/codex-relay`，`fix/drop-contentless-messages`）
  丢弃空 content 的 user/system 消息，让已中毒的 thread 也能恢复。
