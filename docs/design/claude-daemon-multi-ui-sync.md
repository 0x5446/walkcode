# Claude Daemon 多端同步读写方案（1 daemon / 多 UI 订阅读写）

Date: 2026-07-04（v2: 2026-07-05；v3 方案: 2026-07-06）
Status: v1 已实现（reply 写路径 + subscribe 状态同步）；v2 已实现（PreToolUse gate 权限/AskUserQuestion 飞书闭环 + 状态卡/回显整改，见「交互闭环 v2」）；v3 **已实现并通过 Live E2E**（2026-07-06，attach 按键注入实现真双端，Step 0–5 完成、单测 621 绿、work 实例 Playwright 点卡全场景验收——推翻 v2「双端同时可答不可行」结论，见文末「交互闭环 v3」）
Protocol grounding: `docs/design/daemon-appserver-protocol-reference.md`（2026-07-04 实测，Claude Code v2.1.201, proto 1）

## 目标

同一个 Claude Code 会话（daemon worker），终端 TUI 和飞书卡片可以同时读、同时写：

- 飞书发消息 → 通过 daemon `reply` 注入会话，TUI 里如同用户亲手输入；
- TUI/会话状态变化 → 通过 daemon `subscribe` 实时推送，驱动飞书健康卡片与权限提醒；
- 不再需要 takeover（kill TUI + headless resume）——那条路径与 Claude ≥2.1.2xx
  的 daemon worker 架构持续冲突（见 c132da3），且丢失 TUI。

## 现状问题（被替换的机制）

| 现状 | 问题 |
|------|------|
| hooks 观察（`process_tui_hook`，spool 每秒 drain） | 只读；有秒级延迟；权限提醒卡片只能提示"去终端处理" |
| takeover = `LocalProcessController.terminate` + headless `resume` | 杀 TUI，终端侧体验中断；与 daemon worker 进程模型互搏（pgrep 扫尾等补丁） |
| 观察会话 `EXTERNAL_OBSERVED_READONLY` | 飞书端永远要先确认 takeover 才能说话 |

## 适用范围修正（2026-07-05 真机发现）

Claude daemon 只托管 **bg 会话**（`claude --bg` 启动，或 TUI 内 `/bg` 转后台，
再经 `claude agents` attach → pty-host 呈现）。普通 `claude` 交互式 TUI 是
独立进程，不注册为 daemon job、不出现在 `list`、无法 `reply` 直写。因此：

- **能多端读写的**：bg 会话（终端经 pty-host attach + 飞书经 reply，双端同写）；
- **仍走 hooks + takeover 的**：普通 TUI 会话（daemon 探测失败自动回落，
  日志 `claude_daemon_reply_failed ... fallback=takeover_prompt`）。

要让日常会话获得多端同步，用 `claude --bg` 起会话或对现有 TUI 会话 `/bg`
后重新 attach。没有 bg 会话的 profile 不会有 daemon 进程，属正常现象。

> **ADR 0048 更新（2026-07-07）**：上表「观察会话永远要先 takeover」「普通 TUI
> 仍走 hooks + takeover」描述的是 daemon-native 之前的默认。现在：
> (1) `daemon_live` 的外部观察会话首选 daemon `reply` 直写，只有非 daemon 的
> 普通 TUI 观察会话才回落 takeover；(2) 飞书**新建**会话默认生而为 daemon
> bg worker（`WALKCODE_CLAUDE_SPAWN_MODE` 默认 `daemon`，2026-07-07 飞书
> Live E2E 通过后切换；`headless` 为逃生口，`DAEMON_MODE=off` 自动降级
> headless）；(3) wrapper 的 `--resume/-r <hex-id>` 不再原样透传，而是按意图
> 处理（活会话 attach、死会话 bg 复活），见 ADR 0048 与 lark-profile-deploy.md。

## 协议依据（已实测验证）

- socket 路径可确定性推导：`/tmp/cc-daemon-<uid>/<sha256(CLAUDE_CONFIG_DIR)[:8]>/control.sock`。
  实测 `sha256("/Users/alpha/.claude-profiles/work")[:8] == "19e5f12f"` 与实际目录一致。
  路径必须是 expanduser 后的绝对路径、无尾部斜杠。
- 认证 key：`<CLAUDE_CONFIG_DIR>/daemon/control.key`（32 hex，owner-only）。
- 读路径：`subscribe`（免认证）→ `snapshot` / `state`(patch: tempo/state/needs/detail) / `stream`(ANSI，忽略) / `settled`。
- 写路径：`reply`（需认证）→ 文本注入正在运行的会话，等价 TUI 输入回车。
  错误码：`EAUTH` / `ENOJOB` / `ENOREPLY`（会话非交互态）。
- `list` / `has`（免认证）：发现会话、确认 job 存活。
- `ping`（免认证）：版本与 proto 校验（proto != 1 → 降级 hooks 模式）。
- `permission-response`：二进制里存在，schema 已实测（`{proto, op, short, requestId, auth, allow}`），
  但 handler 是**空壳**——只校验 auth 后返回 `{ok:true}`，不转发给 worker（真机验证：
  发送后被 gate 的会话仍阻塞）。**不可用**；审批闭环走 PreToolUse gate（v2 章节）。

## 设计

### 分层

```
channel_native_runtime.py
  ├── 发现/订阅维护任务 _watch_claude_daemon_forever
  │       list 轮询发现新 TUI 会话（补充 hooks）；
  │       每个活跃 claude TUI 会话挂一个 subscribe watcher
  └── wiring: _build_transports 注册 "claude_daemon" transport

channel_native/claude_daemon.py           ← 新模块，自包含、可单测
  ├── claude_daemon_socket_path(config_dir)
  ├── ClaudeDaemonClient                  ← ndjson unix socket 协议客户端
  │       ping / list_jobs / has / reply / kill / subscribe(async iter)
  │       control.key 惰性读取；每请求一条连接；subscribe 长连接
  └── ClaudeDaemonTransport (AgentTransport)
          submit_turn → reply；multi_client_observe/write = True
          launch/resume 之外的控制操作按能力位关闭
```

### 会话模型：不改注册表状态机，改写入路由

TUI 会话仍由 hooks 创建为 `external_tui` + `EXTERNAL_OBSERVED_READONLY`（内容
渲染管线不动：用户输入回显、turn 输出、工具进度都来自 hooks，文本干净）。变化
只有两处：

1. **写路由**（Orchestrator.submit_user_input）：`validate_submit` 返回
   `EXTERNAL_TUI_READONLY` 时，先尝试 daemon 直写——会话 resume_ref 能映射到
   存活 daemon job（`has` alive+ready）就 `reply` 注入并直接成功返回；
   daemon 不可用（老版本 Claude / proto 不符 / job 已死）才回落原 takeover 流程。
   写入成功后不做 writer 所有权变更：TUI 继续持有会话。注入的输入会以
   user-prompt-submit hook 回流，但 v2 起由回显去重消费掉——发送者看到的
   是"✅ 已发送到终端会话"回执，而不是自己的话被复读。
