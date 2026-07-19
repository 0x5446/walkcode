# ADR 0056: 关闭 worker 必须验尸——断开 SDK client 不等于进程退出

Date: 2026-07-20

Status: Accepted; implemented

## Context

实锤事故（2026-07-20 凌晨）：一个会话累计挂着三个 headless worker 进程
（22:58 / 23:13 / 00:21 各拉起一个，前两个从未退出），其中一个甚至与用户
的终端 TUI 同时持有会话。后果有二：

1. 残留进程占着 Claude Code 的**同会话单进程锁**，用户在终端
   `claude --resume` 一启动就被弹出（当晚连撞数次，最初被误判为
   "TUI 自动退出"的玄学问题）；
2. 潜在双写——ADR 0049/0053 全力防的场景，被我们自己的残留进程复现。

根因（日志实证，非猜测）：`headless_worker_close_failed/exhausted` 为
0——所有关闭调用都"成功"了；但 `headless_worker_eof_with_background_tasks`
×3——**worker 还挂着后台子进程（当晚的 codex 审查）时，关闭 SDK client
只是关掉了管道，CLI 进程等它的后台子进程、并不退出**。walkcode 的关闭
路径（resume 换代、EOF 清算、shutdown）从不验证进程真死了。

## Decision

所有关闭路径汇聚点 `_disconnect_client` 增加**验尸 + 按身份升级清理**：

- 拉起（launch/resume 成功后）即捕获 worker 进程身份三元组
  `(pid, lstart, command)`——pid 从 SDK client 内部
  （`client._transport._process.pid`）尽力取得，取不到则放弃跟踪；
- 关闭后先给 1.5 秒宽限（健康 CLI 关管道即退，不会挨信号）；
- 仍存活且**身份匹配**才升级：SIGTERM → 等 2 秒 → SIGKILL → 等 1.5 秒，
  每步之间持续探测；
- ADR 0053 规则不打折：探测报错（`error`）**绝不动手**（fail-closed）；
  身份不匹配即视为 pid 复用、worker 已死，同样不动手；
- 全程 degrade 日志：`headless_worker_terminated_after_close` /
  `headless_worker_exit_verify_failed` / `headless_worker_kill_failed` /
  `headless_worker_survived_kill`。

## Consequences

- 换代 resume、EOF 清算、显式 shutdown 后，旧 worker 进程保证消失：
  终端 resume 不再被残留进程挡死，双写隐患关闭。
- 泄漏场景下关闭多付约 1.5–3.5 秒（宽限+信号等待）；健康场景首次探测
  即返回，几乎无感。
- worker 里被 SIGTERM 的后台子进程（如审查任务）成为孤儿继续跑完，
  结果文件照写，但其完成通知不会有人消费——本就是被放弃的任务，符合
  既有语义（`headless_worker_eof_with_background_tasks` 已宣告放弃）。
- 残留：serve 自身崩溃时来不及验尸——依赖进程组随 serve 一起被 launchd
  终止（当日升级实测成立）；跨 serve 的历史孤儿不做全局清扫（无登记
  身份，杀之违反 fail-closed 原则）。

## Revision（发版前两轮审查采纳）

R1 三项 P1：①验尸改为按 handle 的单例后台任务并 shield——调用方（EOF
清算/shutdown）被取消也照样跑完，同 handle 并发关闭共享任务不重复发信号；
②记录只在终局（gone / pid 复用）清除，探测报错与 SIGKILL 未死时保留，
后续关闭尝试能重新验尸；③resume 增加同会话闸门，等上一个已知 worker
到达终态。另：拉起路径身份捕获异步化。

R2 收口：①关旧→验尸→建新→登记→捕获整段加**同会话串行锁**（launch 与
resume 都持锁），并发 resume 不再双双越过闸门各拉一个 worker；②闸门要不
到"确认死亡"（记录仍在=探测报错/杀不死）时 resume 直接抛
TransportUnavailable 拒绝拉新，下一条消息重试整个阶梯；③注销回调按任务
身份条件删除（消 ABA 竞态）；④shield 会重抛验尸自身异常——在关闭路径
隔离并打点，不放大/替换 EOF 原始异常；⑤捕获前确认 handle 仍登记在册，
关闭后的迟到捕获不会复活跟踪记录。

R3 终轮：①拉起时身份捕获对瞬时 ps 失败重试 3 次，仍失败则打点
`headless_worker_capture_failed`——该 worker 退回 ADR 前的无跟踪行为
（残留：审查建议的"未决状态阻塞后续拉新"在 pid 复用时会把会话永久卡死，
不采纳，fail-closed 优先）；②lark 路径给"拒绝拉新"补用户反馈文案
（resume_failed→"稍等几秒再发一次"），不再静默丢消息。
