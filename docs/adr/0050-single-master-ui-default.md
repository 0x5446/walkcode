# ADR 0050: 单 master UI 成为默认——TUI master + 飞书只读观察，双 UI daemon 退为显式 opt-in

Date: 2026-07-13

Status: Accepted

## Context

ADR 0048 把 `WALKCODE_CLAUDE_SPAWN_MODE` 默认切为 `daemon`：飞书新建会话生
而为 `claude --bg` worker，wrapper 裸启动也走 bg + attach，两端随时可写
（v3 真双端）。

实际使用暴露的问题：**双 UI 要求所有会话都活在 bg/attach（agent view）形
态下，而 attach 客户端的 TUI 渲染在双端并发写入时会乱**——注入键位、
observer attach、attach 回放与本地终端渲染相互交叠，表现为界面错位/重绘
混乱。这是官方 CLI attach 面的行为，bg 特性本身仍是 experimental，walkcode
在外面修不了，当前无解。

既然「两端同时是 master」在渲染层不成立，就退而求其次：**同一时刻只有一
端 UI 是 master，另一端只读观察**。这套互斥模型正是 V3 的基础形态，ADR
0048 之前的全部机制都在且有测试覆盖：

- TUI 会话由 `walkcode native hook` 观察，`EXTERNAL_OBSERVED_READONLY`，
  飞书只读渲染（ADR 0032）；
- 飞书输入被 block + takeover 卡，确认后终止 TUI 进程（hook 进程组推断
  `allow_terminate`）、headless resume、提交被阻塞输入（ADR 0002/0022/
  0042）→ 飞书独占；
- 终端 `claude --resume <uuid>` 后 hook 认领，`handoff_to_external_tui`
  generation +1、飞书回到只读（ADR 0031）→ TUI 夺回 master；
- 飞书新建会话 headless 出生（飞书独占），终端 resume 即转 TUI master。

## Decision

1. **默认值翻回**：`WALKCODE_CLAUDE_SPAWN_MODE` 未显式设置时解析为
   `headless`（不再依赖 `WALKCODE_CLAUDE_DAEMON_MODE` 推导）。显式
   `daemon` 仍可用（双 UI 整条链路代码与测试保留），显式 `daemon` +
   `DAEMON_MODE=off` 的矛盾组合仍在配置期报错。
2. **交互模型定调为单 master 乒乓**：TUI master ↔（takeover）↔ 飞书独占
   ↔（终端 resume）↔ TUI master。不新增机制，全部复用既有路径。
3. **wrapper 侧回归纯 TUI**（机器本地脚本，不在仓库内）：裸启动不再
   `--bg` + attach，`--resume` DWIM 一并停用（`WALKCODE_NO_BG=1`），
   `--resume` 恢复官方原义。
4. **daemon 附属机制不删除**：list 兜底收编、常驻 observer、daemon
   reply/subscribe、spawner 全部保留为 opt-in / 兜底能力。默认 headless
   下收编回到 ADR 0048 定义的纯兜底语义（用户手动 `claude --bg` 起的 job
   仍会被观察收编）。本机部署若要彻底关掉 daemon 面，继续用单变量逃生口
   `WALKCODE_CLAUDE_DAEMON_MODE=off`。

## Consequences

- 会话不再默认脱离终端存活：关终端 = TUI 会话结束；walkcode 标记
  `EXTERNAL_DETACHED_IMPORTABLE`，飞书仍可经 resume 接管复活，但不是
  bg 语义的无缝续命。
- 飞书想写必须先过 takeover 确认卡（移动端多一步）；这是单 writer 模型
  的固有代价，也正是本 ADR 的目的。
- 每次乒乓（takeover 的 headless resume、终端 `--resume`）都是 fork 语
  义、产生新 session id；walkcode 按 resume 血缘跟踪，飞书话题不变。
- 权限语义回到 headless SDK 闭环：`can_use_tool` 进程内闭环、
  always-allow 持久化恢复、`WALKCODE_CLAUDE_PERMISSION_MODE` 重新经 SDK
  注入。ADR 0048 列出的 daemon 模式权限退化不再默认生效。
- takeover 时 TUI 内 pending 的权限/问答框按 ADR 0042 标 stale，不代答。
- ADR 0049 的 choose 对话框注入取证工作服务于 daemon 注入路径，默认关闭
  后优先级下降；daemon 作为 opt-in 仍在，工作不作废。
- attach 渲染混乱若未来在官方 CLI 侧修复，翻回 daemon 默认只需还原本
  ADR 的默认值一行 + wrapper，机制层无迁移成本。

## Verification

- 配置解析：默认 headless、显式 daemon 生效、显式 daemon+off 报错、
  `DAEMON_MODE=off` 单变量逃生口维持 headless——
  `tests/test_channel_native_daemon_spawn.py` 对应用例已随本 ADR 更新。
- runtime spawner：无 env 覆盖时 `_spawn_claude_daemon_native_session`
  返回 None（走 headless SDK 兜底路径），显式 daemon 仍产出外部 TUI 形
  态会话。
- 全量单测绿；本机按新默认重启实例后 `walkcode native doctor` 的
  `claude_daemon.spawn_mode` 应显示 `headless`。