2. **状态同步**（subscribe watcher）：state patch 驱动会话生命周期与健康卡：
   - `tempo=active` → ACTIVE；`tempo=idle` → EXTERNAL_OBSERVED_READONLY（可写，
     命名沿用但语义已是"外部 TUI 会话"）；
   - `needs` 语义分两类（v2 修正）：`approve <Tool>: <detail>` / `tempo=blocked`
     才是权限阻塞 → WAITING_PERMISSION + 橙色提醒卡；idle 型 needs（如
     "send a prompt to start"）只记 progress，**不**触发等待态（v1 曾把所有
     非空 needs 当权限阻塞，产生空闲误报橙卡）；
   - needs 从 approve 清空 → 回 READONLY 并发"✅ 已在终端处理"同步文本
     （终端侧决策回传飞书）；
   - `settled` → 标记 stopped。state patch 同时在 `transport_ref.daemon_live`
     打活跃标记，供停止守卫与状态卡使用。
   `stream`（ANSI）事件忽略，内容仍走 hooks —— 协议文档 §3.2 的推荐用法。

这样注册表、takeover 状态机、卡片管线全部保留，回退路径零成本。

### 发现（list 轮询，hooks 的补充而非替换）

hooks 仍是会话创建的主通道（能拿到干净的 cwd/transcript）。list 轮询解决的
是"hook 没配 / spool 丢失"时的兜底发现，以及为已知会话维护 watcher 的启停。
v1 范围：**watcher 只为已存在的 walkcode 会话服务**（resume_ref ↔ JobRecord.sessionId
匹配），list 兜底建会话作为后续步骤（已由 ADR 0048 落地，见「本期不做」）。

### 能力位

`ClaudeDaemonTransport.capabilities()`：

| 能力 | 值 | 原因 |
|------|----|------|
| structured_input | True | reply 文本注入 |
| structured_output | False | 内容输出走 hooks，不走本 transport 事件流 |
| permission_callback / ask_user_question | True（配置了 gate spool 时） | v2：决策写 gate 决策文件，由阻塞 hook 消费；daemon 自身的 permission-response 是空壳 |
| interrupt / set_model / set_permission_mode / checkpoint_rewind | False | 协议无对应操作 |
| resume_after_complete | True | reply 对 idle 会话有效 |
| multi_client_observe / multi_client_write | True | 本方案核心 |
| external_tui_takeover | False | 被本方案取代 |
| requires_single_writer | False | daemon 自己序列化输入 |

### 配置

- `WALKCODE_CLAUDE_DAEMON_MODE=auto|off`（默认 auto）：auto = socket 存在且
  `ping` 返回 proto 1 时启用；off = 完全走旧 hooks/takeover。
- socket 路径由 `WALKCODE_CLAUDE_CONFIG_DIR`（缺省 `~/.claude`）推导；
  不新增 socket 路径配置项，与 5-instance 矩阵（每 instance 一个 profile、
  一个 daemon）天然对齐。

### 安全与降级

- `ping` 校验 `proto==1`，不符则 daemon 模式整体禁用（描述信息进 doctor）。
- reply 前先 `has` 确认 alive+ready；`ENOREPLY`/`ENOJOB` 时回落 takeover 流程。
- control.key 只在进程内存读取，绝不落日志/卡片。
- 附件仍按 headless 的约定：下载到本地后以文件路径注记拼进 reply 文本。

### POC 已验证：/bg 不 kill 双端共写（2026-07-05，完整成功）

pty harness 驱动真实 TUI 完成全流程（`/tmp/poc_bg_tui.py`、
`/tmp/poc_attach_dualwrite.py`）：

1. 普通 TUI 会话内执行 `/bg`（= `/background`，官方描述 "Send this session to
   the background and free the terminal"）→ 会话无中断转为 daemon job（turn
   进行中也能转），TUI 释放终端；
2. `claude attach <short>` → 终端端恢复，历史完整可见；
3. 外部（飞书端）用生产 `ClaudeDaemonClient.reply` 注入 → **attach 的终端
   实时渲染注入的输入与回答**；
4. 终端端继续手输下一轮 → 同样正常。

即「终端 attach + 飞书 reply/subscribe」双端读写同一 session 完全成立。

工程要点（实现时处理）：
- `/bg` 是 TUI 客户端本地命令，**无法从外部远程触发**（普通 TUI 无 IPC 面；
  嵌套 Claude 环境变量还会导致 "session persistence is disabled" 拒绝 /bg，
  POC 中已踩过）。飞书侧只能引导用户在终端敲一次 `/bg`；无人值守场景仍需
  bg 化 takeover（kill + `--bg --resume`）兜底。
- `/bg` 为 fork 语义：产生**新 session id**（例：80c7c2d6-345c-...）。新
  worker 的 session-start hook 会让 walkcode 建新会话/新卡片；如需沿用原
  飞书 topic，须做 fork 父子映射（后续实现项）。

### 已落地：daemon-native wrapper（2026-07-05，端到端验证通过）

`/bg` 无法外部触发的最终解法在 wrapper 层：**让会话从出生就是 daemon worker**，
TUI 只是 attach 上去的视图。三个 wrapper（`~/.local/bin/claude-personal` /
`claude-work` / `claude-work2`）已改造：

```
裸交互启动（无参数 + tty + 非嵌套）：
  claude --bg（空启动，"idle — send a prompt to start"）
  → 解析 short id → exec claude attach <short>
其余情况（带任何参数 / -p / --resume / 子命令 / CLAUDECODE 嵌套 /
WALKCODE_NO_BG=1）：原样 exec claude "$@"
```

相比「启动→首轮→/bg」的优势：无浪费轮次；**无 fork 换 id 问题**（会话生而
即 bg，一个 id 走到底，hooks/飞书卡片/daemon job 一一对应）。

端到端实测（session 924d9d7b）：wrapper 起会话 → 终端手输正常 → 生产
client `reply` 注入 → attach 终端实时渲染注入轮次 → walkcode personal
实例经 hooks 自动建观察会话（飞书卡片就位，真实飞书回复将走直写不弹卡）。

语义变化（用户须知）：attach 模式下 `/exit` = **detach（会话继续跑）**，
Ctrl+C 只打断当前 turn；真正结束会话用 `claude stop <short>`。长期积累的
空闲 bg 会话需要偶尔 `claude agents` 清理。

### 后续方向：bg 化 takeover（2026-07-05 可行性已实测）

普通 TUI 会话的 takeover 可改为「kill TUI → `claude --bg --resume <uuid>` →
daemon reply/subscribe 驱动」，替代现行「kill TUI → headless SDK resume」。
实测已验证：`--bg --resume` 续传上下文完整（会话答得出前文内容）；续传后
reply/subscribe 全链路可用；用户可 `claude attach <short>` 随时拿回终端——
takeover 从不可逆变为可逆，且会话生命周期脱离 walkcode 进程。

实测发现的两个工程要点：
- `--bg --resume` 会生成**新的 session id**（fork 语义），takeover 后必须用
  roster/list 里的新 sessionId 更新会话 resume_ref；
- bg 会话可能卡在启动对话框（实测见 "stuck on a startup dialog"，如目录
  trust 提示），需 subscribe 的 blocked/needs 监控 + 提醒卡兜底。

短板：权限审批无 SDK can_use_tool 闭环，依赖 permission-response（schema
未验证）或 attach 回终端处理。方案取舍见 ADR 0046 讨论。

