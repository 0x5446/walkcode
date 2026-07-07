# ADR 0048: 会话生命周期大一统——飞书新建会话 daemon 化 + list 兜底收编 + wrapper resume DWIM

Date: 2026-07-07

Status: Accepted; implemented 2026-07-07 — spawn 路径真机 E2E 通过（真 work
daemon：spawner 起 bg job → 首轮 daemon reply 注入 → turn 跑完 transcript
验证 → kill 收尾），单测 650 绿。`WALKCODE_CLAUDE_SPAWN_MODE` 默认 headless，
飞书 Live E2E（真实例开关）后再切 daemon。

## Context

ADR 0046 v3 落地后，同一 profile 里存在三种会话形态：

| 形态 | 出生方式 | 飞书能力 | 终端能力 |
|------|---------|---------|---------|
| daemon bg 会话 | wrapper 裸启动（`claude --bg` + attach） | 读写全通（v3 真双端） | attach |
| walkcode headless 会话 | 飞书新建（SDK） | 全通（SDK 进程内闭环） | 无面（handoff 需停 worker） |
| 普通 TUI 会话 | 裸 `claude` / 带参调用 | 只读 + takeover | 原生 |

用户实际撞上的裂缝（2026-07-07）：对一个还活着的 bg 会话习惯性
`claude-work --resume <uuid>`，官方 CLI 拒绝（"currently running as a
background agent"）。**resume 的语义只剩「复活死会话」，活会话的正确动词是
attach**——但肌肉记忆没跟上，且飞书生的 headless 会话终端侧根本没有 attach
面。三种形态三套心智模型，不统一。

前置事实（本 ADR 实测补充，全部真机验证 2026-07-07）：

- `claude --bg` 在**无 TTY、无 CLAUDECODE** 的子进程环境可用（rc=0，输出
  `backgrounded · <short>`；stdout 在 FORCE_COLOR 环境下带 ANSI 色码，解析
  必须先 strip）；
- daemon `list` 含 `sessionId`（完整 uuid）、`cwd`、`createdAt`、
  `source`（CLI 起的 = `"shell"`，含 Agent-tool bg 子代理，**不可用于区分**）；
- `reply` 对 idle 新生 job 有效——首轮消息可注入并驱动完整 turn；
- `claude --bg --resume <uuid>` 复活死会话为 bg worker（fork 语义、新
  session id），`claude stop` 干净收尾；
- daemon `dispatch` op 的 `d` spec 是 CLI 内部结构（Zod 校验、未文档化），
  **不逆向**——`claude --bg` 就是官方稳定面，效果等同。

## Decision

目标心智模型：**活会话一律 attach/daemon 读写；resume 只用于复活死会话；
复活也复活成 bg（保双端）。**

1. **飞书新建会话 daemon 化**（`WALKCODE_CLAUDE_SPAWN_MODE=daemon`，默认
   `headless`）：orchestrator 新增 `daemon_spawner` 钩子，飞书首条消息建会话
   时先走 runtime 的 `_spawn_claude_daemon_native_session`——
   `ClaudeDaemonTransport.spawn_bg_job`（子进程 `claude --bg`，注入与 headless
   spawn 相同的 `--settings` tap/base-url 覆盖，env 去 `CLAUDECODE` 加
   `CLAUDE_CONFIG_DIR`）→ list 拿完整 sessionId → **按外部 TUI 形态预注册**
   （writer external_tui + 嵌套 claude resume_ref + `daemon_short` +
   `daemon_live`，binding 打 `origin=daemon_spawn` 标记）→ 首轮及后续消息
   自动走已验证的 v3 daemon reply 写路径；内容渲染走 hooks（hooks 按
   resume_ref 认领预注册会话）；权限/问答走 v3 dual gate。任何失败
   `_log_degrade` 后返回 None，**headless SDK 路径原样兜底**。
