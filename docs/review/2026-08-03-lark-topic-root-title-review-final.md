# Deep Review 终版结论 — Lark 话题根标题

VERDICT: SAFE

**轮次**：3 / 3（round 1 报 3 条、round 2 报 3 条，均已修；round 3 唯一剩余项判定为存量）
**类型**：code

> 范围：给飞书话题根一个有意义的标题。(1) TUI observed 的 Lark 话题根从纯文本换成 health 卡片，根即 status card；(2) 新增统一入口 `Orchestrator._maybe_refresh_session_title`，四条 turn 结束路径都汇入，用来源分级 + 同级节流决定标题覆盖；(3) `_status_card_fingerprints` 的 key 改为 `(message_id, fingerprint)`。
> Review engine：codex codex-cli 0.144.5（host: claude; engine_source: auto；模型：gpt-5.6-sol effort=xhigh）
> Cursor：disabled（composer-2.5 smoke test failed）
> 维度：round 1 = correctness / goalfit / maintainability / conventions / concurrency；round 2 = correctness / goalfit / maintainability；round 3 = correctness
> Phase 2 验证：round 1 派 3 条，全部 VERIFIED，0 误报
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 9273576cb35957ec67e9e6294a3b9a0c3e1e14c6
> RunDir: round1 `…-1785743106.G7Kk` / round2 `…-1785744974.U4wq` / round3 `…-1785745835.NKmp`
> 规模：终版 diff 约 1600 行 / 8 文件
> 模式提示：报告模式 + 人工修复（未走 --fix 自动循环）
> 验证状态：1021 tests OK；ruff 与基线持平（172 = 172），新测试文件 0 error

## 三轮发现与处置

### Round 1（3 条 Warning，全部 Phase 2 VERIFIED）

| # | 问题 | 处置 |
|---|---|---|
| 1 | codex app-server 真实 `turn/completed` 不带 message，标题路径拿不到素材；原测试用 FakeAgentTransport 伪造完成事件，假绿 | ✅ 已修：drain 循环累积 TURN_DELTA 作 fallback；新增用真实 `CodexAppServerTransport` + 真实协议序列的回归测试（已人工验证：摘掉 fix 即红） |
| 2 | 根卡片兼任 status card 后，edit 失败的既有兜底会把指针挪到子卡，话题根永久停在旧标题 | ✅ 已修：`_root_card_edit_may_retry` 守卫 |
| 3 | ARCHITECTURE.md / ADR 0044 未同步 | ✅ 已修 |

### Round 2（3 条 Warning）

| # | 问题 | 处置 |
|---|---|---|
| 1 | 累积器三个洞：上限非硬上限（transport 会把整批 delta 合成一个不限长事件）、`SESSION_ERROR` 也是回合终点却不清空导致跨回合污染、空白 message 因 `or` 短路遮蔽累积正文 | ✅ 已修：按剩余额度切片、两种终点都清空、`.strip()` 后再判断 |
| 2 | 根卡片永久失效（被删/撤回/超编辑窗）会每事件重试且状态卡彻底停摆——这是 round 1 修复引入的新故障模式 | ✅ 已修：`ROOT_CARD_EDIT_RETRY_BUDGET`（=3）+ `PermanentDeliveryError` 直接放弃 + 耗尽后降级到子状态卡；成功编辑清零计数 |
| 3 | 文档把 `root == health` 写成无条件不变式，但 `_place_lark_new_session` 建根卡失败时会退回消息为根 | ✅ 已修：两份文档改为 best-effort 表述并列出降级路径 |

### Round 3（1 条，判定为存量，不计入本次裁决）

**降级发送失败后仍会每次刷新重试**（correctness，0.99，reviewer 标 PreExisting: no）。

**本方判定：PreExisting: yes，本次不修。** 依据：

- `git show HEAD` 对比确认，`send_view` 失败的处理（`_log_degrade(..., drop=True)` + `return`，不写指纹）**改动前后逐字相同**。无界重发是 `refresh_session_status_card` 的既有机制，对所有 channel、所有 status card 一视同仁，与本次目标无关。
- 本次引入的重试预算针对的是 round 1 修复自己制造的新故障模式（根卡片 edit 永久失败），那一条已闭环。把预算扩展到 send 侧会改变所有 channel（含 Telegram）的 status card 失败语义，属于范围蔓延。
- 且简单计数熔断会把「吵闹地重试」换成「静默地永久失效」——未必更优，需要单独设计带时间窗口的退避，不适合塞进本次改动。
- 承认的不对称：edit 侧有熔断而 send 侧没有。建议另开任务统一 status card 的失败退避策略（连同 P1 竞态、P2 缓存不清理一起做）。

## 🟣 Pre-existing（存量，另开任务）

- **P1**（round 1，Warning）：`refresh_session_status_card` 跨 await 的 read-modify-write 竞态，并发刷新时旧视图可覆盖新视图。本次让它从「进度行滞后」升级为「标题回退」这种用户可见现象。修法：按 session 加异步锁。
- **P2**（round 1，Suggestion）：`_status_card_fingerprints` 不随 session 停止/归档清理，长期运行无界增长。
- **P3**（round 3，本报告判定）：status card `send_view` 失败无界重试，无退避无熔断。

三条同属「status card 刷新链路的健壮性」，建议合并为一个后续任务。

## 💡 Suggestion（已知，本次未做）

- 三条 Lark 建根路径各自造卡（`_place_lark_new_session` / TUI observed 建根 / rootless heal），标题清理逻辑三套，应抽共享 helper。
- 标题来源用裸字符串散落在 rank 表、rolling 集合、生成器、runtime 赋值四处，应收敛为枚举。
- `_RealProtocolCodexClient` 与 `tests/test_channel_native_codex.py` 的 `_FakeCodexClient` 重复。

## 维度元信息

| 轮次 | 维度 | VERDICT | issues | 备注 |
|---|---|---|---|---|
| 1 | correctness | NEEDS_FIX | 2 | 全部 VERIFIED |
| 1 | goalfit | NEEDS_FIX | 3 | |
| 1 | maintainability | NEEDS_FIX | 4 | |
| 1 | conventions | NEEDS_FIX | 1 | |
| 1 | concurrency | NEEDS_FIX | 2 | 首轮 5 并发超时，单跑重试成功；2 条均 PreExisting |
| 2 | correctness | NEEDS_FIX | 3 | |
| 2 | goalfit | NEEDS_FIX | 2 | |
| 2 | maintainability | NEEDS_FIX | 3 | |
| 3 | correctness | NEEDS_FIX | 1 | 唯一一条经核对为存量，见上 |

## 裁决

本次变更引入的问题已全部修复并有针对性回归测试保护；剩余项均为改动前既已存在、且与本次目标无关的存量问题，按 deep-review 的 PreExisting 原则不计入本次裁决。

**VERDICT: SAFE**（存量问题另开任务跟踪）

## 原始报告

- round 1：`/var/folders/…/deep-review-walkcode-9273576-1785743106.G7Kk/`（dim-*.md、verify-{1,2,3}.md）
- round 2：`/var/folders/…/deep-review-walkcode-9273576-1785744974.U4wq/`
- round 3：`/var/folders/…/deep-review-walkcode-9273576-1785745835.NKmp/`
- 首轮报告：`docs/review/2026-08-03-lark-topic-root-title-review.md`
