# 全项目清理第 2 批 — 删除死代码 — deep-review

- Repo: /Users/alpha/workspace/walkcode-batch2
- Branch: chore/dead-code-batch2
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）；cursor 未登录跳过
- 维度: correctness / goalfit / maintainability / conventions（纯删除，无 security/concurrency/data 信号）
- 门禁: 无 Critical
- VERDICT: SAFE（唯一 Warning 为文档未同步，已修）

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | e9c1a72 | deep-review-walkcode-batch2-e9c1a72-1790692914.8K5I | correctness / goalfit / maintainability 零 ISSUE（R1–R9 全部兑现，未发现误删可达代码）；conventions 1 条 Warning：README 中英文迁移指南仍推荐 LegacyFeishuEnvConverter，ADR 0016 / 0003 仍列已删接口 → 本提交同步 |

## 刻意保留（非遗漏）

- `handle_is_live`、`_download_suffix`：测试观察真实逻辑的只读探针 / 薄包装
- `_is_interactive`（sendCard vs sendMessage）：测试靠它区分卡片与普通消息，合并会削弱十余处断言
- `interrupt_session` / `transport.interrupt`：用户已决定删除，放第 3 批
- 序列化死字段（WriterLease、PendingBinding、capability 字段）：第 4 批状态瘦身

## 验证

- 全量 1253 passed, 9 skipped（删掉的 15 个测试只覆盖被删代码）
- vulture 全仓只剩 `redirect_request` 一条误报（覆盖 HTTPRedirectHandler 回调）
- ruff F401 src/scripts/tests 全清