### 本期不做（后续步骤）

- ~~`permission-response` 闭环审批~~ → 已由 v2 的 PreToolUse gate 取代
  （permission-response 实测为空壳，见「协议依据」）。
- ~~`dispatch` 新建 daemon 会话（飞书新建会话仍走 headless SDK）~~ →
  已由 ADR 0048 落地（2026-07-07）：不逆向 dispatch 的内部 `d` spec，改用
  官方 CLI 面 `claude --bg` 子进程 spawn + 外部观察形态预注册 + 首轮
  daemon reply 注入；`WALKCODE_CLAUDE_SPAWN_MODE` 门禁
  （2026-07-07 飞书 Live E2E 通过后默认已切 daemon，headless 为逃生口）。
- ~~list 兜底自动建会话（无 hook 场景）~~ → 已由 ADR 0048 落地：watcher
  的 list 轮询收编 walkcode 不认识的活 job（`source=shell` + 30s 年龄阈值
  + resume_ref 去重），`WALKCODE_CLAUDE_LIST_ADOPT=off` 可关。
- Codex 侧持久订阅化改造（现有 per-turn drain 继续用；协议已是 app-server）。

## 测试与验证

- 单测：`tests/test_channel_native_claude_daemon.py`，用 `asyncio.start_unix_server`
  起假 daemon（ndjson 协议桩），覆盖 client 各 op、错误码映射、subscribe 事件流、
  transport reply、orchestrator 写路由（daemon 直写 vs takeover 回落）、watcher
  state patch → 生命周期。
- 真机只读验证：对本机 work profile daemon `ping`/`list`/`subscribe`（已完成，
  见协议文档附录 A）。
- 真机写验证：起一次性测试 TUI 会话，飞书侧 reply 注入 echo，确认 TUI 呈现。

## 实施步骤与状态

| # | 步骤 | 状态 |
|---|------|------|
| 1 | `claude_daemon.py`：ClaudeDaemonClient（socket 推导、ndjson、各 op、subscribe） | 完成 |
| 2 | ClaudeDaemonTransport（AgentTransport 实现 + 能力位） | 完成 |
| 3 | runtime 接线：transport 注册、`WALKCODE_CLAUDE_DAEMON_MODE`、subscribe watcher 维护任务 | 完成 |
| 4 | Orchestrator 写路由：daemon 直写优先，takeover 回落 | 完成 |
| 5 | 单测 + 真机验证 | 完成（见下） |
| 6 | ADR 0046 定稿、本文档状态收尾 | 完成 |

### 验证记录（2026-07-04）

- 单测：`tests/test_channel_native_claude_daemon.py` 26 例（假 daemon unix socket
  服务覆盖 client 各 op / EAUTH / ENOREPLY / subscribe 事件流 / transport reply /
  orchestrator daemon 直写与三种 takeover 回落 / watcher 状态同步 / 配置门禁），
  全量套件 503 通过、零回归。
- 真机（work profile daemon, Claude Code 2.1.201）：用生产 `ClaudeDaemonClient`
  跑 `probe`（proto 1 校验通过）、`list_jobs`（2 个活跃 job）、`subscribe`
  （收到 snapshot，长连接保持；静默会话无后续事件属预期）。
- 真机端到端写入（2026-07-05，personal profile）：`claude --bg` 起一次性会话
  `5a13a58e`（daemon 按需拉起，socket 推导 `6937f06e` 与实际一致）→ 生产
  client `reply` 注入新输入 → `{'ok': True}` → `subscribe` 实时收到
  `tempo=active, detail=<注入文本>` → `tempo=idle, state=done, detail=
  "replied with requested four characters: 验证成功"`。写路径全链路打通。
- 回落路径真机验证（2026-07-05）：普通 TUI 会话（非 daemon job）走飞书发消息，
  日志出现 `claude_daemon_reply_failed ... fallback=takeover_prompt`，takeover
  卡正常弹出——降级行为符合设计。
- 澄清：Claude 的 `/remote-control` 是 claude.ai 云桥（需 claude.ai 登录/订阅/
  org policy/rollout gate），与本地 daemon 控制协议无关；走本地协议的
  remote-control 是 Codex 侧（`codex remote-control start` 暴露 app-server）。
- 待办门禁：合并 main / 发版前按仓库规则跑 `/deep-review`（walkcode-release
  skill 已内置该门禁）。

### 落地代码索引

- `src/walkcode/channel_native/claude_daemon.py` — client + transport（新增）
- `src/walkcode/channel_native/__init__.py` — `WALKCODE_CLAUDE_DAEMON_MODE` 配置、
  `_external_claude_resume_ref`、`Orchestrator._try_external_daemon_reply` 与
  `submit_user_input` 写路由分支
- `src/walkcode/channel_native_runtime.py` — transport 注册（`_build_transports`）、
  `_watch_claude_daemon_forever` / `_sync_claude_daemon_watchers` /
  `_watch_claude_daemon_job` / `_apply_claude_daemon_state_patch` /
  `_settle_claude_daemon_session`、describe 的 `claude_daemon` 状态块
- `tests/test_channel_native_claude_daemon.py` — 全部新增用例

## 交互闭环 v2：PreToolUse gate（复用 headless 闭环，2026-07-05）

v1 落地后真机暴露五个交互问题（截图复盘）：飞书消息被以 "TUI input" 回显一遍；
活跃 daemon 会话被误标"已结束"并挂着失效 Take over；空闲会话误弹权限橙卡；
权限/AskUserQuestion 只有"去终端按"死胡同；英文 idle 通知原样透传。v2 一次性
整改，核心是把 headless 的权限/问答闭环搬到 daemon/TUI 会话上。

### 核心洞察：can_use_tool 的进程外孪生是 PreToolUse hook

headless 闭环 = SDK `can_use_tool` 回调：建 Future → float 事件发卡 → 等人点 →
决策转 `PermissionResult` 返回。daemon/TUI 会话没有进程内回调，但 PreToolUse
hook 是阻塞 hook 且支持完全相同的返回结构（POC 实测，bg worker 内成立）：

| headless（SDK can_use_tool） | daemon/TUI（PreToolUse hook） |
|---|---|
| `PermissionResultAllow()` | `permissionDecision: "allow"` |
| `PermissionResultDeny(msg)` | `permissionDecision: "deny"` + `permissionDecisionReason` |
| `PermissionResultAllow(updated_input={questions,answers})` | `permissionDecision: "allow"` + `updatedInput: {questions,answers}` |
| 进程内 Future + `resolve()`（write-once） | 跨进程决策文件轮询（write-once） |

AskUserQuestion 的答案注入方式两侧一致：不是回传工具结果，而是改写工具输入
（`updatedInput.answers`），worker 不再弹终端 dialog、直接采纳答案继续。
POC 记录：hook 注入 `{"answers": {"你最喜欢的颜色是什么？": "蓝色"}}` 后
worker 零交互完成。

上层全部复用：卡片渲染（`ViewModelFactory.permission_prompt` /
`ask_user_question_prompt`）、HITL/interaction 注册（`_event_to_view`）、
token 回调路由（`_handle_callback_event`）、决策卡翻面。唯一新增的是
跨进程决策通道。

