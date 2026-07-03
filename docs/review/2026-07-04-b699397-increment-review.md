# Deep Review 综合结论（b699397..HEAD 增量）

**VERDICT**: NEEDS_FIX → 全部修复完成（474 tests green）
**轮次**：1 / 3（--plan-only 变体：审查后由 host 直接修复并以测试回证）
**类型**：code

> 范围：refactor/channel-native-v3 上 b699397..HEAD 4 个提交（form 卡 div 修复、
> 健康卡 model/context、/model 翻转结果卡、lark 拒绝回执），8 files +351/-7
> Review engine：codex 0.142.5（host: claude; engine_source: auto; effort=medium）
> Cursor：composer-2.5 smoke test 失败，跳过
> 维度：8 个 codex 并行（correctness/errors/security/concurrency/data/observability/design/tests）
> Phase 2 验证：由 host 逐条读源码核实并直接修复 + 单测回证（未派独立回证进程）
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: dbab12c（审查时）
> RunDir: /var/folders/00/.../deep-review-walkcode-dbab12c-1783095173.t2Ix

## 结果一览（9 findings，0 Critical / 9 Warning，全部处置）

| # | 维度 | 问题 | 处置 |
|---|---|---|---|
| 1 | correctness + data（跨维度共识） | `_model_slug_matches` 前缀关系 slug（claude-opus-4 vs claude-opus-4-8）可多命中，多个按钮标"当前" | ✅ 修复：`model_choice` 只标最长匹配；测试 `test_model_choice_prefix_related_slugs_mark_only_longest_match` |
| 2 | errors | 已停止会话（非 TUI）里发消息被 SESSION_STOPPED 拒且无回执 | ✅ 修复：回执表加 SESSION_STOPPED；测试覆盖 |
| 3 | security | 健康卡模型名未消毒直接进 code span，可被反引号逃逸伪装 UI | ✅ 修复：strip 反引号 + `_inline` |
| 4 | data | LEASE_EXPIRED 回执与入站账本不完成矛盾：重投会重发回执，lease 恢复后还会提交用户被告知"未提交"的消息 | ✅ 修复：LEASE_EXPIRED 撤出回执表（保持重投自动重试语义，telegram 既有测试锁定）；SESSION_STOPPED 加入账本终态集合 |
| 5 | observability | telegram `/status` 文本渲染缺 Model/Context 行 | ✅ 修复：render_view_text health 分支补齐 |
| 6 | observability | 拒绝回执发送失败被 `except: pass` 吞掉 | ✅ 修复：`_log_degrade("lark_rejection_note_send_failed", ...)` |
| 7 | design | `[1m]` 长上下文标记被 dated live id 覆盖后上限显示 200k | ✅ 缓解：`_context_window_limit(model, used)` 观测占用超默认窗即升级 1m 显示（完整拆字段方案留作后续）；测试覆盖 |
| 8 | tests | /model 命令真实路径缺"current 标记"回归测试 | ✅ 补测试 `test_slash_model_marks_session_live_model_as_current` |
| 9 | concurrency | — | SAFE，零 issue |

## 备注

- cursor-agent 已登录但 composer-2.5 smoke test 失败（超时），本轮无跨引擎视角。
- 第一轮 8 并发 + 默认 xhigh effort 10 分钟未出结果；降至 4 并发 + medium 后全部完成。
- 原始产物在 RunDir（dim-*.md / dim-*.log），保留供回溯。
