# Deep Review — 权限卡片 TUI↔飞书同步失效

**VERDICT (initial)**: NEEDS_FIX → **已修 + re-verify 全绿**
**日期**: 2026-06-17
**Review engine**: codex 0.139（8 维度并行，隔离 CODEX_HOME 禁 hooks 避免碰实时）+ cursor composer-2.5 holistic；host=claude
**范围**: 工作区改动（claude 权限卡片失效重构，8 文件 +278/-119）

## 已修（高共识真 bug）

1. **失效门禁与 `set_decision_once` 竞态**（correctness/errors/security/concurrency/data/design 6 维度共识，0.88–0.92）：门禁在 `_on_card_action` 锁外 check `is_invalidated`，`set_decision_once` 在另一把锁只查 `decision is None`，中间 `invalidate_session` 可写入 → 失效后迟到点击仍写决定。**修**：`set_decision_once` 锁内加 `invalidated_at` 拒绝（authoritative refusal inside lock）。
2. **`register_or_get(None)` 不触发 GC**（correctness/errors 2 维度，0.93–0.95）：claude 全 None-key，注册时 `key is None` 跳过 `_gc_locked` → None-key GC 修复在纯 claude 流程不生效。**修**：无条件 `_gc_locked()`。
3. **AskUserQuestion 分支漏门禁**（correctness/design 2 维度，0.86–0.9）：门禁只加在权限分支，AskUserQuestion 分支（toggle/select/submit）失效后仍可写。**修**：门禁提到 `_on_card_action` 入口，统一覆盖两类卡。

回归测试：`test_set_decision_rejected_after_invalidate`、`test_none_key_registration_triggers_gc`、`test_askq_click_rejected_after_invalidate`。

## 已 acknowledge（次要，未阻塞 PR）

- **invalidate_session 按 session 批量失效偏宽**（0.78）：claude 工具串行，PostToolUse 时该 session 一般只一张未决卡；已加边界注释说明（无 tool_use_id 可精确匹配的已知窄边）。
- **observability**：cmd_hook post-tool 失败已加 stderr 日志；入口门禁日志加 `reason`（invalidated vs stale_poll）。
- **tests**：cmd_hook→/hook/post-tool 链已由真实冒烟覆盖（隔离 tmux 跑 `walkcode hook post-tool`，mock server 收到 POST + session_id）。

## re-verify

253 单测 + 5 subtests passed、ruff clean、import OK。无 Critical 残留。
