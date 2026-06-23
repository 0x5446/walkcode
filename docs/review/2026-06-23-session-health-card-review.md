# Deep Review:会话健康卡片方案

**VERDICT**: NEEDS_FIX(1 Critical + 多 Warning)
**类型**: design(纯方案审查) · **轮次**: 1(--plan-only,不自动改码)
**Review engine**: codex 0.141.0(host: claude;6 维度并行,medium effort)
**Repo**: walkcode · **HeadSHA**: e33724d · **Branch**: feature/session-health-card
**目标**: plans/lazy-bouncing-whisper.md(会话健康卡片设计)

6 维度均 NEEDS_FIX,共 32 条 issue。归并去重 + 用已读源码回证后得 13 个有效问题,基本全 VERIFIED,无明显误报。

## 🔴 Critical(改设计)

### 1. 状态判定用错信号:is_agent_alive 不能判任务运行/停止
- 共识:feasibility(0.95)/consistency(0.9)/completeness(0.86)/clarity(0.85) **4 维度** · 回证 VERIFIED
- 问题:`is_agent_alive`(tty.py:328)判的是 pane 当前命令非 shell,即"进程在不在"。Claude/Codex 任务完成后进程仍在等下一次输入,`is_agent_alive` 仍为 true → 永远显示 running、进不了 DONE、权限等待也显示 running。
- 修:running/stopped 以**轮次状态**为主,用现有 `_is_session_busy(session_id)`(server.py:1379-1404,基于 UserPromptSubmit↔Stop 时序);HITL_WAITING 优先级要能覆盖 running;`is_agent_alive` 仅辅助区分 stopped 是 DONE 还是会话已死。

## 🔴 高置信必修(Warning,源码确认)

### 2. has_open_request 误判失败/过期权限为 HITL_WAITING
- 共识:feasibility(0.88)/consistency(0.84)/risk(0.72) · 回证 VERIFIED
- 问题:permreg `card_failed`(302-315)设 card_status=FAILED 但 decision/invalidated_at 仍 None,gc 只在 register/consume/显式调用时触发(无后台清理)。纯查 `decision is None and invalidated_at is None` 会把已失败/超时未清理的请求算作等待,最多误报 90s。
- 修:`has_open_request` 在锁内先 `_gc_locked()` 再判,且排除 `card_status != READY`、`fallback_claimed`。

### 3. Session 字段更新必须走 SessionStore 锁内持久化
- consistency(0.74) · 回证 VERIFIED
- 问题:`get()`(123)/`items()`(130)返回 `Session(**to_dict())` 副本,直接改不落盘 → 重启/冻结/解冻全失效。
- 修:set_title/set_status/set_health_card 做成 SessionStore 锁内方法(改 _sessions[sid] + _save_locked)。

### 4. 健康卡失败不能波及主转发 + 缺 kill switch
- risk(0.86 + 0.82) · 回证 VERIFIED
- 问题:文本发送有重试/redelivery(_send_with_status),卡片路径(794-833)直接调无同等保护。在主建根路径建卡失败会害首条回复进不了话题。且无总开关,出问题只能回滚发版。
- 修:默认 **Branch B**(根保持普通消息);健康卡在根+首条回复成功后再建;所有卡片调用 try/except 返回可分类状态,失败清 card_id 继续文本话题。加健康卡**总开关**(env),关闭完全回退现有行为、已持久化卡片字段只读忽略。

### 5. 飞书发起路径建卡时机错(pending 阶段无 session_id)
- 共识:feasibility(0.9)/consistency(0.88)/completeness(0.88) · 回证 VERIFIED
- 问题:`add_pending`(state.py:263)只存 root_msg_id/reply_id/cwd,session_id 要等首个 hook。plan 写"DM-start 用户消息根(1251)建卡"时还没 session_id,卡片会失联。
- 修:飞书发起的健康卡在**首个 hook 的 pending 分支**(pop_pending 拿到 session_id 后)创建,不在 _start_agent。

### 6. summarizer 不能拖慢 Stop 回执
- risk(0.8) · 回证 VERIFIED
- 问题:`_msg_executor` 是 max_workers=1 给飞书消息回调;Stop hook 在 receive_hook 直接处理。summary 占用单 worker 或在 Stop 路径同步调会拖慢主回复/重投。
- 修:用独立后台 executor + 短超时 + 熔断;Stop 主回复先按现有路径发,summary 成功只更新卡片标题,失败用本地首行兜底。

### 7. 性能:60s 全量解析无预算
- 共识:feasibility(0.82)/completeness(0.83)/risk(0.74) · 回证 VERIFIED(现有读取整文件 read_text,rollout 曾见 8MB+)
- 修:增量采集——Session 存 transcript/rollout 路径、mtime、offset、累计 token;Stop/sync 时更新统计,poller 只读缓存 + 轻量补采;单会话超时/最大字节/失败退避/冻结不解析;超预算显示"统计不可用"而非阻塞。

## 🟡 中置信改进(单维度,质量好)

- **8. ERROR 判定无规则**(completeness 0.8/clarity 0.8):定义 ERROR 来源表(各 agent 字段/日志模式/排除项/优先级/不可判断降级 DONE),覆盖正常结束/用户中断/启动失败/鉴权失败/工具错误/空响应。
- **9. resume 解冻条件无边界**(completeness 0.82):只在确认新进程恢复同一会话时解冻(SessionStart source=resume / resume 成功),列出调用点;普通 stop/notification/permission/redelivery 不解冻。
- **10. 14天过期卡片无恢复路径**(risk 0.78/completeness 0.9):过期清 card_id+解冻,Branch B 下限次重建线程内健康卡,重建失败发一次文本提示并停。
- **11. 时长起止点未定义**(clarity 0.8):start=transcript/rollout 首条 timestamp,end=末条(冻结时);重启/恢复/缺字段兜底。
- **12. Codex token 模型名来源**(clarity 0.9):取 sqlite threads.model 或 turn_context.model;缺失兜底 "unknown";Codex 单模型,拆分用 rollout token_count。

## 🟢 扩展性建议(Suggestion,适度采纳,不过度抽象)

- extensibility 1-4:采集挂到 AgentAdapter(统计提供器,避免 collect_stats 内按名分支)、卡片用标准化视图模型、原因用检测器列表、summarizer 拆提供器接口、summary 凭证显式可选配置。
- 采纳:summary 凭证/开关显式配置化(必做,见 #4/#6);原因检测器列表(轻量,利于加 ERROR 子类);采集层留 agent 派发点。其余(视图模型抽象、提供器接口)对内部工具属过度设计,暂不做。

## 处理决定

Critical #1 + 高置信 #2–#7 全部纳入,更新 plan 后再写代码;中置信 #8–#12 纳入;扩展性按上方"适度采纳"。无 unresolved Critical 残留后方进入实现。
