# Deep Review — PR #90 / v0.14.22（codex item 类型工具卡）终版

- Repo: `/Users/alpha/workspace/walkcode`
- HeadSHA: round 1 `219268e` → round 2 `5d52d02` → 修复后 `3500a61`
- RunDir: round 1 `…/deep-review-walkcode-219268e-1786121146.CsIw`；
  round 2 `…/deep-review-walkcode-5d52d02-1786123429.BDzN`
- Host: claude / Engine: **codex `gpt-5.6-sol`，effort xhigh**（跨模型，非自审）
- 维度（mixed，8）：correctness、goalfit、maintainability、conventions、
  consistency、feasibility、alignment、concurrency（并发信号命中：异步生成器）
- cursor-agent: 未启用（composer-2.5 smoke 失败，已跳过）

## VERDICT: SAFE（两轮均无 Critical；高共识 Warning 全部修复或显式归档）

## Round 1（8 维 / 22 条）—— 已修

| # | 命中维度 | 问题 | 处理 |
|---|---|---|---|
| 1 | correctness · goalfit · alignment · consistency | `webSearch` 完成事件丢摘要：进度卡按 `tool_id` upsert，兜底的 "Tool completed" 覆盖开始事件的 query | spec 摘要只算一次，started/completed/failed 三分支共用 |
| 2 | goalfit · alignment · conventions · feasibility | 「词根 + 两个例外」与文档宣称的「按 schema 显式列举」不符 | 换成穷举表 `_CODEX_TOOL_ITEM_SPECS`；词根只决定事件名那一维 |
| 3 | correctness | `declined` 的补丁被判成完成卡 | item `status` 优先于方法名，`declined` 归 failed |
| 4 | correctness · concurrency · goalfit · feasibility | `agent-smoke` 超时被吞成 `ok=true`（发版门禁的验收入口自己坏了） | 整轮绝对截止时间、超时不吞、无终止事件即 `ok=false` + `drain_error` |
| 5 | consistency | `ClaudeHeadlessTransport.events()` 是「解析出异步生成器的协程」，先判流会漏 | 先 await 再判 `__aiter__` |
| 6 | consistency（存量） | 未知事件日志按外层事件名去重，`item/started` 裹 `agentMessage` 一次就把整个信封消音 | 去重键加 `item.type`，两个字段都打进日志 |
| 7 | goalfit | `mcpToolCall` 等把工具名写在 `tool` 字段，卡片一律叫 "tool" | 兜底链补上 `tool` |
| 8 | feasibility | 批量 `fileChange` 摘要中间截断 | 前 N 个路径 + `(+N more, M files)` |

## Round 2（8 维 / 13 条）—— 已修

| # | 命中维度 | 问题 | 处理 |
|---|---|---|---|
| 1 | 六维共同命中 | **round 1 的漂移守卫是假绿**：变体全集写死在测试里，升级 codex 不会改变它 | 快照改为 `codex app-server generate-json-schema` 生成并入库；新增测试在本机重新生成比对（无 codex 则 skip） |
| 2 | 四维命中 | `(+N more)` 追加在 160 字预算之外，长路径下连同文件总数一起被截掉 | 在预算内递减路径条数计算；抽出 `_TOOL_SUMMARY_LIMIT` |
| 3 | alignment | 通用 `summary` 字段优先级高于类型摘要，上游塞补丁正文可绕过「只出路径」 | 类型摘要优先 |
| 4 | correctness | `aclose()` 无界等待会越过刚强制的截止时间 | 5 秒 `wait_for` 包住 |
| 5 | alignment（Suggestion） | ADR/AGENTS.md 称词根「只服务旧 event_msg 事件名」，实际对所有事件名都跑 | 措辞改准 |
| 6 | alignment（Suggestion） | 未映射清单未区分「已有渲染路径」与「还没接」 | 分两类列出 |

## 存量问题（PreExisting，单独归档，不算本次变更的账）

- `debug_agent_smoke` 结束后不显式关闭 handle，codex app-server 子进程要等命令行
  进程退出才回收（concurrency round 1 #2 / round 2 #1，Confidence 0.96）。
- `item/mcpToolCall/progress` 会产生一张名字与摘要都不准的重复 TOOL_STARTED 卡
  （goalfit round 2，Confidence 未验证）。
- `upgrade.sh` 不支持指定旧版本安装，回滚只能手工（feasibility round 2，Suggestion）。

## 已接受的验证缺口

`webSearch` 没有真实回合报文。三条 provider 实测均不产出该 item：Command Code
（经 codex-relay 转 Chat Completions，无服务端工具）、Azure 部署（不触发，模型凭
记忆作答）、DeepSeek 官方直连（支持，但本机无 `DEEPSEEK_API_KEY`）。验证到
schema + 单测 + 漂移守卫三层，已写进 ADR 0063 的验证一节。这是本次唯一没有真实
报文背书的路径。

## 真实环境验收

- `agent-smoke --live --agent codex` × 2 轮（apply_patch / shell），真实 codex
  app-server 0.144.5，出卡含 started / completed / **failed** 三态，名字与摘要正确。
- 反向证据：`webSearch` 那次尝试撞上新判定，如实报
  `{"ok": false, "drain_error": "timed out after 180s without a turn-closing event"}`；
  修复前它会报 `ok: true, event_count: 0`。
- 突变验证：删 `websearch` spec、拿掉完成分支摘要、往变体快照加假类型——对应测试
  都会挂，不是假绿。
- 全量单测 1119 passed。
