# 只挪不改拆出 TUI hook 辅助函数 — deep-review

- Repo: /Users/alpha/workspace/walkcode-t
- Branch: refactor/split-tui-hooks
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）
- 维度: correctness / maintainability / conventions
- VERDICT: SAFE

| 轮次 | HeadSHA | RunDir | 结果 |
|---|---|---|---|
| 1 | 245292f | deep-review-walkcode-t-245292f-1790760308.ETQC | 三维 SAFE，零 ISSUE |

## 结果

- channel_native_runtime.py 5,621 → 4,411 行；新增 channel_native/tui_hooks.py 1,282 行
- 校验脚本：135 个顶层单元逐字相同（problems=0）；tui_hooks 不导入 runtime，无循环
- 测试只改 patch 目标与 import：_probe_process（12 处）、_TRANSCRIPT_READ_MAX_BYTES（1 处）改到 tui_hooks；宿主与 agent 各审计一次，无失效 patch

## 验证

- 全量 1127 passed / 9 skipped（与拆分前相同）
- 6 个实例真实 env 跑 doctor 正常；真实 Claude 回合跑通；临时 env 下 native hook --defer 写队列正常
