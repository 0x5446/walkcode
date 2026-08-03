# Deep Review 综合结论

VERDICT: NEEDS_FIX

**轮次**：1 / 2
**类型**：code

> 范围：给飞书话题根一个有意义的标题。(1) TUI observed 的 Lark 话题根从纯文本换成 health 卡片，根即 status card，可持续 patch；(2) 新增统一入口 `Orchestrator._maybe_refresh_session_title`，四条 turn 结束路径（claude TUI hook / codex TUI hook / codex app-server 事件流 / claude headless 事件流）都汇入，用来源分级 + 同级节流决定标题覆盖；附带把 `_status_card_fingerprints` 的 key 从 `session_id` 改成 `(message_id, fingerprint)`。
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto；模型：gpt-5.6-sol effort=xhigh）
> Cursor：disabled（composer-2.5 smoke test failed）
> 维度：基础 4（correctness / goalfit / maintainability / conventions）+ 信号触发 1（concurrency，首轮 5 并发时 exit=124 超时，单独重跑 exit=0，产物完整）
> Phase 2 验证：3 条已派（0 条跨引擎共识免验）；结果 3 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 9273576cb35957ec67e9e6294a3b9a0c3e1e14c6
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-9273576-1785743106.G7Kk
> 规模：954 行 / 5 文件（含 untracked 新增测试文件）
> 模式提示：默认报告模式（未 --fix，未动文件）

## 🔴🔴 顶级必修

### 1. [Warning] src/walkcode/channel_native/__init__.py:11934-11937 (Symbol: 事件消费循环中的 TURN_COMPLETED 标题刷新调用)

> **一句话**：应用服务器的正常完成事件不会生成会话标题

- **Category**: Bug
- **Confidence**: dim-correctness 0.99, dim-goalfit 0.99, dim-maintainability 0.99
- **来源**: correctness + goalfit + maintainability（3 来源命中，同引擎多维度共识）
- **证据**：新代码在 TURN_COMPLETED 上取 `assistant_text=str(event.payload.get("message", "") or "")`。但 codex app-server 的 JSON-RPC 形态事件 `turn/completed` 的 params 只含 `threadId` 和 `turn`，不含 `message`；助手正文是此前 `item/agentMessage/delta`（TURN_DELTA）单独发出的。仓库自身 `tests/test_channel_native_codex.py` 里 10 处 `turn/completed` 全为 `{"threadId": ...}` 形态。
- **问题**：codex app-server 路径传入空 `assistant_text` → `compose_session_title` 返回空 → `_maybe_refresh_session_title` 直接 `return False`，标题永不刷新。四条路径全部有效的验收目标未达成。只有 event_msg 形态的 `task_complete`（带 `last_agent_message`）与 Claude 分支能拿到正文。新增测试用 `FakeAgentTransport` 直接伪造带 `message` 的 TURN_COMPLETED，绕过真实事件转换，因此测试通过并不能证明这条路径可用。
- **修复**：在事件消费循环里按回合累积 TURN_DELTA 文本，完成事件正文为空时用累积值，回合结束后清空；或由 transport 层把合并正文带入完成事件。回归测试必须用「正文增量 + 无正文完成通知」的真实序列，经真实适配器验证。
- **回证**：VERIFIED @ 11934-11937, 8306-8313，`assistant_text=str(event.payload.get("message", "") or "")`；`"message": str(payload.get("message", ""))`。理由：JSON-RPC 的正文仅以 TURN_DELTA 单独发出，完成事件无 message，且无其他标题调用消费 delta。

### 2. [Warning] src/walkcode/channel_native/__init__.py:10267-10302 (Symbol: Orchestrator.refresh_session_status_card)

> **一句话**：状态卡编辑失败后，话题根标题会永久停止更新

