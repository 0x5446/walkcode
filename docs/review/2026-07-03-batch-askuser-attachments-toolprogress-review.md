# Deep Review 综合结论

**VERDICT**: NEEDS_FIX
**轮次**：1 / 3（--plan-only，不进入 fix 循环）
**类型**：code

> 范围：本分支最近三个提交 —— 1b45097（附件传递+多tab卡片修复）、383bcd9（批量 AskUserQuestion 重构）、810e7c5（附件权限 add_dirs + 工具进度合并卡片）
> Review engine：codex codex-cli 0.142.5（host: claude; engine_source: auto）
> Cursor：disabled（composer-2.5 smoke test 失败，跳过）
> 维度：8 个 codex 并行（correctness errors security concurrency data observability design tests，全部成功）
> Phase 2 验证：9 条已派（另 5 组高共识高自信免回证）；结果 8 VERIFIED / 1 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 810e7c5（refactor/channel-native-v3）
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-810e7c5-1783070938.CEs3
> 规模：+694 / -210 行，9 文件
> plan-only：只出报告，未改任何文件

无 Critical。全部 finding 为 Warning 级。按 PR 门禁规则（不带未解决 Critical 不阻断），本报告不阻塞合并，但下列顶级项建议合并前修复。

## 🔴🔴 顶级必修（≥2 维度共识，高自信）

### 1. [Warning] src/walkcode/channel_native/__init__.py:2059-2078 (Symbol: answer_awaiting_other / _finalize_ask_user)
> **一句话**：点过"其他"再改点选项提交后，等待状态不清理，用户下一条普通消息会被吞成旧问题的答案，不会发给智能体。

- **Category**: EdgeState
- **Confidence**: dim-correctness 0.95, dim-concurrency 0.90
- **来源**: correctness + concurrency（跨维度共识，免回证）
- **问题**: `begin_awaiting_other()` 写入 `_awaiting_other_by_binding` 后，若用户改点选项并 `submit_all`，`_finalize_ask_user()` 只写 `ctx.decision`，不清 `ctx.awaiting_other` 与 binding 映射。下一条文本进 `handle_inbound_event()` 会被当自由文本答案消费。且 `answer_awaiting_other()` 对已决 ctx 仍返回 accepted 并改写 `ctx.answers`，破坏 write-once。
- **修复**: `_finalize_ask_user()` 里清理 awaiting 映射（`ctx.awaiting_other = None` + pop binding key）；`answer_awaiting_other()` 开头对 `ctx.decision is not None` 返回 `ALREADY_DECIDED`。

### 2. [Warning] src/walkcode/channel_native/__init__.py:2132-2133 (Symbol: _decide_ask_user_question)
> **一句话**：一题都没答也能点提交，智能体会收到空答案或缺一半的答案继续干活。

- **Category**: DataIntegrity
- **Confidence**: dim-correctness 0.90, dim-data 0.95, dim-concurrency 0.90
- **来源**: correctness + data + concurrency（免回证）
- **问题**: `submit_all` 直接 `_finalize_ask_user`，不校验完整性。空 `{}`、部分作答、多选全取消后的 `[]` 都会成为最终 decision 传给 `transport.answer_user_question()`。
- **修复**: finalize 前校验每题已有非空答案（多选允许显式空需产品决策）；不满足时不写 decision，返回带缺失题提示的 update 视图。

### 3. [Warning] src/walkcode/channel_native/__init__.py:7658-7666 (Symbol: _drain_events / _seal_tool_progress_burst)
> **一句话**：回合结束消息为空时进度卡不封口，下一轮工具会继续改上一轮的旧卡，两轮进度混在一起。

- **Category**: EdgeState
- **Confidence**: dim-correctness 0.90, dim-data 0.90, dim-design 0.82
- **来源**: correctness + data + design（免回证）
- **问题**: 封口只发生在"有可见文本"的分支里；`TURN_COMPLETED` 的 message 为空时 `render_view_text()` 返回空串直接 `continue`，`tool_progress_message_id`/`lines` 残留。另外这两个键随 `binding.capabilities` 持久化，进程重启后 stale 值复活，同样污染新一轮。
- **修复**: 对 `TURN_COMPLETED`（及权限/提问等非工具事件）无条件封口，不依赖 visible_text；`_binding_to_dict/from_dict` 过滤这两个临时键（或恢复时丢弃）。