### gate spool（跨进程决策通道）

`channel_native/claude_gate.py`（纯 stdlib，hook 进程轻依赖）：

```
<state>.tui-hooks.d/gate/pending/<rid>.json     hook → runtime 请求
<state>.tui-hooks.d/gate/decisions/<rid>.json   runtime → hook 决策（write-once，硬链接原子创建）
<state>.tui-hooks.d/gate/serve.heartbeat        runtime drain 心跳（每 tick touch）
```

- `rid = tool_use_id`（PreToolUse payload 自带；PermissionRequest hook 无
  tool_use_id 且不能携带决策，不可用——发射顺序实测 PreToolUse → 权限引擎 →
  PermissionRequest）。
- pending 内容：rid/kind/tool_name/tool_input/permission_mode/session_id/
  resume_ref/cwd/created_at/deadline/hook_pid。
- 决策动作：`allow` / `always_allow` / `deny(+reason)` / `answers(+answers)` /
  `pass`（弃权 → hook 无输出 → 走原生权限流）。

### 阻塞 hook 路径（`walkcode native hook PreToolUse --gate`）

1. 先落观察 spool（等价 `--defer`，工具进度照常渲染）；
2. 判定是否 gate（见下）；不 gate → 无输出退出（原生流程不受影响）；
3. gate → 写 pending，轮询 decisions（0.25s）；心跳过期（>45s）→ 弃权
   （walkcode 服务没在跑时终端原生提示继续可用，**不失能终端**）；
4. 拿到决策 → 输出 `hookSpecificOutput`；超时（默认 1800s）→ **弃权**回落
   终端原生弹窗（`timeout_decision` 返回 pass；trace 记
   `gate_timeout_abstain`。早期版本为超时 deny，后经复盘修正为"IM 先答、
   超时终端接管"，两端都不失能）。

hook 配置必须放大 Claude 侧超时（否则 60s 默认值先杀掉 hook、静默退回原生流）：

```json
"PreToolUse": [{"matcher": "", "hooks": [{
  "type": "command",
  "command": "WALKCODE_ENV_FILE=... walkcode native hook PreToolUse --agent claude --gate",
  "timeout": 1830
}]}]
```

### gate 判定（谁被拦）

- `AskUserQuestion` **永远拦**（它就是问人的，人可能只在飞书侧）；
- 权限 gate 只瞄准会触发原生提示的工具：默认集
  `{Bash, Edit, Write, MultiEdit, NotebookEdit}` + `mcp__*`；内部/只读工具
  （Read/Task/TodoWrite/ExitPlanMode…）一律放行，避免"原生不会问的被 gate 问"；
- 豁免：`permission_mode ∈ {bypassPermissions, plan}`；acceptEdits 豁免编辑类；
  profile `permissions.allow` 规则命中（裸工具名 + `Bash(prefix:*)` 前缀规则；
  其他带参规则不求值、保守拦）。**dontAsk 刻意不豁免**（work E2E 实测教训）：
  dontAsk 的原生兜底是自动拒绝，"别在终端问"≠"别问"——它恰是飞书审批卡
  唯一能放行的场景；
- walkcode 自己的 headless worker（进程树识别）不拦——SDK can_use_tool 已在
  进程内闭环，拦了会双重提问；
- 配置：`WALKCODE_CLAUDE_GATE_MODE=auto|off|ask_only`、
  `WALKCODE_CLAUDE_GATE_TIMEOUT=<秒>`、`WALKCODE_CLAUDE_GATE_TOOLS=<逗号列表>`
  （替换默认权限 gate 集）。

### runtime drain 与回调路由

- serve 维护任务 `_drain_claude_gate_requests_forever`（1s tick，touch 心跳）：
  pending → 按 resume_ref/session_id 找观察会话 → `post_claude_gate_prompt`
  合成 `PERMISSION_REQUESTED` / `ASK_USER_REQUESTED` 事件走 `_event_to_view`
  （注册 HITL + interaction，`transport_request_id=rid`）→ 话题内发卡；
  找不到会话 / 卡发不出 → 宽限 10s 后写 `pass` 决策弃权。
- 回调：`_handle_callback_event` 原样复用，仅把 `transports[transport_kind]`
  直查改为 `_interaction_transport(session)`——`external_tui` 会话解析到
  `claude_daemon` transport，其 `approve_permission` / `answer_user_question`
  写 decisions 文件（write-once）。
- `always_allow`：hook 无法持久化权限规则（不同于 headless 的
  `updated_permissions→localSettings`），语义降为**会话级、进程内**：
  runtime 记住 (session, tool)，后续同工具 pending 直接写 allow 决策不再发卡。
  重启即忘，文档明示。

### 状态卡 / 回显 / 通知整改（对应截图五问题）

- needs 语义修正 + 终端决策回传：见「会话模型」第 2 点（v2 修正版）。
- 停止守卫：`process-exit` hook 与 stale-pid 扫描在标记 STOPPED 前先查
  daemon job 存活（`has` alive+ready）——attach TUI 退出只是 detach，记
  `external_tui.tui_detached_daemon_alive`，会话继续可写；`settled` 才是
  权威结束信号。
- Take over 按钮：`transport_ref.daemon_live` 为真时隐藏（直写可用，按钮只会
  误导）；stopped 时清标记。
- 回显去重：daemon reply 成功后记 (session, text, at)，该文本 180s 内以
  user-prompt-submit 回来时跳过 "TUI input" 卡（消费一次即失效，不误伤终端
  手输的相同文本）；同时补发 "✅ 已发送到终端会话" 简短回执。
- 通知过滤：idle 型 Notification（"waiting for your input" 等）对 TUI 观察
  会话不透传（状态卡已表达 idle）。

### 取舍与已知边界

- **被 gate 的工具审批以飞书为主**：hook 阻塞在权限引擎之前，终端此时只见
  spinner、无法按原生提示（esc 打断 turn 是终端侧唯一逃生口）。"双端任一侧
  审批"留作后续增强；终端优先的用户可 `WALKCODE_CLAUDE_GATE_MODE=off` 或
  `ask_only`。**（2026-07-06 更新：该增强已有可行机制，见「交互闭环 v3」。）**
- 非 gate 集合内的工具若仍触发原生提示，行为同 v1：橙卡提示"终端确认"+
  等待态，终端处理后回传（needs 清空）。
- hook 超时改为**弃权**（2026-07-06 起）：飞书卡在超时窗口内优先，超时后
  hook 返回无决策、终端原生弹窗接管——"飞书优先、超时落回终端"。~~真正的
  双端同时可答仍不可行（阻塞 hook 挡在原生 UI 之前是协议约束）~~
  **（此结论已被同日 attach 注入实测推翻，见「交互闭环 v3」；
  `docs/review/2026-07-06-v0.10.57-*.md` §追加变更中的同句一并作废，
  历史报告不改，以本节为准）**。超时后的
  旧卡再点选会写入决策文件但无人消费（drain 清理孤儿决策）。