- **Category**: ErrorHandling
- **Confidence**: dim-correctness 0.98, dim-goalfit 0.99
- **来源**: correctness + goalfit（2 来源命中）
- **证据**：本次改动让 Lark TUI observed 的话题根卡片兼任 status card（`health_message_id == root_message_id`）。而既有失败处理是 `edit_view` 抛异常或返回 False 时 `binding.health_message_id = ""`，随后 `send_view` 发新卡片并把指针指向它。飞书话题根无法被替换，新卡片只能是根下回复。
- **问题**：一次瞬时编辑失败后，status card 指针永久转移到子卡片，后续所有标题刷新只改子卡片，折叠视图里的话题根永远停在旧标题（通常就是 uuid 占位）。这正好破坏本次改动"原地 patch、message_id 不变"的验收标准。heal、维护 tick、`static_status_card` 都不会把指针拉回根卡片。
- **修复**：在共享刷新入口识别"根即状态卡"的 binding（`health_message_id == root_message_id` 且 `origin == external_tui`）。这类 binding 编辑失败时保留原指针、记录降级、等后续事件重试，不回退发子卡片；仅非根状态卡允许回退。补"先失败后成功、始终编辑同一根消息"的测试。
- **回证**：VERIFIED @ 10280-10315，`binding.health_message_id = ""; new_message_id = await channel.send_view(binding, view); binding.health_message_id = str(new_message_id)`。理由：Lark 编辑异常会触发沿 root 发 thread reply 并永久改指针；heal、维护 tick 和 capabilities 均不会恢复根卡片。

### 3. [Warning] ARCHITECTURE.md:61-71

> **一句话**：飞书话题根和会话标题的新规则没有进入架构文档

- **Category**: Completeness
- **Confidence**: dim-conventions 0.99, dim-maintainability 0.96
- **来源**: conventions + maintainability（2 来源命中）
- **证据**：ARCHITECTURE.md 仍写 "Session placement is one session per reply chain: a non-reply message roots a new session at its own message id."；`docs/adr/0044-lark-live-ingress-and-card-rendering.md:54` 也只记录旧建根规则。本次 diff 无任何 `.md` 变更。
- **问题**：话题根从文本变卡片、根兼任状态卡、标题来源分级 / 节流 / 持久化水位（`Session.title_refreshed_at`）——都是用户可见行为与持久化策略的变更，目前只存在于实现注释里。AGENTS.md 与全局规约要求同步文档。后续维护者无法从文档得知 `root_message_id == health_message_id` 这个不变式，也不知道换状态卡时指纹必须按消息失效。
- **修复**：更新 ARCHITECTURE.md 终端观察会话章节 + 修订 ADR 0044，记录根卡片身份、状态卡指针、补建迁移语义、标题来源优先级与节流规则。README 的终端观察说明补一句标题刷新行为。
- **回证**：VERIFIED @ 61-71。理由：旧规则本身仍正确，但遗漏根卡兼任状态卡及持久化标题策略；全局规约要求同步文档，**更新既有文档即可，无须新增 ADR**。

## 🔴 高置信

无。

## 🟡 中置信

无。

## ⚠️ 冲突项（必须人工判断）

无。

## 💡 Suggestion（未回证，参考）

### S1. [Suggestion] src/walkcode/channel_native_runtime.py:4353-4375 (Symbol: _create_lark_tui_observed_binding) — Reuse

> **一句话**：飞书三条建根路径各自造卡，标题规则已经不一致

- **Confidence**: dim-goalfit 0.96
- **问题**：频道建根（同文件约 1853 行）、TUI observed 建根、rootless 补建三处各自构造卡片，混用手写视图字典、`ViewModelFactory.health_view` 和三套标题清理方式（首行前 40 字 / `_telegram_session_topic_name` 128 字 / `_clean_session_title` 40 字）。后续修改根卡片契约要同步改三处。
- **修复**：抽取共享的 Lark 根状态卡创建 helper，统一用 `ViewModelFactory.health_view` + 一套标题清理，三处只传各自元数据。

### S2. [Suggestion] src/walkcode/channel_native/__init__.py:971-987 (Symbol: SESSION_TITLE_SOURCE_RANKS) — Maintainability

> **一句话**：标题来源散落为裸字符串，新增来源时容易漏改或拼错

