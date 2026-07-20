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

### 1. 自杀陷阱守卫（upgrade.sh + walkcode upgrade + runtime 标记面）

- `walkcode native serve` 启动时把自己的 launchd label 写进自身环境
  `WALKCODE_DRIVER_LABEL`（已有值不覆盖）。label 真源是
  `_launchd_service_label(channel_kind, agent, profile)`——与 install 写
  plist 同一函数；配置推不出时才退化到 env 文件名（R1：env 文件名只是
  文档约定，改名部署时按它推导会导出错误标记）。worker 派生整体继承
  os.environ（`claude --bg` 与 SDK spawn 皆然），于是会话内每个子进程——
  包括 Bash 工具里跑的 upgrade.sh——都能识别"驱动我的 runtime 是谁"。
- **两个升级入口同守卫**（R1：`walkcode upgrade` CLI 与 upgrade.sh 功能
  等价，缺一即复现事故）：优先读 `WALKCODE_DRIVER_LABEL`；退化路径
  （旧 runtime）沿进程树向上找 `walkcode native serve` 祖先并经
  `launchctl list` 映射 label（LC_ALL=C，v0.14.4 的 locale 教训）。
- 重启集合命中 self label 时**跳过立即 kickstart**，改为脱管延迟重启：
  `subprocess.Popen(start_new_session=True)` 派生独立会话进程
  `sleep $DELAY && exec launchctl kickstart -k ...`，能活过它即将投递给
  我们祖先的那记 SIGTERM。加固（R1）：延迟值先校验（非数字/负数回退
  120 并警告）；`&&` 保证 sleep 失败绝不落到立即重启；调度放在 doctor
  之后、脚本收尾处，倒计时不与尾部工作赛跑。
- **精确时序**：安装新版 → 立即重启其他实例 → 逐实例 doctor（此时 self
  实例仍是旧版，doctor 不验证重启后状态）→ 调度脱管重启 → 脚本结束 →
  配置延迟（默认 120s）后 self 实例重启，**当前会话仍会中断一次**（不再
  "立即"自断，不是"不再"中断），之后下一条消息触发复活。脚本输出明说
  这一点，让会话有时间把收尾结论发完。

### 2. 丢失回合自动重放（orchestrator）

- headless worker EOF 且有已接受未回答的提交时，SESSION_ERROR 事件带
  结构化标记 `reason=pending_turn_lost`，并附 `traffic_seen`（该回合是否
  已流出过输出/工具事件）与 `pending_lost`（丢失条数）。
- **重放安全的分界是 traffic_seen**（R1 Critical 共识）：回合已经流出过
  delta/工具事件意味着副作用可能已发生（删密钥、发消息、部署……），
  自动重放等于重复执行——此时作废暂存、明说"不自动重发，请确认现场
  后再重发"。只有零流量死（本次事故正是：API 故障窗内模型一字未出）
  才自动重放。
- `submit_user_input` 成功提交后暂存 `(replay_id, turn, actor, attempt,
  盖章水位)`。replay_id 是身份钉子：新提交覆盖暂存后，还睡在退避里的旧
  重放任务靠它识别自己已被取代——比水位容差硬，堵死"旧消息在 0.5s
  容差窗内追着新会话跑"的路（R1 复现 old-new-old）。空提交不暂存且
  **作废旧暂存**（它是更新的人类输入）。daemon 直写路径 TUI 持有 UX，
  不暂存。in-memory：runtime 重启后下一条新消息本来就走复活。
- orchestrator 在事件排水中看到 `pending_turn_lost`：按
  `TURN_REPLAY_DELAYS=(30, 300)` 调度自动重放，文案改为"N 秒后自动重发"
  （`pending_lost>1` 时明说只自动重发最后一条，更早的请自行重发）；
  退避用尽则明说"已尝试 N 次仍未成功，请稍后重发"。
- 重放触发时的让位围栏（每条都有 degrade 标记，杜绝"承诺了自动重发
  然后无声消失"）：
  - 身份钉子不匹配（暂存已被更新提交覆盖）→ `superseded`；
  - 会话被外部 TUI 认领或正在跑回合 → `lifecycle`；
  - 水位越章（有更新的人话，容差 0.5s 只是兜底）→ `watermark_advanced`。
- 重放提交被拒分两类：所有权类（时效守卫/只读/接管中）静默让位；故障
  类（`resume_failed` / `missing_resume_ref` / `transport_not_wired`）
  **直发**渠道通知——用户拿着承诺在等，没有继任者会替我们说话。重放
  提交抛异常同样直发。degrade 打点：`turn_replay_attempt` /
  `turn_replay_skipped` / `turn_replay_rejected` / `turn_replay_failed` /
  `turn_replay_exhausted` / `turn_replay_refused_partial_execution`。

## Consequences