- ~~决策卡翻面在真机未生效~~ → **已定位并修复（2026-07-05）**：根因是
  Lark 卡片 `config` 缺 `update_multi: true`——应用发的交互卡默认为
  "独享卡片"，而消息 PATCH 更新接口只支持共享卡片，patch 一律被拒，
  所以 Lark 渠道的决策卡翻面（permission / ask / takeover 结果卡）
  **从 headless 时代起就从未生效**（Telegram editMessageText 无此概念，
  故未暴露；c132da3 的 flip retries 修的是重试策略，没触及根因）。
  修复：`lark_cards._card_message` 的 config 增加 `update_multi: true`。
  真机验证：生产 `LarkChannelAdapter` send_view 权限卡 → edit_view 翻
  decision_result 成功，浏览器确认卡片变"🚫 已拒绝"且按钮消失。历史
  已发出的独享卡无法补救（可接受）。
- 决策文件与 pending 由 hook 退出时清理；hook 被杀（超时/断电）遗留的
  pending 由 drain 按 deadline+60s 收尾。

### 运行时重启语义（2026-07-06 补）

claude_headless 的 worker（SDK client + CLI 子进程 + in-flight can_use_tool
Future）都活在 runtime 进程内，**runtime 重启即全灭**。真机踩到的后果：重启
前发出的 ask/permission 卡还留在飞书上，点提交只落决策账、注入时 KeyError
被 WS 层吞掉——用户看到 toast 但卡片不翻、答案不生效、会话装作 running。
三项修复：

- transport 注入路径在 worker 缺失时抛 `TransportUnavailable`（不再裸
  KeyError）；callback 层捕获后发提示文本并把卡片翻成"⚠️ 卡片已失效"
  （`decision_result` 的 `action="stale"` 渲染）。
- runtime 启动时一次性 sweep：orchestrator 拥有的 claude_headless 会话若
  仍标 running，settle 为 `stop_reason=runtime_restart`（TUI 观察会话不受
  影响——daemon worker 独立于 runtime 存活）。
- `bypassPermissions` 不再摘掉 can_use_tool 桥：CLI 在 bypass 下对常规工具
  自动放行（不调回调），但对 AskUserQuestion 仍会调（真机实测），摘桥会
  顺带杀死飞书答题闭环。

另：TUI 观察会话的状态卡模型此前恒为"—"（daemon job/state patch 无 model
字段，实测确认），现从 hook payload 的 `transcript_path` 尾读最近一条
assistant 记录的 model 回填（model 为空或 stop 事件时读，避免每事件 IO）。

回执形态（2026-07-06 起）：飞书直写成功与 headless turn 提交的确认**优先
用消息表情回应**（对用户那条消息随机贴 DONE/OK/THUMBSUP/MUSCLE/APPLAUSE
之一），reaction 失败或渠道不支持时回退文本「✅ 已发送到终端会话」。
Telegram 不走该池——其 runtime 已对每条入站消息预贴 ✅，二次 reaction 会
覆盖原回执。启动 sweep 对每个 ingress 入口（lark ws / telegram 轮询 /
`--once`）都作为前置栅栏执行，进程内幂等一次。

### v2 验证记录（2026-07-05）

- POC（一次性 profile + 阻塞 hook 脚本，/tmp/wc_poc）：deny 拦截 bg worker
  工具执行成立；allow+updatedInput 注入 AskUserQuestion 答案成立（无 dialog
  直接采纳）；PreToolUse（有 tool_use_id）→ PermissionRequest（无）顺序确认；
  daemon reply 无法应答原生权限提示确认。
- 单测：新增 `tests/test_channel_native_claude_gate.py` 33 例（spool write-once/
  心跳弃权/超时、should_gate 判定矩阵、hookSpecificOutput 映射、transport 决策
  文件写入、gate_tui_hook 各弃权路径与决策回读、drain 发卡幂等/always_allow/
  不可路由 pass、needs 语义、Take over 隐藏）+ daemon 测试文件补回显去重/回执
  1 例；全量 537 通过、零回归。
- Live E2E（personal 半自动，2026-07-05）：一次性 profile + 真实 bg worker +
  `--gate` hook，手写决策文件模拟回调：answers 注入（无 dialog 直接采纳）、
  deny 拦截（文件未写）、allow 放行（文件写成）三链路全通，spool 清理干净。
- Live E2E（work 全自动 Playwright 点卡，2026-07-05）：停 launchd 用 repo 新
  代码 serve work-claude + work profile settings 切 `--gate`（测后还原），
  `claude --bg` 起真会话，Playwright 驱动飞书 web 真实点卡验收：
  1. 飞书发消息 → daemon 直写 → 终端会话响应回话题；**无 "TUI input" 回显**，
     有 "✅ 已发送到终端会话" 回执；
  2. AskUserQuestion 卡（下拉单选 + Other + 提交全部）→ 选「蓝色」提交 →
     工具行翻 ✅ → 模型按答案继续（"你选择了蓝色"），终端零交互；
  3. 权限卡 Deny → 工具被拦（文件未写）、模型收到拒绝；Always allow →
     放行（文件写成）+ **会话级记忆生效**（下一次同工具不发卡直接放行）；
  4. 状态卡：🟢 运行中、`gate.waiting:<Tool>` 进展、无 Take over 按钮；
     `claude stop` → settled → 状态转 STOPPED（`daemon_settled_killed`）。
  E2E 抓出并当场修复两个缺陷：**dontAsk 误入 gate 豁免集**（dontAsk 原生
  兜底是自动拒绝，恰是飞书卡唯一放行通道——已改为不豁免）；**subscribe
  单行超 asyncio readline 64KB 上限**导致 watcher 重连循环（`_connect`
  limit 提到 16MB）。另修 readonly 文案：daemon 直写可用时状态卡改示
  "🔁 双端同步中"（原"接管后才能发消息"误导）。

### v2 落地代码索引

- `src/walkcode/channel_native/claude_gate.py` — gate spool + 判定 + 输出映射（新增）
- `src/walkcode/channel_native/claude_daemon.py` — transport 决策写入 + 能力位
- `src/walkcode/channel_native/__init__.py` — `_interaction_transport` 回调路由、
  `post_claude_gate_prompt`、daemon reply 回执 + 回显去重、gate env 配置、
  `_status_card_actions` daemon_live 守卫
- `src/walkcode/channel_native_runtime.py` — `gate_tui_hook`（阻塞 hook CLI）、
  `drain_claude_gate_requests(_forever)`、needs 语义修正、daemon 存活停止守卫、
  idle 通知过滤
- `src/walkcode/__main__.py` — `native hook --gate` 参数
- `tests/test_channel_native_claude_gate.py` — 全部新增用例

## 交互闭环 v3：attach 按键注入实现真双端（2026-07-06，已实现）

### 背景：v2 的核心取舍被新事实推翻

v2 的形态是"单端活"：阻塞 gate 期间飞书可答、终端只见 spinner；超时弃权后
终端原生对话框接管、飞书旧卡失效。当时结论是"双端同时可答不可行——阻塞
hook 挡在原生 UI 之前是 hook 协议约束"。这个结论只看了 hook 面，漏了
daemon 控制面的第三条路：**`attach` op 支持多路接入（协议文档 §1.6.6
Multi-attach），第二个 attacher 写入的原始 PTY 字节能直接驱动原生对话框**，
与真人在终端敲键盘完全等价。

