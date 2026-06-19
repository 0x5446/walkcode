# Deep Review — 飞书整轮 backfill (v0.10.41)

**VERDICT**: NEEDS_FIX → 已修复（高价值项全部落地）后 **SAFE**
**类型**：code（+ ARCHITECTURE.md）
**轮次**：1 / 3（plan-only，手动修复非 auto-fix loop）

> 范围：未提交改动，修复"飞书只收到 TUI 回复最后一段"。核心 `src/walkcode/__main__.py` Stop 分支整轮 backfill。
> Review engine：codex 0.141.0（host: claude；engine_source: auto）
> Cursor：composer-2.5（holistic）
> 维度：8 个 code 维度并行 + cursor-holistic
> Repo: /Users/alpha/Documents/workspace/walkcode（worktree: fix+feishu-partial-forward）
> 分支: worktree-fix+feishu-partial-forward

## 已修复（高共识 / 高价值）

| # | 发现（来源） | 严重度 | 修法 |
|---|---|---|---|
| 1 | 截断从头切，丢最终结论（data 0.95 / cursor 0.92） | Warning | `_join_turn_segments` 改保留**尾部**，超限丢前导叙述，标 `…(truncated)` 在头 |
| 2 | 无 user 边界时把整个文件历史当一轮转发（correctness 0.86） | Warning | 两个 reader 加 `seen_turn_start`，没见边界返回 ""→回退 last_assistant_message |
| 3 | codex 双发读到半写 rollout 漏最终段，被 turn_id 去重冻结（correctness/errors/concurrency/data 4 维） | Warning | cmd_hook 在 last_assistant_message 不在结果中时**追加**为最终段（claude 恒等→不重复） |
| 4 | `message: null` → AttributeError 崩 hook，整条通知丢失（errors 0.67） | Warning | `_is_user_turn_start` + reader 全部 `isinstance(dict/str)` 守卫 |
| 5 | session_id 直拼 glob，含 `*?[` 可匹配别会话 rollout（security 0.9） | Warning | `_CODEX_SESSION_ID_RE` 校验 + 精确后缀 `-<sid>.jsonl` |
| 6 | codex 给了 transcript_path 仍全盘 rglob（cursor 0.88 / design） | Suggestion | hook 路径是 `rollout-*` 时直接用，否则才 rglob 兜底 |
| 7 | 静默回退无痕迹（observability 0.95） | Warning | Stop 分支 stderr 打 `source=/chars=/truncated=` 面包屑 |
| 8 | cmd_hook Stop 接线无集成测试（tests 0.92 / cursor 0.9） | Warning | `CmdHookStopMessageTests` 5 例（claude 整轮/回退/追加尾段/不重复/codex 分支） |

新增/更新测试共 +14（总 298 通过）。

## 已知/接受（未在本轮改）

- **claude `read_text()` 整文件读 + codex rglob 在 Stop 关键路径同步执行**（design 0.86 / cursor 0.95）：每轮一次、claude transcript 体量有界、codex 优先用 hook 路径已大幅缓解；真正的字节预算 tail-read 改动大，留作后续。
- **transcript_path 未做白名单校验**（security ISSUE_1）：hook_data 的 cwd/session_id 等本就被信任，属既有信任模型，非本次引入。
- **`_read_last_assistant_text` 现仅测试在用 + 两个 reader 有重复**（design ISSUE_2，Suggestion）：保留作回退 helper，后续可抽共享收集器。
- **空内容 user 记录被当边界**（cursor 0.42）：退化输入，影响极小。

## 原始产物

RunDir（codex 子进程原始输出 / cursor JSON）：`/var/folders/.../deep-review-fix+feishu-partial-forward-962d235-*`（未清理，供回溯）