### 4. [Warning] src/walkcode/channel_native/__init__.py:7612-7617 (Symbol: _handle_ask_user_decision)
> **一句话**：问答卡原地更新失败时被当成功处理，用户看到旧卡以为选择没生效，且日志无线索。

- **Category**: ErrorHandling
- **Confidence**: dim-errors 0.95, dim-observability 0.91
- **来源**: errors + observability（免回证）
- **问题**: `edit_view()` 协议返回 bool；Telegram 失败返回 False 不抛异常。当前代码 `await channel.edit_view(...); return` 不检查返回值——False 时既不补发新卡也无日志。
- **修复**: `edited = await channel.edit_view(...)`，仅 `edited is True` 才 return；False/异常统一走 `_send_session_view` 补发，并打一条降级日志（session_id/interaction_id/异常）。

### 5. [Warning] src/walkcode/channel_native/__init__.py:7718-7733 (Symbol: _upsert_tool_progress_view)
> **一句话**：工具进度卡绕过重试队列直发，网络抖动时这条进度直接消失且无任何日志。

- **Category**: ErrorHandling
- **Confidence**: dim-errors 0.90, dim-observability 0.90
- **来源**: errors + observability（免回证）
- **问题**: 直发绕过 DurableOutbox；edit 失败静默转发新卡、send 失败静默 return。`tool_progress_lines` 已先行写入，内存状态与用户所见分叉。
- **修复**: 至少给两个失败分支加 stderr 日志（fallback=send_new / drop=true + tool_id/tool_name/异常）；可选：发送成功后再提交 lines/message_id。

## 🔴 高置信必修（单维度 + 回证 VERIFIED）

### 6. [Warning] src/walkcode/channel_native/__init__.py:33-48 + 4928-4942 (attachment_download_dir / _create_client)
> **一句话**：四个实例共用同一个附件目录，且整个目录开放给智能体，工作与个人档案的附件互相可读。

- **来源**: security 0.88；**回证**: VERIFIED @ 33-48/4928-4942 —— 默认目录未按 profile 隔离，add_dirs 加的是共享目录，无 symlink/属主检查，mkdir 错误被吞
- **修复**: 默认目录按 `WALKCODE_PROFILE`（或 CLAUDE_CONFIG_DIR）派生子目录并 chmod 0700；或在 4 个 `~/.walkcode/*.env` 里各配 `WALKCODE_DOWNLOAD_DIR`。目录创建失败不再 suppress，改为带路径报错。

### 7. [Warning] src/walkcode/channel_native/__init__.py:7694-7735 (_upsert_tool_progress_view 并发)
> **一句话**：后台事件流和终端观察钩子可能同时更新同一张进度卡，造成重复发卡或已封口的卡被复活。

- **来源**: concurrency 0.88；**回证**: VERIFIED —— `_start_background_event_drain` 的 task 与 deferred TUI hook drain task 可并发触达同一 session/binding 的 upsert/seal
- **修复**: 按 binding.key() 加 asyncio.Lock 包住 upsert/seal 的读改写；或引入 generation 标记，await 返回后 generation 变了就不写回 message_id。

### 8. [Warning] src/walkcode/channel_native/__init__.py:4006-4018 / 4240-4252 (download_attachment)
> **一句话**：下载的附件文件永不清理，服务常驻数周后磁盘持续累积。

- **来源**: data 0.95；**回证**: VERIFIED —— 全 src 无任何 unlink/cleanup 路径命中 local_path（缓解：单用户附件频率低）
- **修复**: 启动时按目录年龄做兜底清理（如 >7 天删除）；或 turn 完成后删除本轮附件。

### 9. [Warning] src/walkcode/channel_native/__init__.py:7682-7719 + lark_cards.py:241-268 (工具摘要脱敏)
> **一句话**：命令行里的密钥令牌等敏感值会原样出现在飞书进度卡上，聚合多行后暴露面更大。

- **来源**: security 0.86；**回证**: VERIFIED —— tool_input 键值未脱敏进 summary（缓解：160 字符截断 + allowlist 接收方是用户本人）
- **修复**: `_compact_tool_summary` 前加脱敏（token/secret/password/api_key/bearer/cookie 等键与 `KEY=value`、`--token x`、`Bearer ...` 形态掩码）。