- 会话内发起升级不再**立即**自断驱动：其余实例立即重启，自己的驱动
  延迟脱管重启，升级回显能完整回到会话并给用户预告。代价：self 实例
  的新版本生效晚 ~2 分钟（可用 WALKCODE_SELF_RESTART_DELAY 调），且
  当前会话在延迟到点时仍会中断一次、由复活机制接管。
- API 故障窗内的复活不再"两次空转后永远沉默"：短退避接瞬时抖动，长
  退避跨典型过载窗；超出重试预算时用户至少得到一句实话。
- 重放让位以 replay_id 身份钉子为主围栏、水位容差为兜底：任何更新的
  人类输入（含空提交）都让旧重放失效，不存在"旧消息追着新会话跑"。
- 已知边界（评审后接受）：
  - 重放暂存不跨 runtime 重启持久化——300s 档赶上重启会湮灭，代价是
    一次"请重发"；复活路径覆盖重启后的新消息。
  - 多条消息同一 worker 内全部丢失时只自动重发最后一条（单槽暂存），
    文案明说其余需人工重发；按提交逐条建账留作后续演进。
  - 缺 durable resume ref 的会话重放被拒并直发通知，这类会话本来也
    无法复活。
  - 延迟重启是定时的，不感知"到点时是否正有新回合在跑"；被切断的
    回合由 traffic_seen 分界决定重放或如实报告。

## R2 修订（2026-07-20 收口轮采纳）

- **traffic_seen 语义精化**：真源改为"非注入回合的流量"（`user_turn_traffic`）。
  注入回合（通知重放等 CLI 发起）的输出不代表排队中的用户消息动过手——
  按旧口径会误拒安全重放；浮出的权限事件计入流量——获准的工具可能已
  产生副作用，按旧口径会漏判并重复执行（R2 Critical）。非注入回合终局
  时清零。
- **暂存前置**：`_remember_replayable_turn` 挪到 transport 提交**之前**
  （水位值预计算、失败按 replay_id 回滚）。否则提交等待期间 EOF，排水
  看到的暂存还是上一条已完成消息，会误重放它（R2 Critical 复现）。
- **replay_guard 终审**：重放提交在 writer 恢复等 await 之后、真正发出
  之前，最后一刻复核身份钉子；被更新提交覆盖即返回 `replay_superseded`
  自灭（R2 Critical 复现 new-then-old）。残余窗口收窄到 transport 提交
  自身的单次 await，接受。
- **延迟调度收尾化 + 依赖加固**：两个入口的脱管调度都放在全部输出之后
  （零/短延迟也不会砍掉收尾消息）；CLI 延迟校验加 isascii（全角数字会
  让 sleep 静默失败）；upgrade.sh 缺 python3 时拒绝重启 self（提示手动
  kickstart），绝不退化为立即重启。
- **无法代码修复、发布说明承接的版本偏差**：从 ≤0.14.9 首次升级时，
  正在运行的旧版 `walkcode upgrade` / 旧 runtime 里的会话不受新守卫保护
  （旧代码已加载，换盘上文件不热更新）。首次升级请在会话外执行，或用
  仓库最新 upgrade.sh；此后升级由标记面全覆盖。
- **遗留边界（记录，暂不做）**：长生命周期 codex app-server daemon 若
  先于新 runtime 启动并被复用，其 worker 缺环境标记且祖先链不含
  `walkcode native serve`——codex 会话的守卫要等 daemon 重启后生效；
  CLI 脱管重启子进程失败仍静默（与 shell 同边界）。

## R3 修订（2026-07-20 终验轮采纳，双怀疑论进程交叉）

- **resume 重试补终审**（双进程同报 Critical）：首发 `TransportUnavailable`
  后 writer 恢复的 await 窗口里，更新提交可覆盖暂存并先行发出——二次
  发送前对 replay_id 再终审一次，失配即 `replay_superseded` 自灭。
- **水位单次预计算**：登记与成功后落章用同一个 `_effective_input_stamp`
  值。落章时重读时钟会让无时间戳消息（Telegram 等 created_at=0）在慢
  提交后越过自己的水位、自我封禁重放。
- **回滚改还原**：提交失败时把暂存还原到提交前状态（含空提交 pop 掉的
  旧条目），不是光删——上一条仍在等回答的有效暂存不能陪葬。
- **注入回合权限归属**：注入回合开着时浮出的权限事件不再置位用户流量
  标记；回合归属不明时仍保守计入（宁可错杀重放，不可重复副作用）。
- **调度前把话说完**：两个入口都先输出（CLI 加 flush）再启动脱管定时器，
  零/短延迟不再可能砍掉收尾提示。
- **R3 后仍接受的边界**：多条排队提交共享一个 traffic_seen 聚合布尔——
  第一条有流量会连坐第二条的重放资格（安全方向：只损失便利，不产生
  重复副作用；逐条建账留作演进）。