- **Confidence**: dim-maintainability 0.94
- **问题**：来源名同时出现在 rank 表、rolling 集合、生成器、runtime 赋值四处；未知来源被静默映射为 rank 0，拼错不会报错。
- **修复**：用字符串枚举统一来源，rank 与 rolling 属性集中定义；反序列化时显式处理未知旧值。

### S3. [Suggestion] tests/test_channel_native_session_title.py:461-476 (Symbol: StatusCardFingerprintTests.test_pointing_at_a_new_card_invalidates_the_fingerprint) — Completeness

> **一句话**：新卡片首次更新后能否继续去重没有测试保护

- **Confidence**: dim-maintainability 0.97
- **问题**：测试只确认新消息首次被编辑，没有再刷一次验证「编辑成功分支写回的新 key 确实能去重」。即使该分支写错格式，测试仍会通过；编辑失败改发新卡片的分支也未覆盖。
- **修复**：首次编辑后再刷一次并断言调用数不变；再补编辑失败 → 发新卡片 → 相同视图被去重的回归测试。

## 🟣 Pre-existing（存量问题，不计入本次裁决）

### P1. [Warning, PreExisting] src/walkcode/channel_native/__init__.py:10265-10293 (Symbol: Orchestrator.refresh_session_status_card) — Concurrency

> **一句话**：并发刷新会让旧标题覆盖新标题，话题根可能长期显示过时内容

- **Confidence**: dim-concurrency 0.97
- **问题**：`get(指纹) → await edit_view(...) → 写回指纹` 是跨 await 的 read-modify-write，无版本校验。`_drain_events`、TUI hook、维护 tick 可以并发刷同一 session，两个调用同时越过去重检查，后返回的旧视图覆盖新视图。**这是存量竞态**，但本次新增的标题刷新让它从"进度行滞后"升级为"话题根标题回退"这种用户可见现象；`(message_id, fingerprint)` 配对只缩小换卡片时的误去重窗口，对同一消息无保护。
- **修复**：按 session 加异步锁，锁内重算视图并完成去重、编辑/发送、缓存写回；远端返回后复核当前 binding 与 message_id。补阻塞适配器的并发测试。

### P2. [Suggestion, PreExisting] src/walkcode/channel_native/__init__.py:9238 (Symbol: Orchestrator.__init__) — Concurrency

> **一句话**：归档会话的卡片指纹不会释放，常驻进程内存会持续增长

- **Confidence**: dim-concurrency 0.99
- **问题**：`_status_card_fingerprints` 全仓库只有初始化、读取、覆盖，没有删除；`archive_session` 只标记归档。长期运行条目随历史会话增长。本次没有增加条目数量，但每条从一个字符串扩大为含 message_id 的元组。
- **修复**：终态卡片送达后 / 归档成功后删除对应缓存，并补长期创建—停止—归档的容量测试。

## ❌ 已驳回（Phase 2 判定为误报，仅作日志）

无。

## 非结构化报告（格式不符，独立展示）

无。

## 维度元信息

| 来源 | VERDICT | issues | exit | 备注 |
|---|---|---|---|---|
| dim-correctness | NEEDS_FIX | 2 | 0 | — |
| dim-goalfit | NEEDS_FIX | 3 | 0 | — |
| dim-maintainability | NEEDS_FIX | 4 | 0 | — |
| dim-conventions | NEEDS_FIX | 1 | 0 | — |
| dim-concurrency | NEEDS_FIX | 2 | 0 | 首轮 5 并发时 exit=124 超时，单跑重试成功；2 条均标 PreExisting |

## 裁决依据

- 存活 Warning 3 条（均高共识 + Phase 2 VERIFIED）→ NEEDS_FIX
- Suggestion 3 条不参与裁决
- PreExisting 2 条单独归档，不算本次变更的账
- 全部 5 个维度产物完整（concurrency 经重试），无 unavailable

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`；回证：`$RUN_DIR/verify-{1,2,3}.md`
- 元信息：`$RUN_DIR/meta-*.txt`；身份：`$RUN_DIR/run.json`
- RunDir: `/var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-9273576-1785743106.G7Kk`
