# 清理遗留 A 轮（gate 锁、重启收卡、长期空闲会话）— deep-review

- Repo: /Users/alpha/workspace/walkcode-la
- Branch: fix/leftovers-a
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）
- 门禁: 无 Critical
- VERDICT: SAFE（已达 MAX_ROUNDS=2，最后一轮 3 条以回归测试 + 旧版本对照验证）

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | 736c120 | deep-review-walkcode-la-736c120-1790756078.VWjU | 4 维共识：重启收卡仍会漏（启动时压缩先跑、改卡重试只在内存）；心跳失效退出路径未被锁覆盖（PreExisting）；竞态测试只覆盖投递侧 → 0676809：card_open/card_message_id 持久化、收尾总读磁盘决策、真实 hook 暂停的双侧锁测试 |
| 2 | 0676809 | deep-review-walkcode-la-0676809-1790756639.walY | 两卡交错时第二张被误收（2 维共识）；gate 超时长于 HITL 有效期时 hook 仍在等却被收；后台重试送达的卡片没记 message id（2 维共识）→ 本提交修复，三个新测试均在上一版失败 |

## 真实环境验证

- `uninstall.sh --dry-run` 在真机运行：只列 6 个实例（跳过 3 个 tap）、各 profile 只删 WalkCode hook 并先备份、env/备份/workspace 保留；前后目录快照一致（无副作用）
- 真实状态副本：work-claude 80 个长期空闲会话过期、2 个当场修剪；work-codex 6、work2-claude 2
- 9/13 残留的半截状态临时文件（14.8MB）移入 ~/.walkcode/backups/orphans/
- 全量 1128 passed / 9 skipped
