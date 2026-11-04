# Deep Review 综合结论

VERDICT: NEEDS_FIX（审查对象 9c9610c；10 条 Warning 已在后续提交里全部处理，见文末「修复状态」）

**轮次**：1 / 2
**类型**：mixed

> 范围：walkcode 分支 feat/native-slash-commands 相对 main 的 diff。目标：让 Codex/Claude 原生斜杠命令在飞书频道里真正执行（ADR 0071）。
> Review engine：codex codex-cli 0.166.0（host: claude; engine_source: explicit；模型：gpt-6-luna / llm_proxy / effort medium）
> Cursor：disabled（未安装）
> 维度：基础 correctness goalfit maintainability conventions consistency feasibility alignment + 信号触发 security concurrency + 红队 redteam（10 个，全部 exit 0）
> Phase 2 验证：10 条已派（0 条免验）；结果 10 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 9c9610c2c72405ad699cff9db6393a70cd16b582
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T//deep-review-walkcode-9c9610c-1793778052.8H1x
> 规模：1099 行新增 / 10 文件
> 模式：默认报告模式（审查本身未动文件；修复由 host 另行提交）

## 📋 需求点交付核对

| 需求点 | 状态 | 兑现位置 / 缺口 |
|---|---|---|
| R1 | ✅ 已兑现 | `channel_native_runtime.py` `_model_inventory`；`codex_transport.py` `list_models` |
| R2 | ✅ 已兑现 | `codex_transport.py` `set_model` / `_models_for_validation` |
| R3 | ⚠️ 部分兑现 | 模型列表不可用时 `/effort` 不校验 → 见 #4 |
| R4 | ✅ 已兑现 | `_start_compaction` / `_claim_compaction_turn`（残留风险见 #8） |
| R5 | ⚠️ 部分兑现 | 回合进行中 `/clear` 丢失确认；重启后丢失模型选择 → 见 #1 #2 |
| R6 | ✅ 已兑现 | `submit_turn` 中 review/init 改写（多行重点见 #10） |
| R7 | ⚠️ 部分兑现 | `/diff` 不显示未跟踪文件内容、无提交仓库报错 → 见 #5 |
| R8 | ✅ 已兑现 | `_CODEX_CHAT_UNSUPPORTED` |
| R9 | ✅ 已兑现 | `_parse_bot_command` 只在 agent=codex 时拦 effort/mcp |
| R10 | ✅ 已兑现 | `_CLAUDE_NATIVE_COMMANDS` / `_CODEX_NATIVE_COMMANDS` |
| R11 | ⚠️ 部分兑现 | 判断过宽，任何斜杠输入都会被确认 → 见 #3 |
| R12 | ✅ 已兑现 | ADR 0071、部署文档、README、AGENTS.md、单测 |

## 🔴🔴 顶级必修

### 1. [Warning] src/walkcode/channel_native/codex_transport.py:731 (Symbol: events / _start_fresh_thread)
> **一句话**：回合进行中执行 `/clear` 时，新 thread 的确认会丢失，下一轮的真实回复会被截断。

- **来源**：correctness + goalfit + maintainability + concurrency + feasibility（5 个维度）
- **问题**：同一 handle 已有 drain 时，orchestrator 不会启动新 drain；`_fresh_threads` 标记留到下一次 drain，导致那次 drain 只发出合成完成事件就退出。
- **回证**：VERIFIED

### 2. [Warning] src/walkcode/channel_native/codex_transport.py:615 (Symbol: _start_fresh_thread)
> **一句话**：runtime 重启后再执行 `/clear`，新 thread 会回到默认模型。

- **来源**：correctness + feasibility + redteam
- **回证**：VERIFIED

### 3. [Warning] src/walkcode/channel_native/orchestrator.py:3475 (Symbol: _silent_command_notice)
> **一句话**：结果为空的任意斜杠输入都会被提示为"已执行"。