### 实测依据（2026-07-06，真机三组实验）

1. **带 gate 的 personal bg 会话**：AskUserQuestion 原生对话框弹出后，以
   `attachId=injector` 的第二连接注入 `b"2"` → 对话框立即选中并推进
   （subscribe 观测 blocked→idle）。
2. **无 gate 的 `~/.claude` bg 会话**：对话框 +8.6s 渲染（纯模型延迟、无任何
   人为等待），subscribe `needs` 给出
   `answer: Which fruit? (apple · banana · cherry)`（问题 + 有序选项标签），
   注入 `b"3"` → 立即 resolved。
3. **注入层级**：直接发原始字节（`b"1"`/`b"2"`/`b"3"`），经 attach 握手后的
   raw PTY 通道进入 worker 的终端输入处理器——这一层不区分对话框类型，权限框
   走同一键盘通道（该 profile 为 auto 模式自动放行、未能现场逼出权限框，
   键位映射留待 Step 0 实测对齐）。
4. **原生对话框无自动超时**：无限等待键盘输入（v2 里"等几分钟没反应"是
   阻塞 gate 在挡，不是对话框超时）。对话框在无人 attach 时也照常在 job 的
   PTY 内渲染，`needs` 照常出现——注入不依赖终端是否有人挂着。

单选对话框的关键性质：**数字键一击即选中并确认，无需回车**（三次实测一致）。

### 目标形态（闭环）

```
PreToolUse gate hook ──捕获完整 tool_input 落 pending(mode=notify)──► 立即弃权退出
        │                                                    │
        ▼                                                    ▼
原生对话框正常渲染（终端键盘可答）              runtime drain → 飞书富卡片
        │                                     "终端与飞书均可回答，先答先生效"
        │                                                    │
        │                            飞书点卡 → callback → 选项映射按键序列
        │                                     → attach 第二连接注入 ◄──┘
        ▼
任一侧答完 → subscribe needs 清空 / tempo 离开 blocked
        → 未答一侧卡片翻面（"✅ 已在终端回答" / "✅ 已在飞书回答"）
```

飞书卡片**不需要第二个超时**：它是"无限等"对话框的镜像，谁答了就翻牌，
没人答就一直挂着（可选加提醒，不加硬超时）。

### 按键映射表（Step 0 全部实测于 Claude Code 2.1.201）

| 对话框形态 | 按键序列 | 状态 |
|---|---|---|
| 权限框 允许一次 | `b"1"` | ✅ 实测：blocked→active，命令执行 |
| 权限框 拒绝（模型继续） | `b"3"` | ✅ 实测：命令不执行、tempo 转 idle/active。**实现未采用**：No 项数字位随布局变（2 项/3 项框），见下「拒绝键位落定」 |
| 权限框 取消整轮 | `b"\x1b"`（ESC） | ✅ 实测：turn 取消，回 idle。**实现采用为 deny 键位** |
| 权限框 总是允许 | 见下「权限框首版简化」 | ⚠️ 位置随文案变，首版降级为 allow |
| Ask 单选 | `b"N"`（一击选中即确认） | ✅ 实测（含旧会话共 4 次） |
| Ask 单选 Other 自由输入 | `b"<other_idx>"` + 文本字节 + `b"\r"` | ✅ 实测：`4`+`mango`+Enter → "you prefer mango" |
| Ask 多选（multiSelect） | 各选中项 `b"N"`(toggle) + `b"\x1b[C"`(→Submit 页) + `b"1"`(Submit answers) | ✅ 实测：勾 python/go → 提交 |
| Ask 多问题（questions>1） | 逐题 `b"N"`（答完自动前进下一题/Submit 页）+ 末尾 `b"1"`(Submit answers) | ✅ 实测：apple+blue 两题提交 |

**Step 0 关键行为（决定映射函数写法，非直觉，必须遵守）**：

- **数字键语义随对话框类型变**：单选/多问题的普通选项——数字键**一击选中即
  确认**（多问题下还自动前进到下一题）；多选选项——数字键是 **toggle**（勾/取消），
  不推进。
- **Enter 语义随光标上下文变，禁止盲发 Enter**：单选选项上=确认；多选选项上=
  toggle 当前项（实测把已勾项又取消了）；Other 项**空文本**上=取消整个对话框
  （回 idle、不提交）；Other 项**有文本**上=确认。因此 Enter 只在两处发：
  Other 自由输入文本之后、以及多步序列不需要它（Submit 页用数字键 `1`）。
- **Other 必须先定位再打字**：数字键先把光标移到 "Type something." 项（此时**不**
  确认），随后**直接输入文本**（内联编辑，选项文字变为输入内容），最后 Enter 确认。
  顺序不可换——先 Enter 会以空文本取消。
- **多选/多问题都以 Submit 页收尾**：Submit 页恒为 `1. Submit answers / 2. Cancel`，
  注入 `b"1"` 提交。多选需先 `b"\x1b[C"` 从选项页横向切到 Submit 页；多问题答完
  最后一题会自动落到 Submit 页。
- **序列完全由捕获的 `tool_input` 构造，不依赖 `needs` 解析**：gate 捕获完整
  `questions` 数组（含 options 顺序、multiSelect 标志），飞书答案（选项标签或
  自由文本）→ 映射为选项序号或 Other 分支 → 拼字节序列。`needs` 仅用于注入
  前/后的 blocked 校验。

**权限框「总是允许」首版简化**：权限框选项文案含动态范围
（`2. Yes, and always allow access to ws/ from this project`），项数与文案随
工具/上下文变化（可能没有 always 项、或范围不同）。首版**不按渲染定位 always
项**——飞书「总是允许」注入 `b"1"`（allow once），持久化仍由 runtime 层的会话级
`always_allow` 记忆承担（沿用 v2 语义：runtime 记住 (session, tool)，后续同工具
pending 直接放行——v3 下的"放行"= 自动注入 `b"1"`，不再写决策文件）。这样既拿到
"这次放行"，又拿到"本会话后续不再问"，且不依赖脆弱的文案定位。

**拒绝键位落定（实现决策）**：deny 注入 **ESC**，不用 `b"3"`。原因：No 项的
数字位随对话框布局变（2 项框里是 `2`、3 项框里是 `3`，且项数无法从捕获的
tool_input 推导），而 ESC 在权限框上绑定的就是 No 项（选项行自带 `(esc)` 注记），
位置无关、任何布局都成立。语义差异：终端回 idle 等新输入，deny reason 无法像
v2 那样经 hook 送回模型——飞书侧拒绝理由目前只记录在交互台账，如需给模型
补指示可直接在话题里发消息（daemon reply 直写）。

Step 0 未覆盖 / 未来版本改布局的形态**不注入**：卡片降级为"请在终端回答"提示卡
（仍优于 v2——终端此时是可答的，不是被 gate 挡死的 spinner）。
不做 CLI 版本硬白名单（patch 版本频繁），安全性主要靠注入前/后校验
（见下）+ `block` 风格逃生口；映射表在代码内带"实测于 2.1.201"注记。

### 路由决策：哪些会话走哪条路

