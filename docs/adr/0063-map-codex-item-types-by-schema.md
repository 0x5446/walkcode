# ADR 0063 — codex 的工具类事件按 schema 列举，不靠词根猜

- 状态：Accepted
- 日期：2026-08-08
- 版本：v0.14.22
- 相关：[ADR 0062](0062-surface-codex-turn-errors.md)（未知事件留痕）、
  [ADR 0055](0055-mid-turn-narration-on-progress-card.md)（工具进度卡）

## 背景：ADR 0062 的日志立刻兑现了它的价值

ADR 0062 加的 `codex_event_type_unhandled` 上线当天就照出一片盲区：

```
walkcode degrade=codex_event_type_unhandled event_type=item/started
walkcode degrade=codex_event_type_unhandled event_type=item/completed
```

第一眼像是工具卡整体没接。实际不是——`item/started` / `item/completed` 在打这行
日志之前先过 `_codex_tool_event()`，绝大多数 tool-like 的 item 都正常出卡。日志只
打了一次，是因为 `_log_unhandled_event_type` 按类型每进程去重（ADR 0062 的设计，
防止 token_count 之类刷屏）：某一次 `item/started` 裹的是 `agentMessage`，打了一行，
这个类型从此被拉黑，后面成千上万个 tool-like 的 item 再没机会留痕。

**去重是对的，但它让"这个类型有时接得住、有时接不住"变得不可见。**

## 真正漏掉的是什么

`_codex_tool_like_name` 用词根子串判定：`tool` / `function` / `command` / `exec` /
`shell` / `bash`。拿 `codex app-server generate-json-schema` 生成的
`ThreadItem` 全量变体一比（18 个），漏了两个真·工具活动：

| item type | 词根命中 | 后果 |
| --- | --- | --- |
| `webSearch` | 无 | 服务端执行的联网搜索完全没有卡片 |
| `fileChange` | 无 | 改文件没有卡片（审批走另一条通道，成交后无声） |

`webSearch` 的漏洞此前看不出来，因为 codex-personal 的自定义 model catalog 把
`gpt-5.6-sol` 的 `tool_mode = "code_mode_only"` 深拷贝进了 Command Code 条目，
Codex 请求体里连 `tools` 字段都没有，`web_search` 从来没被下发过。两个 bug 互相
遮蔽。

## 决定

### 1. 词根表不动，另加一份按 schema 列举的 item type 白名单

```python
_CODEX_TOOL_LIKE_ITEM_TYPES = frozenset({"websearch", "filechange"})
```

**不往词根表里塞 `search` 或 `file`**。这两个词在 codex 的通知里另有主人：

- `fuzzyFileSearch/sessionCompleted` —— 文件选择器的自动补全推送，不是 agent
  行为。子串命中 `search` + `completed`，会变成 TOOL_COMPLETED 卡片，用户每敲
  一个字符收一张卡。
- `item/fileChange/outputDelta`、`item/fileChange/patchUpdated` —— 增量推送，
  不该当成独立的工具调用。

白名单只对 item type 精确匹配，事件名那条路径仍走词根，行为不变。

### 2. 卡片名和摘要按 item 形态补齐

两个 item 都既没有 `name` 也没有 `command`，落进兜底就叫 "tool"、摘要为空——
接住了跟没接住看起来一样。

- `webSearch { id, query, action? }` → 名字 `web_search`，摘要取 `query`。
- `fileChange { id, changes[], status }` → 名字 `apply_patch`，摘要取
  `changes[].path` 拼接。**只出路径，不出 diff**：`changes[].diff` 是完整补丁，
  和 command 的 output 一样不进卡片（`_codex_file_change_summary`）。

### 3. 新增事件类型一律先查 schema

`codex app-server generate-json-schema --out <dir>` 是权威来源。往
`_CODEX_TOOL_LIKE_ITEM_TYPES` 加东西之前先跑它，不要从日志里的类型名倒推——
日志只在去重前打过一次，看不出覆盖率。

## 影响

- 联网搜索和改文件在频道里可见，与 exec/MCP 调用一致。
- `fuzzyFileSearch/*` 明确不出卡（有回归测试钉住）。
- 仍未映射的 item type：`imageGeneration`、`imageView`、`sleep`、
  `subAgentActivity`、`plan`、`enteredReviewMode` / `exitedReviewMode`、
  `contextCompaction`。它们不是"漏了"，是当前没有面向用户的形态；真要接时按本
  ADR 的方式加白名单 + 摘要，别动词根表。

## 验证

**真实环境（AGENTS.md 要求）**：`WALKCODE_CODEX_SANDBOX=danger-full-access
WALKCODE_ENV_FILE=…/personal-codex.env python scripts/channel_native_debug.py
agent-smoke --live --agent codex`，提示词要求用 apply_patch 建文件。真实
codex app-server（0.144.5）+ 真实 provider，实测：

```
event_types: [tool.started, tool.completed, turn.delta, turn.completed]
tool_events:
  started   apply_patch  /tmp/wc-e2e-filechange.txt
  completed apply_patch  /tmp/wc-e2e-filechange.txt
```

文件落盘内容正确，摘要是路径不是补丁体。改动前同一条提示词只产出
`turn.delta` + `turn.completed`，零工具事件。

**`webSearch` 未能跑真实回合**，如实记录：它需要服务端执行 `web_search` 的
provider。Command Code（经 codex-relay 转 Chat Completions）没有这个能力；
DeepSeek 官方直连有，但本机没配 `DEEPSEEK_API_KEY`。因此 `webSearch` 的验证到
两层为止：本机安装的 codex 自己生成的 `ThreadItem` schema（`{id, query, action?}`，
`query` 为 required），以及照该形态写的单测。补上 key 之后应当补一次真实回合。

**顺带修好的 E2E 工具**：`agent-smoke --live --agent codex` 此前必抛
`'async_generator' object is not iterable`——codex/external-TUI 的 `events()` 是
异步生成器，脚本只处理了 awaitable 分支。现在按回合结束事件收敛地排空，并在
输出里带上 `tool_events`（卡片的 name/summary），否则"接住了"和"接住了但叫
tool、摘要为空"在 `event_types` 里长得一模一样。