- **来源**：correctness + consistency（违背 ADR 0061 的零输出告警约定）
- **回证**：VERIFIED

## 🔴 高置信

4. [Warning] `codex_transport.py` `set_effort`：model/list 不可用时接受任意值，并在之后每一轮发送（goalfit，VERIFIED）
5. [Warning] `channel_native_runtime.py` `_git_diff_text`：不显示未跟踪文件内容；没有提交的仓库里 `git diff HEAD` 报错（goalfit + redteam，VERIFIED）
6. [Warning] `channel_native_runtime.py` `_handle_mcp_command` / `_handle_diff_command`：只过了聊天白名单，不检查会话角色（security，VERIFIED）
7. [Warning] `channel_native_runtime.py` `_git_diff_text`：git 会执行仓库配置的 `core.fsmonitor` 或外部 diff 程序（security，VERIFIED）
8. [Warning] `codex_transport.py` `_listen_own_turn`：压缩期间 TUI 正好在同一 thread 开 turn 时，可能被当成压缩 turn 认领（concurrency，VERIFIED）
9. [Warning] `codex_transport.py` `_start_fresh_thread`：中断失败时旧任务可能继续运行，而用户看到的是清空成功（feasibility，VERIFIED）
10. [Warning] `channel_native_runtime.py` `_parse_bot_command` 和 `codex_transport.py` `_codex_native_command`：多行 `/review` 识别不出来（redteam，VERIFIED）

## 💡 Suggestion（未回证）

- maintainability：`/effort` 无参时调了两次 model/list。
- alignment：部署文档没说明 model/list 失败时跳过校验。

## 维度元信息

| 来源 | VERDICT | issues | exit |
|---|---|---|---|
| dim-correctness | NEEDS_FIX | 3 | 0 |
| dim-goalfit | NEEDS_FIX | 3 | 0 |
| dim-maintainability | NEEDS_FIX | 2 | 0 |
| dim-conventions | SAFE | 0 | 0 |
| dim-consistency | NEEDS_FIX | 1 | 0 |
| dim-feasibility | NEEDS_FIX | 3 | 0 |
| dim-alignment | SAFE | 1 | 0 |
| dim-security | NEEDS_FIX | 2 | 0 |
| dim-concurrency | NEEDS_FIX | 2 | 0 |
| dim-redteam | NEEDS_FIX | 3 | 0 |

## 修复状态（后续提交）

| # | 处理 |
|---|---|
| 1 / 9 | `/clear` `/new` 不再作为 turn，改由 `Orchestrator.start_new_agent_thread` 处理：回合进行中拒绝，新 `handle_id` 和 `thread_id` 一起写进 transport_ref。另外修了一个审查没发现的问题：从没跑过 turn 的 thread，`thread/resume` 会报 "no rollout found"（0.166 实测），现在用 `_unstarted_threads` 直接复用，重启后遇到这个错误就新开 thread |
| 2 | `start_new_thread(model=session.model)`，用持久化的模型兜底 |
| 3 | 改为按 transport 区分的 `_SILENT_COMMANDS` 白名单 |
| 4 | 没有模型列表时，退回到已知的推理强度取值集合 |
| 5 | 显示暂存区、工作区和未跟踪文件（限数量和大小）的内容；没有 HEAD 时改为对比暂存区 |
| 6 | `/diff` `/mcp` 需要会话主人或管理员（`_session_query_allowed`） |
| 7 | git 带 `-c core.fsmonitor=false -c diff.external=`、`--no-ext-diff --no-textconv` |
| 8 | 不修。两边都没有可区分的字段，需要共享 daemon 加两边同时操作才会发生，已记入 ADR 0071 Consequences |
| 10 | 两处解析都改成按任意空白切分 |
| 附带 | 切换模型后，第一轮状态卡显示旧模型（真实 E2E 中发现）：resume 时以待生效的 override 为准 |

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`；回证：`$RUN_DIR/verify-{N}.md`；身份：`$RUN_DIR/run.json`
