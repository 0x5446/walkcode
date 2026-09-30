# 全项目清理第 3 批 — codex 修复 + 去重 + SDK 兼容删除 + 脚本重写 — deep-review

- Repo: /Users/alpha/workspace/walkcode-3a
- Branch: chore/batch3（合成 fix/batch3a-codex-daemon、chore/batch3b1-dedup、chore/batch3b2-sdk-compat、chore/batch3c-scripts）
- 引擎: codex（~/.codex-profiles/work，llm_proxy / gpt-6-astra / medium）；cursor 未登录跳过
- 门禁: 无 Critical
- VERDICT: SAFE

| 轮次 | HeadSHA | RunDir | 结果与处理 |
|---|---|---|---|
| 1 | 54b4637 | deep-review-walkcode-3a-54b4637-1790736884.v2ts | security / concurrency SAFE；correctness + goalfit 共识：镜像退避期限用循环开始时的时钟，30 s 请求超时吃掉退避；maintainability：connect/query 不重试缺回归测试；conventions：daemon 探测文档过时；data 2 条 PreExisting（hook 10 次归档、保存失败后重复事件被当成已处理）不在本批 → 905611d 修前 3 条 |
| 2 | 905611d | deep-review-walkcode-3a-905611d-1790737371.xvXX | correctness / goalfit SAFE |

## 真实环境验证

- 本机三个 codex home 用新探测：~/.codex → daemon，两个 profile → stdio（与现状一致）
- `channel_native_debug.py agent-smoke --live`（personal-claude，真实 Claude CLI）：turn.delta + turn.completed
- 全量 1251 passed / 9 skipped；connect/query 不重试测试在 main 旧代码上均为 2 次调用（失败），修复后 1 次

## 未处理（记入后续）

- 保存失败后重复事件被当成已处理而删队列文件（PreExisting，data 维度）
- uninstall.sh 只在临时 HOME + 假命令下验证，未在真实环境运行