2. **list 兜底收编**（`WALKCODE_CLAUDE_LIST_ADOPT=auto`，默认开）：daemon
   watcher 的 list 轮询发现「walkcode 不认识的活 job」（hook 没配 / spool
   丢 / `claude --bg` 起完还没发首条 prompt）时，按 hook 同款外部观察形态
   补建会话（观察 binding、嵌套 resume_ref、`daemon_live`）。保守过滤：仅
   `source=shell`、job 年龄 > 30s（`createdAt`，让 spawner 永远先注册赢下
   自家会话）、resume_ref 双重去重（ingress lock 内二次确认）。
3. **wrapper `--resume <id>` DWIM**（3 个 claude wrapper，机器本地脚本）：
   仅拦「恰好 `--resume/-r <hex-id>` 两参」的调用——daemon 里活着 → 转
   `claude attach <short>`（匹配优先 sessionId 前缀，attach 用 entry 自己的
   id：named agent 的 id ≠ uuid 前缀）；死了 → `claude --bg --resume` 复活
   再 attach，打印 fork 出的新 session id（walkcode 经 hooks/list 自动收编）。
   `--fork-session` 等任何额外参数、非 hex id 原样透传；
   `WALKCODE_NO_BG=1` 跳过；`WALKCODE_RESUME_DWIM_DRYRUN=1` 只打印决策。
4. **门禁与守卫**：`spawn_mode=daemon` 与 `WALKCODE_CLAUDE_DAEMON_MODE=off`
   组合在配置解析期报错；`_ensure_tui_observed_binding_capabilities` 对
   `origin=daemon_spawn` 的 binding 直接豁免——飞书生会话的 binding 是用户
   自己的话题，不能被 hook 认领路径重涂成只读观察话题。

## Consequences

- 三形态收敛为两形态：daemon bg（飞书生 + 终端生 + 收编的野生）与
  headless（兜底/逃生口）。两端统一 attach 模型，takeover 进一步边缘化。
- **权限语义变化**（spawn_mode=daemon 时）：headless SDK 的
  `can_use_tool` 进程内闭环（含 `updated_permissions` 持久化 always-allow）
  换成 v3 dual gate——always_allow 降级为 runtime 进程内记忆（ADR 0046 v2
  已明示）；`WALKCODE_CLAUDE_PERMISSION_MODE`（如 acceptEdits）不再经 SDK
  注入，会话用 profile 默认 permission mode。这是切换默认值前 Live E2E 要
  重点验的两点。
- 无人 attach 的 bg 会话原生对话框照常渲染（协议文档 §1.6.6），飞书卡片经
  attach 注入作答——v3 机制不变，只是第一 attacher 从终端变成注入连接。
- 收编的野生会话含 Agent-tool bg 子代理（`source=shell` 区分不了）；hooks
  今天本来就观察它们，收编只是把发现从「首个 hook 事件」提前到「30s 内的
  list 轮询」，不引入新类别的噪音。
- `claude --bg` 是官方 CLI 面但 bg 特性本身 experimental：spawn 失败整体
  回落 headless，行为与 ADR 0046 的 probe 门禁一致。
- wrapper DWIM 改变 `--resume` 的字面语义（活会话不再报错而是 attach）；
  逃生口齐全，且官方原行为（报错）本来就是死路。

## 实施与验证记录（2026-07-07）

- 代码：`claude_daemon.py`（`spawn_bg_job` + `parse_backgrounded_short`）、
  orchestrator `daemon_spawner` 钩子、runtime
  `_spawn_claude_daemon_native_session` / `_maybe_adopt_wild_claude_daemon_job`
  / `_tui_observed_session_id`（与 hook 建会话共用）、配置门禁两枚。
- 单测：`tests/test_channel_native_daemon_spawn.py` 24 例（解析/ spawn 失败
  路径/配置校验/orchestrator 钩子/runtime spawner 形态/收编过滤/binding
  豁免），全量 650 绿。
- 真机：POC（`claude --bg` 无 TTY + reply 首轮注入 + transcript 验证 +
  kill）与 runtime 级 E2E（真 work daemon 走 spawner → submit_user_input →
  `daemon_reply` → transcript 含标记 → headless 零调用）均通过；wrapper
  DWIM 活/死/透传三分支 pty 下验证（死分支真实复活 fork 实测）。
