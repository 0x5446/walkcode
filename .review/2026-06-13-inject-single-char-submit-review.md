# Deep Review 综合结论

**VERDICT**: NEEDS_FIX → 已修复
**轮次**：1 / 3
**类型**：code

> 范围：v0.10.30 inject 重构（git range HEAD~1..HEAD），修复单字符飞书消息被误判成菜单键而不提交
> Review engine：codex 0.139.0（host: claude；engine_source: auto）
> Cursor：disabled（composer-2.5 smoke 失败，跳过）
> 维度：8 个 codex 并行（correctness / errors / security / concurrency / data / observability / design / tests）
> Phase 2 验证：主 agent 源码回读确认（concurrency+data 配对高共识 + tests 单维度高置信，均经源码核对为真）
> Repo: /Users/alpha/Documents/workspace/walkcode
> HeadSHA(review 时): 68622311eb07bdfd4086a0571d71b3c464db0bbe
> 规模：60 行 / 5 文件

## 维度判定汇总

| 维度 | VERDICT | 说明 |
|---|---|---|
| correctness | SAFE | 单字符消息提交路径正确，enter/menu_key 分支覆盖完整 |
| errors | SAFE | subprocess 非零返回处理完整 |
| security | SAFE | tmux 参数有 `--` 边界，无注入面 |
| design | SAFE | menu_key API 契约清晰 |
| observability | SAFE | 投递确认走 UserPromptSubmit hook，无新增盲点 |
| concurrency | NEEDS_FIX | 见 Finding 1 |
| data | NEEDS_FIX | 见 Finding 1（与 concurrency 配对，同一根因）|
| tests | NEEDS_FIX | 见 Finding 2 |

## 🔴 高置信必修（已修复）

### Finding 1 — 全局固定 tmux buffer 名导致并发注入串会话/丢消息
- **来源**：concurrency (0.87) + data (0.82)，≥2 维度配对高共识
- **Severity**: Warning
- **File**: src/walkcode/tty.py — `inject`（原 `_INJECT_BUFFER = "walkcode-inject"`）
- **问题**：`_INJECT_BUFFER` 是固定名，而 tmux buffer 存活在（共享的）tmux server 上，等于一个全局可变槽位。两个并发注入（不同 worker 线程，或分别驱动同一用户 tmux server 的 claude/codex 两个 bot 进程）会在 `set-buffer` / `paste-buffer -d` 上竞争：后一次 `set-buffer` 覆盖前一次内容，前一次 `paste-buffer -d` 可能把后一次的文本贴进自己的会话并删掉 buffer，后一次粘贴随之失败。结果是**消息串会话**（A 会话收到 B 的文本）或**丢消息**。
- **本次相关性**：pre-existing（v0.10.24 引入固定 buffer），但本次把 `"2"`/`"y"` 这类单字符聊天消息也纳入这条 paste 路径，**扩大了触发面**，故一并修。
- **修复**：改为每次调用生成唯一 buffer 名 `walkcode-inject-{pid}-{tid}-{seq}`（覆盖跨进程/跨线程），并在 paste 失败/异常时 `delete-buffer` 尽力清理避免泄漏。新增 `test_concurrent_injects_use_distinct_buffers` 锁定。
- **回证**：VERIFIED — 源码确认 buffer 名为模块级固定常量，set/paste 两步非原子，且 walkcode 实际是 claude/codex 双进程部署（共享 tmux server）。

## 🔴 高置信必修（已修复）

### Finding 2 — 权限兜底的 menu_key=True 缺少服务层测试锁定
- **来源**：tests (0.94)，单维度高置信
- **Severity**: Warning
- **File**: tests/test_perm_dedupe.py
- **问题**：`tests/test_tty_inject.py` 只测了 `tty.inject(..., menu_key=True)` 的低层分支；服务层的 `_maybe_tmux_fallback` 相关测试又把 `_tmux_fallback` 整个 mock。若将来 `_tmux_fallback` 漏传 `menu_key=True`，现有测试仍会全绿，但权限超时兜底会走聊天 paste 路径而非菜单原始按键，菜单选择回归且无人发现。
- **修复**：新增 `test_tmux_fallback_injects_menu_choice_as_menu_key`，直接调用 `_tmux_fallback`，断言 `inject("sess1", "1", enter=True, menu_key=True)`。
- **回证**：VERIFIED — 现有 fallback 测试确实只断言 `_tmux_fallback` 被调用，未触及 inject 参数。

## ✅ 结论

3 个 NEEDS_FIX finding 收敛为 2 个根因（buffer 竞态、测试缺口），均已修复。无 Critical。修复后完整测试套件 160 passed。