### 10. [Warning] src/walkcode/channel_native/__init__.py:6428-6435 (附件大小/数量限制)
> **一句话**：附件没有大小和数量上限，超大文件会先整个读进内存再落盘。

- **来源**: security 0.82；**回证**: VERIFIED（平台级上限未能在本地证实，视为未确认的缓解）
- **修复**: 加 max_attachment_bytes / max_attachments_per_turn，Telegram 用 getFile 的 file_size 预检，Lark 下载时累计字节超限中止。个人工具场景优先级低。

### 11. [Warning] src/walkcode/channel_native/__init__.py:2450-2469 (双形态视图)
> **一句话**：同一问答视图有两套形态，飞书能显示勾选态而电报侧丢失，两边语义已经分叉。

- **来源**: design 0.88；**回证**: VERIFIED（缓解：Telegram 通道处于退役观察期）
- **修复**: 以 `questions` 为唯一契约，`actions` 改由 helper 派生（label 带 ✓ 与当前答案）。Telegram 退役后此项可降级为清理任务。

### 12. [Warning] tests/（批量提交边界测试缺口）
> **一句话**：空提交、多选全取消、双击提交、重启恢复后继续作答这四个真实路径都没有测试钉住。

- **来源**: tests 0.91；**回证**: VERIFIED —— 4 项中至少 3 项无覆盖（双击有 permission 路径的等价 write-once 测试，ask 专属未钉）
- **修复**: 补 `test_submit_all_without_answers_*`、`test_multi_select_toggle_all_off_then_submit_*`、`test_submit_all_is_write_once_on_double_click`、`test_batch_ask_user_round_trips_after_restore`。与 #2 的行为决策一起做。

### 13. [Warning] tests/（工具进度失败回退测试缺口）
> **一句话**：飞书卡片更新失败后的补发路径、多工具多行渲染都没有测试驱动。

- **来源**: tests 0.92；**回证**: VERIFIED（同 tool_id 合并已有部分覆盖）
- **修复**: 补 lark patch 失败→回退发新卡+替换 message_id、多 tool_id 累积多行 + render_view_text 多行分支的测试。

### 14. [Warning] src/walkcode/channel_native/__init__.py:46-48 (mkdir suppress + add_dirs 无日志)
> **一句话**：下载目录建不出来或授权目录没注入成功时毫无日志，排查"为什么还弹权限卡"无从下手。

- **来源**: errors 0.88 + observability 0.83（配对）；**回证**: 核心事实由 verify-1 一并证实（suppress 无检查）
- **修复**: mkdir 失败打日志并带路径；`_create_client` 记录一次 `supports_add_dirs` + `download_dir` 决策日志。

## ⚠️ 冲突项

无。

## ❌ 已驳回（Phase 2 判定误报）

- **design/VersionSkew：旧卡 token 无视图版本会错位覆盖答案** —— FALSE_POSITIVE。回证确认：同一交互内 `ctx.questions/options` 不可变，位置索引不会错位；提交后 `ALREADY_DECIDED` 拒绝一切旧 token。提交前旧卡改选属设计允许的"可反悔"。

## 维度元信息

| 来源 | VERDICT | issues | exit | 备注 |
|---|---|---|---|---|
| dim-correctness | NEEDS_FIX | 3 | 0 | — |
| dim-errors | NEEDS_FIX | 3 | 0 | — |
| dim-security | NEEDS_FIX | 3 | 0 | — |
| dim-concurrency | NEEDS_FIX | 2 | 0 | — |
| dim-data | NEEDS_FIX | 3 | 0 | — |
| dim-observability | NEEDS_FIX | 3 | 0 | — |
| dim-design | NEEDS_FIX | 3 | 0 | 1 条被回证驳回 |
| dim-tests | NEEDS_FIX | 4 | 0 | — |
| cursor-holistic | (disabled) | — | — | composer smoke 失败 |

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`（RunDir 见报告头）
- 各回证：`$RUN_DIR/verify-{1..9}.md`
- 元信息：`$RUN_DIR/meta.txt`、`$RUN_DIR/run.json`