- 未做（后续）：飞书 Live E2E（真实例 `WALKCODE_CLAUDE_SPAWN_MODE=daemon`
  开关 + Playwright 点卡全场景，重点：权限卡、AskUserQuestion、stop 流），
  通过后把默认值切到 daemon；Codex 侧等价统一不在本 ADR 范围。

## Deep-review 记录（2026-07-07，14 维度 codex 引擎）

首版实现（commit b2a869b）经 deep-review 发现一个 Critical + 一批 Warning，
均已在同分支修复、656 单测绿、真机 E2E 复测通过：

1. **[Critical, 已修] ingress lock 自锁**：`_spawn_claude_daemon_native_session`
   重入 `_ingress_lock`，而 `serve_lark_ws`/`poll_telegram_once` 已在锁内调用
   `handle_inbound_event`。asyncio.Lock 不可重入 → 开启 spawn_mode=daemon 后
   首条消息必然死锁。修复：spawner 去掉重入（注册由调用方的锁串行化），补真实
   入口锁死回归测试（旧代码会 `wait_for` 超时）。首版单测/E2E 都直连绕过入口锁
   才漏掉——这是"测试形态与生产入口不一致"的典型盲区。
2. **[Warning, 已修] spawn_bg_job 孤儿 job**：仅 ready 超时 reap，list/ready 探测
   抛异常时漏 kill。修复：整个 ready-wait 包统一 try/except，任何非正常返回都
   best-effort kill 并记 `claude_daemon_spawn_reap_failed`。
3. **[Warning, 已修] 注册失败孤儿 job**：spawn 成功后 create/authz 抛异常不回收。
   修复：注册段 try/except + `_reap_daemon_job`。
4. **[Warning, 已修] 收编年龄阈值竞态**：30s < spawn 最坏窗口，watcher 可能误收
   自家在途 job 再被 spawner 当 duplicate kill。修复：阈值提到 60s（> ready
   超时 20s + 轮询余量），补 45s 边界不收编测试。
5. **[Warning, 已修] watcher 内联收编阻塞**：收编的网络副作用挡住已知会话的
   subscribe 建立。修复：已知会话先建 watcher，未知 job 收编推后；收编前加锁外
   预检消除最常见的孤儿 binding 竞态。
6. **[Warning, 已修] 可观测性**：reap 静默吞错、`_describe_claude_daemon` 不暴露
   spawn/adopt 策略。修复：reap 失败落 stderr；describe 加 `spawn_mode`/
   `list_adopt`/`daemon_spawner_installed`。
7. **[Warning, 已修] 文档陈旧**：设计文档旧 takeover/`--resume` 语义、lark 部署
   漏写收编依赖 `WALKCODE_LARK_TUI_CHAT_ID`。已更新。

**已知并接受（未在本 ADR 修）**：
- **spawn_bg_job 继承完整 os.environ**（安全维度提出）：bg worker 能读到
  `LARK_APP_SECRET`/`TELEGRAM_BOT_TOKEN` 等。这与现有 headless SDK spawn 行为
  **一致**（后者同样把完整 os.environ + `CLAUDE_CONFIG_DIR` 传给 worker），且
  wrapper 裸启动的会话也继承登录 shell 全 env——非本 PR 新增的洞。收敛 env 需
  同时改 headless 与 daemon 两条 spawn 路径并逐一验证各 profile 认证不回归，
  留作独立硬化 PR。
- **list 收编的信任边界**：收编按 `source=shell` 过滤，无法区分 wrapper 会话与
  Agent-tool 子代理；但收编只作用于**本 profile 的 daemon**（同 `CLAUDE_CONFIG_DIR`），
  即用户自己经该 profile 起的会话树。默认 `auto`，可 `WALKCODE_CLAUDE_LIST_ADOPT=off`
  关闭。默认 spawn_mode=headless 时收编是纯兜底。