| 场景 | 路径 | 原因 |
|---|---|---|
| daemon bg 会话 + 常规 permission_mode | **v3 真双端**（capture → abstain → inject） | 对话框会渲染，attach 可注入 |
| `permission_mode=dontAsk` | 保留 v2 阻塞 gate | dontAsk 原生兜底是自动拒绝：弃权 = 工具直接被 deny，**没有对话框可注入**；飞书卡仍是唯一放行通道 |
| 非 daemon 的普通 TUI 会话 | 保留 v2 阻塞 gate | 不是 daemon job、无 attach 面；v2 行为保住飞书可答 |
| walkcode 自身 headless worker | 不拦（不变） | SDK can_use_tool 已进程内闭环 |

hook 侧判定"是否 daemon job"：session_id 规范化为 8-hex short id
（`claude_daemon_short_id`），对该 profile 的 daemon socket 发一次 `has`
（约 200ms 预算，失败/超时一律按非 daemon 处理 → 走阻塞路径）。探测放在
`gate_tui_hook`（runtime 层，可 import claude_daemon）；`claude_gate.py`
保持纯 stdlib，不产生循环依赖。

### observer attach：daemon 生 / detach 会话的状态盲区（2026-07-07 补）

ADR 0048 live-E2E 发现：**daemon 只在 job 有 ≥1 attacher 连接期间发布
state patch（tempo/needs）**。wrapper 起的会话终端天然 attach 着所以此前
一切正常；飞书生的 bg worker（以及终端 `/exit` detach 后的会话）零
attacher，对话框在 PTY 里照常渲染（attach 回放可见、注入可用），但
`list`/`subscribe` 账本冻结、`needs` 恒空——notify gate 的对话框探针据此
误判"从未渲染"，飞书卡片永不发出，会话静默卡死。

解法是把 ADR 里"第一 attacher 从终端变成注入连接"落实为常驻连接：runtime
对每个被观察的 daemon job 保持一条只读 **observer attach**
（`attachId=walkcode-observer`，200x50 与 pty-host 生成默认一致，PTY 流
排空丢弃、永不写字节）。挂接点两处：daemon spawner 注册成功后、首轮注入
**之前**（保证首个对话框的 patch 就有 attacher 可推）；watcher 发现循环
对所有受观察 job 兜底 ensure（覆盖 wrapper 会话 detach 后的窗口）。断线
按 5s 重连，job 消失自行退出。

同批实测修正键位矩阵：select 类对话框数字键落在已高亮选项上只重选不确认，
通用注入序列改为**数字 + Enter**（数字已确认时 Enter 是 no-op）；deny 的
ESC 保持单键（详见协议文档 §1.6.6）。

### 注入前/后校验与竞态防护

- **注入前**：取 subscribe 最新 state 快照，要求 `tempo=blocked` 且 `needs`
  与本 interaction 匹配（ask：问题文本前缀比对；permission：
  `approve <Tool>` 工具名比对）。不匹配 → 不注入，卡片翻
  "已在终端处理 / 对话框已变化"。这同时防住"第一个对话框被终端答掉、
  第二个已弹出"时的错注。
- **注入后**：3s 内 `needs` 未清 → 判定注入未生效：卡片提示
  "注入未生效，请在终端操作"，**不盲目重试**（重试可能双击）。trace 记录。
- **双端同刻竞态**：终端刚按下、注入字节紧随而至 → 多余字符落进下一个输入框
  （composer 里多一个数字，不会自动提交）。概率极低、危害小，接受并记录。

### gate spool 协议演进（v2 → v3 兼容）

pending 增加 `mode` 字段：

- `block`（v2 语义，缺省值向后兼容）：hook 阻塞轮询 decisions。
- `notify`（v3）：hook 落盘后已弃权退出，decisions 目录对该 rid **无消费者**；
  runtime 回调不写决策文件，改调 transport 注入。

配套调整：

- `WALKCODE_CLAUDE_GATE_TIMEOUT` 只对 block 模式有意义；personal 上的 30s
  试验值随本版清理、恢复缺省。
- interaction 的关闭改由 **watcher needs 清空**驱动（v2 由 decision 写入驱动）。
- pending 清理责任转移：notify 模式下 hook 立即退出、不再负责清理，改由
  runtime 在发卡后删除 pending（幂等由 in-memory 已发卡 rid 集保证）；
  interaction 存活于注册表直至 resolve。runtime 重启丢失注册表时，终端对话框
  仍在，subscribe 快照的 `needs` 会按既有路径重新标 WAITING_PERMISSION 并弹
  提醒卡——即"重启后降级为 v1 式提醒"，不丢安全性。

### transport 与代码落点

- `ClaudeDaemonClient.attach_send_keys(short, keys, *, cols, rows, attach_id)`：
  一次性连接——attach 握手 → 短暂 settle（POC 用 0.8s，Step 0 调参）→ 写入
  字节 → flush 读净 → 关闭。
- 键位映射函数（`keys_for_ask_answer(tool_input, answers)` /
  `keys_for_permission(action, variant)`）与 tool_input 解析同放
  `claude_daemon.py`，独立单测。
- `ClaudeDaemonTransport.approve_permission / answer_user_question`：按
  pending 的 `mode` 分流——notify 走注入、block 维持写决策文件。**签名不变，
  `_handle_callback_event` 及以上全部零改动。**
- 卡片文案：v3 卡标注"终端与飞书均可回答，先答先生效"；翻面新增
  "已在终端回答"形态（复用 `decision_result` 渲染）。

### 配置

- 新增 `WALKCODE_CLAUDE_GATE_STYLE=dual|block`（默认 `dual`）。dual 下对
  dontAsk / 非 daemon 会话自动降级 block（路由表）；设 `block` 整体恢复 v2
  行为（逃生口）。
- 现有 `WALKCODE_CLAUDE_GATE_MODE=auto|off|ask_only`、
  `WALKCODE_CLAUDE_GATE_TOOLS` 语义不变。

### 风险表

| 风险 | 影响 | 缓解 |
|---|---|---|
| 键位映射脆弱（CLI 升级改键位/对话框布局） | 注入静默失效 | 注入后 needs 校验 + 失败提示走终端（终端始终可答，无安全损失）；`block` 逃生口；映射表带版本注记 |
| attach 是逆向协议 | 升级可能破坏 | 与 v1/v2 相同 experimental 姿态：probe 门禁 + hooks/block 回落 |
| 双端同刻竞态 | composer 落入多余字符 | 注入前校验收窄窗口；接受残余风险 |
| 多选/多问题/Other 序列复杂 | 部分形态不可注入 | Step 0 实测定支持面；不支持者卡片降级"请去终端"（终端可答） |
| dontAsk 误走 abstain | 工具被自动拒绝 | 路由表显式保留 block；单测覆盖该分支 |
| runtime 重启丢 interaction | 卡片无法注入 | 降级为 needs 提醒卡（v1 路径）；终端不受影响 |

对比 v2 的一个本质改善：v2 里 gate 失效的后果是"终端被挡死等超时"；v3 里
注入失效的后果只是"回到纯终端作答"——**失败模式从两端皆盲降级为单端可用**。

### 实施步骤

