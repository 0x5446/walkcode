# ADR 0063 — codex 的 item 类型按 schema 穷举分类，不靠词根猜

- 状态：Accepted
- 日期：2026-08-08
- 版本：v0.14.22
- 相关：[ADR 0062](0062-surface-codex-turn-errors.md)（未知事件留痕）、
  [ADR 0055](0055-mid-turn-narration-on-progress-card.md)（工具进度卡）、
  [ADR 0060](0060-codex-resident-event-listening.md)（codex 常驻跨回合监听）

## 背景：ADR 0062 的日志立刻兑现了它的价值

ADR 0062 加的 `codex_event_type_unhandled` 上线当天就照出一片盲区：

```
walkcode degrade=codex_event_type_unhandled event_type=item/started
walkcode degrade=codex_event_type_unhandled event_type=item/completed
```

第一眼像是工具卡整体没接。实际不是——`item/started` / `item/completed` 在打这行
日志之前先过 `_codex_tool_event()`，多数 tool-like 的 item 都正常出卡。日志只打了
一次，是因为去重键就是外层事件名：某一次 `item/started` 裹的是 `agentMessage`，
打了一行，这个键从此被占住，后面所有 item 子类型——包括真正漏接的那些——再没机会
留痕。

**去重是对的（ADR 0062 防刷屏），但键取错了粒度。`item/started` 不是类型，是信封。**

## 真正漏掉的是什么

原判定 `_codex_tool_like_name` 用词根子串：`tool` / `function` / `command` /
`exec` / `shell` / `bash`。拿 `codex app-server generate-json-schema` 生成的
`ThreadItem` 全量 18 个变体一比，漏了两类真·工具活动，还错了一处命名：

| item type | 词根命中 | 后果 |
| --- | --- | --- |
| `webSearch` | 无 | 服务端执行的联网搜索完全没有卡片 |
| `fileChange` | 无 | 改文件没有卡片（审批走另一条通道，成交后无声） |
| `mcpToolCall` 等 | 命中 `tool` | 出卡，但名字取不到——它们把工具名写在 `tool` 字段，而兜底链只认 `toolName`/`name`，于是卡片一律叫 "tool" |

`webSearch` 的漏洞此前看不出来，因为 codex-personal 的自定义 model catalog 把
`gpt-5.6-sol` 的 `tool_mode = "code_mode_only"` 深拷贝进了 Command Code 条目，
Codex 请求体里连 `tools` 字段都没有，`web_search` 从来没被下发过。两个 bug 互相
遮蔽。

## 决定

### 1. item 类型走穷举表，词根只留给旧事件名

```python
_CODEX_TOOL_ITEM_SPECS: dict[str, _CodexToolItemSpec] = {
    "commandexecution":    (…, lambda p: p.get("command")),
    "mcptoolcall":         (…, lambda p: p.get("arguments")),
    "dynamictoolcall":     (…, lambda p: p.get("arguments")),
    "collabagenttoolcall": (…, lambda p: p.get("prompt")),
    "websearch":           ("web_search",   lambda p: p.get("query")),
    "filechange":          ("apply_patch",  _codex_file_change_summary),
}
```

每个 schema 变体要么在这张表里，要么在测试的「不是工具活动」清单里，二者必居其一。
守卫是两段的，缺一不可：

- `tests/data/codex_thread_item_variants.json` 是**从二进制生成的**变体快照
  （`codex app-server generate-json-schema` → `ThreadItem`），不是手写清单。
- `test_codex_thread_item_snapshot_matches_installed_codex` 在本机重新生成一次并与
  快照比对（没装 codex 的环境 skip）；
  `test_codex_tool_item_specs_cover_every_schema_variant` 拿快照做分割断言。

两段合起来才成立：只写死一份手抄清单的话，升级 codex 不会改变它，测试照样全绿——
"新增变体会让测试挂"就成了一句假话。这条机械保障是本 ADR 的核心，只列白名单而没有
守卫，等于把同一个坑推到下一次升级。

**不往词根表里塞 `search` 或 `file`**。这两个词在 codex 的通知里另有主人：
`fuzzyFileSearch/sessionCompleted` 是文件选择器的自动补全推送，子串会命中
`search` + `completed`，用户每敲一个字符收一张卡。

词根探测 `_codex_tool_like_name` 保留且仍然作用于**所有事件名**（`event_msg/*` 的
旧类型和 `item/*` 的方法名都过它）——它是没有 `item` 结构那条路径的唯一判据。变的是
`item.type`：那一维已经完全由穷举表决定，不再受词根影响。

### 2. 摘要在三个分支之间共用

进度卡按 `tool_id` upsert（ADR 0055），所以完成事件的摘要会**覆盖**开始事件显示
的内容。原完成分支是 `payload.get("summary") or "Tool completed"`，而 `webSearch`
的完成 item 只有 `query` 没有 `summary`——搜索一结束，用户正在看的查询词就被换成
一句"Tool completed"。

