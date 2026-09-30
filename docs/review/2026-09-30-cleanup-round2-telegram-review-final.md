# 清理第 2 轮（第 6 批退役 Telegram，ADR 0069）— deep-review

- Repo: /Users/alpha/workspace/walkcode-r2
- Branch: chore/round2（chore/batch6-retire-telegram 合入最新 main + 审查修复）
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）；cursor 未登录跳过
- 门禁: 无 Critical
- VERDICT: SAFE（已达 MAX_ROUNDS=2，最后一条修复以回归测试 + 旧版本对照验证）

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | d4f9d86 | deep-review-walkcode-r2-d4f9d86-1790743249.p8VN | goalfit SAFE；correctness：修 WAITING_PERMISSION 复位后，无工具事件的新提问/回合结束不再清除等待状态；maintainability：CLI 退役参数测试传参形态错误；conventions：walkcode-release skill 仍写 telegram 检查；data（PreExisting）：延迟 hook 读盘错误被当坏事件归档 → 58488ef 全部修复（顺带修正 skill 回滚步骤：不能再跑 upgrade.sh） |
| 2 | 58488ef | deep-review-walkcode-r2-58488ef-1790743650.HQ1Q | maintainability SAFE（建议补回合结束用例，已补）；correctness：UnicodeDecodeError 不属于 OSError，坏编码文件会堵队列 → 本提交按坏事件归档并补测试 |

## 迁移中发现并修复的 Lark bug

TUI 观察会话每个 hook 都把 WAITING_PERMISSION 复位，Claude 随后的 Notification 重复提示压不住（Telegram 私聊无 thread id，旧测试碰不到）。

## 真实环境验证

- 6 个线上状态文件副本：新代码加载、compact、保存、重载全部成功
- 各实例真实 env 跑 doctor 正常（均为 WALKCODE_CHANNEL=lark）
- agent-smoke --live：真实 Claude、真实 Codex 各跑通一个回合
- 全量 1117 passed / 9 skipped