| # | 步骤 | 说明 |
|---|------|------|
| 0 | 键位对齐 POC | 一次性 profile（`defaultMode=default` 强制权限框）实测：权限框各变体（Bash/Edit/mcp）、多选、多问题、Other 自由输入；产出映射表定支持面 |
| 1 | `claude_daemon.py`：`attach_send_keys` + 键位映射 | 假 daemon 桩扩展 attach op（录制注入字节），单测映射函数与注入流程 |
| 2 | `claude_gate.py`：pending `mode` 字段 + notify 捕获路径 | `gate_tui_hook` 增加 daemon-job `has` 探测；dontAsk / 非 daemon 走 block；30s 试验值清理 |
| 3 | runtime：notify drain + 回调注入 + needs 清空翻卡 | 复用 `_event_to_view` / `_handle_callback_event`；pending 清理责任转移；注入前/后校验 |
| 4 | 配置 + 文档 + 单测全绿 | `GATE_STYLE`、README / 本文档 / ADR 0046 / 协议参考同步 |
| 5 | Live E2E | 终端答一次、飞书答一次、注入失败路径、双端竞态观察；Playwright 点卡验收（对齐 v2 E2E 规格） |

每步保持可独立回退；Step 0 结论若推翻映射假设（如权限框不吃数字键），
方案回到本节评审重议，不带伤上线。

### 实现记录（2026-07-06，Step 0–4）

代码落点（与设计一致处不赘述，只记决策与偏差）：

- `claude_daemon.py`：`ClaudeDaemonClient.attach_send_keys(short, frames)`
  单连接分帧注入（帧间默认 0.15s，attach 后 settle 0.8s，握手后的 PTY 回放流
  后台排干丢弃）；`keys_for_permission` / `keys_for_ask_answer` 键位映射
  （deny=ESC，见「拒绝键位落定」；不可映射返回 None）。**生产代码真机复验**：
  多选序列 `1`→`3`→`→`→`1` 经 `attach_send_keys` 分帧注入，Review 页显示
  "→ Python, Rust"、提交成功、模型按答案继续（与 POC 单次写入等价成立）。
- transport notify 注册表：`register_notify_gate` 由 runtime drain 在发卡后
  调用（同时删除 pending，清理责任转移完成）；注入前 `list_jobs` 校验
  tempo=blocked + needs 匹配（permission 按工具名、ask 按问题文本前 40 字符
  前缀，容忍 daemon 截断）；注入后 3s 窗口轮询 needs 清空/变化。任何一步失败
  抛 `GateInjectionFailed(reason)`（定义在 `claude_gate.py`，避免
  `__init__` ↔ `claude_daemon` 循环依赖），卡片按 reason 如实翻面：
  `dialog_mismatch`/`already_resolved` → "已在终端处理"；`not_injectable` →
  "该形态请在终端选择"；其余 → "注入未生效，请在终端操作"。不盲重试。
- 终端先答的收敛：watcher needs 清空时 `resolve_notify_gates_for_short`
  弹掉该 job 的开放 gate 并留墓碑（bounded 256），迟到的卡片点击翻
  "已在终端处理" 而非假装生效；飞书注入成功走 `recently_injected` 窗口
  （10s）抑制 "✅ 已在终端处理" 的误播报。
- 重复卡抑制（v3 特有）：notify 下对话框真的渲染，daemon needs 会出现——
  watcher 检测到该 short 有开放 notify gate（注册表或 pending 里）时不再发
  v1 式橙色提醒卡，避免与富卡片双报。
- 卡片：v3 卡带注记「终端与飞书均可回答，先答先生效」（`dual_surface`）；
  notify 交互 TTL 放大到 24h（镜像无超时对话框）；多问题含多选的形态发卡前
  降级为"请在终端回答"提示（`ask_form_injectable`，与映射函数支持面同步）。
- 会话级 always_allow 的 v3 形态：drain 命中记忆时自动注入 `b"1"`
  （对话框未渲染完成时按 grace 重试 ~10s，仍失败则回落正常发卡）。
- 路由探测：hook 进程内 `has` 探测 job 存活（预算 0.5s，走已注册 transport
  的 client），失败/超时一律回落 v2 阻塞路径。

单测：621 全绿（对比 v3 开工前基线 570，净增 51 例：键位映射、attach 注入、
hook 双端路由、transport 注入与墓碑、drain notify 分流与对话框预检、
watcher 抑制/收敛、卡片渲染回归）。

### v3 验证记录（Live E2E，work 全自动 Playwright 点卡，2026-07-06）

规格对齐 v2 E2E：停 launchd 用 repo 新代码 serve work-claude、work profile
settings 临时切 `defaultMode=default` + hook 指 repo（测后全部还原、实例复检
健康），`claude --bg` 起真会话，Playwright 驱动飞书 web 点卡。全场景通过：

1. **Ask 单选，飞书答**：终端对话框与飞书卡（带「终端与飞书均可回答，先答
   先生效」注记）同时在场 → 卡上选 blue 提交 → 注入 `2` → 对话框解除、卡翻
   「✅ Color: blue」、工具行翻 ✅、模型按答案继续（"你选了 blue"）。
2. **权限 Allow**：`touch`（写命令）弹框 → 飞书 Allow → 注入 `1` → 命令执行、
   卡翻「✅ 已允许」。
3. **终端先答 + 迟点击**：`rm` 弹框由终端按 `1` 答掉 → 飞书卡迟点击 →
   pre-check dialog_mismatch → 卡诚实翻「✅ 已在终端处理（或对话框已变化），
   本卡片未生效」，未假装生效。
4. **Deny**：注入 ESC → 命令未执行、turn 取消回 idle、会话存活可继续（随后
   飞书直写下一条指令成功，带表情回执、无 TUI input 回显）。
5. **Always allow 链**：点卡注入 `1` + 会话记忆 → 下一次同工具（`rm`）
   **零卡片自动注入放行**（serve 日志：`auto_allow_session ... mode=notify`
   + `inject_ok`）。
6. 抑制项全部生效：v3 卡在场时无旧橙色提醒卡、无 "Claude needs your
   permission" 英文透传。

E2E 抓出并当场修复两个缺陷（各补回归单测）：

- **Lark 卡片信封 KeyError**：`dual_surface` 注记误写 `card["card"]`（实际键
  是 `content`），首张 ask 卡投递 5 次全败进死信——outbox 死信里的
  `last_error: "'card'"` 定位。修复 + 渲染回归用例。
- **auto-approve 悬卡**：CLI 对安全只读命令（实测 `date`）自动放行、原生对话
  框根本不渲染，而 gate 卡照发 → 永久悬挂的活按钮卡。修复为**发卡前 needs
  预检**：对话框真的在等才发卡（`notify_dialog_waiting`，宽限 30s 未见对话框
  则静默丢弃 pending）——卡片严格是对话框的镜像。

已知遗留（不阻塞，记录待后续）：

- runtime 重启后 idle 的 daemon 会话可能被误标 STOPPED，直写降级为接管提示
  （重启语义既有问题，非 v3 引入）。
- 终端先答瞬间若有工具事件把 lifecycle 拨出 WAITING，watcher 的「✅ 已在终端
  处理」文本可能漏发；迟点击墓碑兜底，无正确性损失。
- 新会话首张状态卡在 daemon_live 到达前短暂显示「只读观察 + Take over」。
