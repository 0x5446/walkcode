# Deep Review — ADR 0070 Slack 渠道设计（plan-only）

- Repo: /Users/alpha/workspace/walkcode-slack
- HeadSHA: 5f86008（审查对象：docs/adr/0070-slack-channel.md、docs/slack/app-manifest.yaml 初稿）
- RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-walkcode-slack-5f86008-1792507269.CF3K
- Engine: codex（gpt-6-luna · openai · effort medium，当前配置；用户授权"按推荐来"，未逐项询问）
- 维度：goalfit / consistency / feasibility / risk（信号触发）/ redteam
- Phase 2：由 host 回读源码与 Slack 官方文档核对（文档类发现，未派独立回证进程）

VERDICT: NEEDS_FIX → 已修复（见下），无 Critical

## 🔴 已采纳并修复

| # | 来源 | 发现 | 处理 |
|---|---|---|---|
| 1 | goalfit R4 | 未写明重启恢复不重放 `.processing` | 入站一节写明与飞书共用收件箱代码及全部恢复规则 |
| 2 | goalfit R12 | 缺部署文档 | 新增 `docs/slack-profile-deploy.md`（第 1 期交付），ADR 指向它 |
| 3 | risk | 5 个飞书实例一起升级、无回滚步骤 | 改为先升 work2-claude，逐个升级，失败即停，写明回滚命令；状态格式不变 |
| 4 | redteam | `chat.postMessage` 超时无法区分是否已送达 | 每条发送带 `metadata` 幂等键；可能已生效的错误按可重试处理，重发前用 `conversations.replies`/`history` 查重 |
| 5 | consistency | `topic_sessions` 与绑定 capabilities 字典表述混淆 | 写明是 `ChannelCapabilities` 新字段，由 `_new_binding_capabilities` 据此写字典 |
| 6 | consistency | "Slack 不会触发根卡降级"的说法不成立 | 改为降级逻辑对 Slack 同样生效，列出会触发它的永久错误 |
| 7 | feasibility | 新会话放置写得不明确，可能被理解为以用户消息为根 | 补"新会话放置"一段：复用中性的 `_place_new_session`，先发顶层状态卡再挂 thread |

## ❌ 已驳回

- feasibility：`input` block 不能放进普通消息。Slack 官方 input block 参考页列出的可用场景是
  "Modals, Messages, Home tabs"。保留"待实测"一条：若点按钮时 `state.values` 不全，改用 modal。

## 设计补充（审查期间 host 自行发现）

- 命令：适配器把开头的 `!` 改写成 `/`、`!!` 改写成 `//`，整条命令路径复用，核心不加 Slack 分支。