现在 spec 的 summary 只算一次，started / completed / failed 三个分支共用。失败卡
也带上它：「哪个补丁被拒了」是第一个要问的问题。

### 3. item 的 `status` 优先于方法名

codex 把**被拒绝**的补丁也报成 `item/completed`，只是 `status: "declined"`。只看
方法名里的 "completed"，用户拒绝的改动会显示成一张绿色的成功卡。现在先查
`_CODEX_TOOL_STATUS_STATES`（含 `declined` → failed），查不到再退回方法名。

### 4. 未知事件日志的去重键加上 item 类型

`item/started` + `agentMessage` 不该把 `item/started` + 任何新工具类型一起消音。
键改成 `事件名/item 类型`，日志同时打出两个字段。刷屏防护不变（同一子类型仍然只打
一次），但覆盖率重新可见——这正是 ADR 0062 想要的东西。

### 5. `fileChange` 摘要出路径 + 数量，不出 diff

`changes[].diff` 是完整补丁，和 command 的 output 一样不进卡片。批量改动只列前 5
个路径，其余写成 `(+45 more, 50 files)`——直接截断会把路径切成半截，还看不出这次
到底动了多少文件。

## 影响

- 联网搜索、改文件、MCP 工具调用在频道里都可见且有正确名字。
- `fuzzyFileSearch/*` 明确不出卡（有回归测试钉住）。
- codex 升级新增 item 变体 → 单测挂，而不是线上静默丢卡。
- 未映射的变体不是"漏了"，是显式登记在测试 `not_tool_activity` 清单里的有意排除，
  分两类：
  - **已有别的渲染路径**：`agentMessage`（`item/agentMessage/delta` → TURN_DELTA）、
    `userMessage`（用户自己发的）。
  - **目前没有面向用户的形态**：`reasoning`、`plan`、`hookPrompt`、
    `subAgentActivity`、`imageGeneration`、`imageView`、`sleep`、
    `enteredReviewMode` / `exitedReviewMode`、`contextCompaction`。这些确实还没接，
    要接时按本 ADR 的方式加表项 + 摘要，别动词根表。

## 验证

**真实环境（AGENTS.md 要求）**：

```
WALKCODE_CODEX_SANDBOX=danger-full-access \
WALKCODE_ENV_FILE=~/.walkcode/personal-codex.env \
python scripts/channel_native_debug.py agent-smoke --live --agent codex --json \
  --prompt "用 apply_patch 新建文件 /tmp/wc-e2e-filechange.txt……"
```

真实 codex app-server（0.144.5）+ 真实 provider，实测：

```json
"event_types": ["tool.started", "tool.completed", "turn.delta", "turn.completed"],
"tool_events": [
  {"kind": "started",   "tool_name": "apply_patch", "summary": "/tmp/wc-e2e-filechange.txt"},
  {"kind": "completed", "tool_name": "apply_patch", "summary": "/tmp/wc-e2e-filechange.txt"}
]
```

文件落盘内容正确，摘要是路径不是补丁体。改动前同一条提示词只产出 `turn.delta` +
`turn.completed`，零工具事件。

**`webSearch` 仍未跑到真实回合**，如实记录。它需要 provider 服务端执行
`web_search`，本机三条路都不产出该 item：Command Code（经 codex-relay 转 Chat
Completions）没有服务端工具；Azure 部署实测不触发（模型直接凭记忆作答）；DeepSeek
官方直连支持，但本机没配 `DEEPSEEK_API_KEY`。因此 `webSearch` 的验证到三层为止：
本机 codex 自己生成的 `ThreadItem` schema（`{id, query, action?}`，`query` 必填）、
照该形态写的单测、以及会随 codex 升级失效的穷举守卫测试。补上 key 后应当补一次真实
回合。这一条是本次变更**唯一**没有真实报文背书的路径。

**冒烟脚本自身**：`agent-smoke --live --agent codex` 此前必抛
`'async_generator' object is not iterable`——codex 的 `events()` 是异步生成器，脚本
只处理了 awaitable 分支；而 ClaudeHeadlessTransport 返回的是「解析出异步生成器的
协程」，所以必须**先 await 再判流**。修复同时收紧了成功判定：

- 超时不再被吞掉，整轮收集用一个绝对截止时间（原先每条事件重置计时，滔滔不绝的
  回合可以无限超时，单次慢工具又会提前掐断监听）。
- 没收到 `turn.completed` / `SESSION_ERROR` 就不算通过，返回 `ok: false` +
  `drain_error`。

这一条是发版门禁的真实验收入口，假绿比崩溃更糟。上面那次 `webSearch` 尝试正好撞上
新判定，如实报了
`{"ok": false, "drain_error": "timed out after 180s without a turn-closing event"}`；
修复前它会报 `ok: true, event_count: 0`。
