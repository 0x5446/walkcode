# Deep Review 综合结论：headless 提交归属与 takeover 卡终态（v0.14.1，PR #64）

**VERDICT**: SAFE（3 轮 + 定向对抗回证后收敛；全部 Critical 与回证 BROKEN 项已修复）
**轮次**：3 / 3 + 对抗回证 1 次
**类型**：code（8 维）

> 起因：2026-07-18 09:31 实机事故——takeover 后 resume fork 的注入回合 result 误消
> 「已提交未上流」标记，settle 杀掉排队中的用户回合（"yes" 无回复）。
> Review engine：codex（host: claude/Fable；r1-r2 gpt-5.5-test，r3 起 gpt-5.6-sol，均 xhigh）
> Repo: /Users/alpha/workspace/walkcode；分支 release/v0.14.1
> RunDir：…-d8a07cd-*.nE7c（r1）/ …-1f37aa5-*.IX57（r2）/ …-7939781-*.bXAe（r3）/ …-8455920-verify-*（回证）

## 演进（每轮把上一版方案证伪，最终形态是回合计数器模型）

- **初版（d8a07cd）**：时间归属（result 时间 vs 提交时间 + 开场 user 消息判注入）。
  r1 证伪（3 Critical）：yield 暂停窗口可绕过、typed 通知无 user 开场、单一标记无法表达连发。
- **v2（1f37aa5）**：`_pending_turns` 计数器（提交 +1 / 非注入回合 result -1）、
  注入判定、状态推进移 yield 前、pending 无响应 ceiling 兜底；takeover 卡成功翻面。
  r2 证伪（6 Critical→2 根因）：pending 超时钟误用流静默基准；注入回合带正文时标记被冲掉。
- **v3（7939781）**：`_last_submit_monotonic` 仅供计时；`injected_turn_expected` 粘性预告
  （窗口内下一回合无论开场流量都算注入，过期失效）。
  r3 证伪（7 Critical→2 根因 + 边角）：提交后零流量进程死亡静默丢消息且会话卡 ACTIVE；
  预告过期只在唤醒时检查。
- **v4（8455920）**：EOF 零流量丢提交 → 可见 SESSION_ERROR；过期改在回合归类时判定；
  takeover 失败/转人工也翻终态卡；flip 对 False 返回重试；submit 失败回滚时钟；
  interrupt 无参签名兼容；ceiling 文档口径修正。
- **对抗回证（4 怀疑论者）**：1 CORRECT + 3 BROKEN（窄残留）→ **v5（45f01c0）收口**：
  legacy 零流量 EOF 同样报丢失；drain 围栏加 handle 归属（旧流尾事件不再覆盖新 worker 状态）；
  takeover 非 TakeoverError 异常路径也翻卡；控制调用改签名绑定（不再吞方法内部 TypeError、
  不再二次调用）；时钟 CAS 回滚。

## 已知权衡（记录在案，不阻塞）

- 注入窗口（30s）内到达的「裸 result 用户回合」会被按注入处理，延迟到 ceiling 才收尾
  （条件极窄：回复无任何正文/工具流量且恰在窗口内）。
- pending 计数按 handle 聚合而非逐条绑定时间；连发场景超时告警以最新提交时钟为准。

## 验证

- 全量单测 **749 通过**（本 PR 新增回归 40 例中的 22 例）
- 每轮修复后实机冒烟（真 SDK + CLI 2.1.211）**7/7**，共 5 次
- 反向验证：核心回归用例在各前置版本上均失败
