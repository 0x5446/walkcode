# ADR 0058: 升级自杀陷阱守卫 + 丢失回合自动重放

Date: 2026-07-20

Status: Accepted; implemented

## Context

2026-07-20 15:13 事故（同类问题 07-19 已发生过一次）：飞书接管的 headless
会话自己执行 `./upgrade.sh`，脚本 `launchctl kickstart -k` 重启全部
com.walkcode.* runtime——包括正在驱动这个会话的那个。驱动进程被 SIGTERM，
会话中断在工具调用一半（transcript 里表现为"被用户打断"），upgrade 的
回显、版本确认、doctor 全部没能回到会话。飞书静默开始。

模型侧无法可靠避开：它只能猜宿主类型（这次猜的是"safe: iTerm-hosted"，
猜错了）。upgrade.sh 已有 tap-* 硬守卫（拒绝重启承载 API 流量的代理），
但没有"别重启正在驱动当前会话的 runtime"守卫。

第二层：15:47 用户再发消息，ADR 0054 复活如期触发，但复活后的 worker
撞上模型 API 故障窗（500/529，15:47–16:14 实测），死于回答之前。当时唯一
的信号是一句走事件流的"请重发"——又被 ADR 0051 代际围栏丢弃（继任
generation 已就位）。用户在飞书上盲等半小时，直到自己去终端 resume。

## Decision

### 1. 自杀陷阱守卫（upgrade.sh + runtime 标记面）

- `walkcode native serve` 启动时把自己的 launchd label 写进自身环境：
  `WALKCODE_DRIVER_LABEL=com.walkcode.<env 文件名去 .env>`（已有值不覆盖）。
  worker 派生整体继承 os.environ（`claude --bg` 与 SDK spawn 皆然），于是
  会话内每个子进程——包括 Bash 工具里跑的 upgrade.sh——都能识别
  "驱动我的 runtime 是谁"。
- upgrade.sh 检测 self-driver：优先读 `WALKCODE_DRIVER_LABEL`；退化路径
  （旧 runtime）沿进程树向上找 `walkcode native serve` 祖先并经
  `launchctl list` 映射 label（LC_ALL=C，v0.14.4 的 locale 教训）。
- 重启集合命中 self label 时**跳过立即 kickstart**，改为脱管延迟重启：
  `python3 subprocess.Popen(start_new_session=True)` 派生独立会话进程
  `sleep ${WALKCODE_SELF_RESTART_DELAY:-120}; launchctl kickstart -k ...`，
  能活过它即将投递给我们祖先的那记 SIGTERM。脚本明说：几秒后会中断、
  请提前收尾、之后发消息触发复活。deferred label 照跑 per-env doctor。

### 2. 丢失回合自动重放（orchestrator）

- headless worker EOF 且有已接受未回答的提交时，SESSION_ERROR 事件带
  结构化标记 `reason=pending_turn_lost`（不再只靠文案）。
- `submit_user_input` 成功提交后暂存 `(turn, actor, attempt, 盖章水位)`
  （空提交只盖水位、无内容可重放，不暂存；daemon 直写路径 TUI 持有 UX，
  不暂存）。in-memory：runtime 重启后下一条新消息本来就走复活。
- orchestrator 在事件排水中看到 `pending_turn_lost`：按
  `TURN_REPLAY_DELAYS=(30, 300)` 调度自动重放，文案改为"N 秒后自动重发"；
  退避用尽则明说"已尝试 N 次仍未成功，请稍后重发"。
- 重放触发时的让位围栏（全部静默让位，UX 归继任者）：
  - 会话被外部 TUI 认领（EXTERNAL_OBSERVED_READONLY）或正在跑回合（ACTIVE）；
  - 水位越过暂存时盖的章（有更新的人话，容差 0.5s——等值是自己盖的章）；
  - 重放提交被拒（时效守卫/只读/接管中……）只打 degrade 点。
- 重放提交自身抛异常时**直发**渠道错误通知（不走可能被围栏丢弃的事件
  流），之后交还给人。degrade 打点：`turn_replay_attempt` /
  `turn_replay_rejected` / `turn_replay_failed`。

## Consequences

- 会话内发起升级不再自断驱动：其余实例立即重启，自己的驱动延迟脱管
  重启，升级回显能完整回到会话并给用户预告。代价：self 实例的新版本
  生效晚 ~2 分钟（可用 WALKCODE_SELF_RESTART_DELAY 调）。
- API 故障窗内的复活不再"两次空转后永远沉默"：短退避接瞬时抖动，长
  退避跨典型过载窗；超出重试预算时用户至少得到一句实话。
- 重放对齐 ADR 0057 水位语义：任何更新的人类输入都让重放自动失效，
  不存在"旧消息追着新会话跑"的回归。
- 已知边界：会话缺 durable resume ref 时重放会被拒（degrade 可查），
  这类会话本来也无法复活；跨 runtime 重启的重放不做（复活路径已覆盖）。
