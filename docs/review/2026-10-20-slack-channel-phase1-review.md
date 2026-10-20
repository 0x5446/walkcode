# Deep Review — ADR 0070 第 1 期：Slack 渠道

- Repo: /Users/alpha/workspace/walkcode-slack-p1
- HeadSHA: bee18e7（审查对象）
- RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-slack-p1-bee18e7-1792510317.No3k
- Target: `git diff origin/main...HEAD`（feat/slack-adapter，mixed）
- Engine: codex（gpt-6-luna · openai · effort medium，当前配置）
- 维度：correctness / goalfit / maintainability / conventions / consistency / feasibility / alignment / security / concurrency / redteam
- Phase 2：host 回读源码（含 .venv 里的 slack_sdk）核对

VERDICT: NEEDS_FIX → 已修复，无 Critical

## 🔴 已修

| # | 来源 | 发现 | 处理 |
|---|---|---|---|
| 1 | correctness + redteam（配对） | "可能已生效"发送的查重只比文本，会把更早的同文消息或同标题的另一张权限卡当成本次发送，导致回复/卡片丢失；长 thread 查不到最新消息 | 查重只看首次不确定发送之后的消息（`oldest`），并同时比对文本和按钮值（含一次性令牌）；发送键也纳入按钮值 |
| 2 | concurrency | 同一发送键并发重试可能都查不到、都重发 | 按发送键加 `asyncio.Lock`，查重与发送在锁内完成 |
| 3 | correctness | 原生会话状态同步失败后仍写缓存，之后不再重试 | 只在 Slack 返回成功后更新缓存，状态与标题分别记录、分别重试 |
| 4 | maintainability | `account_id` 首次读取会在事件循环里同步调 `auth.test` | 入站桥线程启动时预取身份，事件循环上只读缓存 |
| 5 | alignment | manifest 注释写"建两个应用"，部署表是三个 | 注释改为按实例建，指向部署文档的对应表 |

## ❌ 已驳回

- feasibility：`connect()` 返回后入站线程只等停止信号，断线后不再重连。核对
  `slack_sdk/socket_mode/builtin/client.py`：`connect()` 建好连接即返回，不阻塞；
  独立的 `current_app_monitor` 线程在会话失活时持续 `connect_to_new_endpoint()`，
  失败只记日志、下个周期重试。

## 维度结论

| 维度 | VERDICT |
|---|---|
| correctness | NEEDS_FIX（#1、#3） |
| goalfit | SAFE |
| maintainability | NEEDS_FIX（#4） |
| conventions | SAFE |
| consistency | SAFE |
| feasibility | NEEDS_FIX → 驳回 |
| alignment | NEEDS_FIX（#5） |
| security | SAFE |
| concurrency | NEEDS_FIX（#2） |
| redteam | NEEDS_FIX（#1） |

全量单测：1172 passed, 9 skipped。真实 Slack E2E 待 bot token（工作区管理员审批中）。
