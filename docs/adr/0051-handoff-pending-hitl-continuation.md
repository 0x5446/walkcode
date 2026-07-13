# ADR 0051: handoff 时 pending HITL 的续接语义——认领卫生 + 隐形 continue 注入

Date: 2026-07-13

Status: Accepted; implemented（continue 注入默认 off，待真机验证重问率后翻默认）

## Context

ADR 0050 把默认交互模型定为单 master UI 乒乓：TUI master ↔（takeover）↔
飞书独占 ↔（终端 resume 认领）↔ TUI master。两个方向的 handoff 都可能撞上
**pending 的 HITL 提示**（AskUserQuestion / 权限确认）：

- **方向一（takeover）**：TUI 被杀时对话框还挂着。现状：pending HITL 被标
  stale + 发过期卡、绝不代答（ADR 0042）；但 resumed headless 会话此后
  **静默等待**——模型上一轮的提问悬空，没人推它继续，用户看到的是"接管
  成功然后没动静"。
- **方向二（终端 resume 认领）**：headless worker 还阻塞在 `can_use_tool`
  的 future 上等飞书卡片作答。现状：认领只 bump generation（点旧卡会被
  STALE_GENERATION 拒绝，安全性没问题），但 (a) worker 挂到权限超时才
  deny 释放，白占资源；(b) 飞书旧卡不翻面，看起来还能点，点了才知道失效。

两个方向的理想体验（用户 2026-07-13 提出）：handoff 后那个问题应该"自己
回来"——新 master 端重新弹出可答的提示，旧端的卡明确作废。

关键机制事实（本 ADR 实现前已代码核实）：

- kill/放弃时 transcript 里 `tool_use` 已落盘、`tool_result` 悬空，resume
  fork 后模型"知道"有问题没被回答；注入一条明确要求重发的指令即可高概率
  触发重问——但这是**概率行为不是机械重放**，新卡≈旧卡而非全等。
- walkcode 只渲染 agent 事件，自己提交的输入不会出现在飞书话题里——
  "channel 侧不可见"免费达成；但合成输入会进 transcript，日后终端
  resume 看历史能看到，文案必须中性。
- `ClaudeHeadlessTransport.shutdown` 自带 `fail_pending_default_deny`：
  对阻塞中的 worker 是立即 deny 收尾，正是认领时需要的释放语义。
- 纯 TUI 没有注入面（V3 原则：绝不往活 TUI 里注文字），方向二的"自动
  continue"不做也做不了；且用户人就在终端，随手一句话就是 continue。

## Decision

1. **方向二认领卫生（无条件，默认行为）**：hook 认领结构化会话
   （`handoff_to_external_tui` 分支）时，新增
   `Orchestrator.settle_hitls_for_external_claim`：
   - 对认领前的 transport handle **best-effort `shutdown`**（模式
     `external_tui_claim`）——pending 的 `can_use_tool` future 立即按
     deny 解除，不再等权限超时；旧 runtime 遗留的 worker 本来就不在，
     失败只落 `external_claim_shutdown_failed` degrade 日志；
   - 认领前 generation 的 pending HITL 全部标 stale，并按 takeover 同款
     `hitl_stale` 视图翻卡，reason 明示「已在终端接管，请到终端作答」。
2. **方向一隐形 continue 注入（`WALKCODE_HANDOFF_CONTINUE=auto|off`，
   默认 `off`）**：takeover 完成路径上，当且仅当
   - 本次 takeover 是 **takeover-only**（无伴随用户消息——带消息的
     takeover 由消息本身驱动续接，注入会双重提示），且
   - stale 清扫**确实清到了 pending HITL**（没有悬空提问的空闲接管必须
     保持安静），且
   - 配置为 `auto`
   时，向 resumed transport 注入合成 turn `HANDOFF_CONTINUE_PROMPT`
   （幂等键 `handoff_continue:<takeover_id>`），随后照常 drain。注入
   失败不影响 takeover 成功语义，落 `handoff_continue_failed` degrade。
   文案要点：中性（不说"异常 kill"，避免模型道歉/绕路）、明确指令重发
   未答提问（把重问率从"大概率"拉到"接近必然"）。
3. **可观测性**：`describe()` / doctor 文本输出新增 `handoff_continue`
   行；配置解析期校验取值（非 `auto|off` 报 ChannelConfigError）。
4. **默认值策略**：`off` 起步。翻 `auto` 的门槛是真机 Live E2E 验证：
   takeover-only + pending AskUserQuestion 场景下，注入后飞书出现新可答
   卡且旧卡已翻 stale、无双重提问。验证过后另行翻默认（一行）。

## Consequences

- 方向二从"点了才知道卡死了 + worker 干等超时"变成"卡立即翻面 +
  worker 立即释放"；纯确定性收益，无行为开关。
- 方向一开启后，takeover-only 场景的悬空提问会以新卡片形式"回来"，
  接近用户期望的无感续接；代价是每次命中多一个模型 turn，以及重问措辞
  可能与原问不完全一致。
- 合成输入对 channel 不可见但对 transcript 可见——终端 resume 看历史会
  看到 `[ui-handoff]` 行；文案已按此设计。
- 带消息的 takeover 行为不变（消息驱动续接，模型需要答案时自会重问）。
- Codex 侧同样受益：注入走 transport 抽象（`submit_turn`），shutdown 走
  getattr best-effort，对没有该能力的 transport 自动跳过。

## Verification

- 单测（678 全绿）：
  - 配置：默认 off / 显式 auto / 非法值报错；
  - 方向一：auto+pending+takeover-only 注入 `HANDOFF_CONTINUE_PROMPT`；
    默认 off 不注入（stale 清扫照跑）；auto 无 pending 不注入；带消息
    takeover 只提交用户消息不注入；
  - 方向二：认领后 worker 收到 `shutdown("external_tui_claim")`、HITL 翻
    stale、飞书收到含「resumed in a terminal TUI」的过期通知。
- 待做（翻默认前）：真机 Live E2E——飞书生 headless 会话触发
  AskUserQuestion → 状态卡 takeover-only →（env 置 auto）确认新卡重现、
  旧卡翻面、终端 resume 后 transcript 中 `[ui-handoff]` 行无歧义。
