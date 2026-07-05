# Claude Daemon 多端同步读写方案（1 daemon / 多 UI 订阅读写）

Date: 2026-07-04（v2: 2026-07-05）
Status: v1 已实现（reply 写路径 + subscribe 状态同步）；v2 已实现（PreToolUse gate 权限/AskUserQuestion 飞书闭环 + 状态卡/回显整改，见文末「交互闭环 v2」）
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
   写入成功后不做 writer 所有权变更：TUI 继续持有会话，hooks 会把注入的输入
   以 `tui_user_input` 卡片回显（作为注入成功的确认）。
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
匹配），list 兜底建会话作为后续步骤。

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
- `dispatch` 新建 daemon 会话（飞书新建会话仍走 headless SDK）。
- list 兜底自动建会话（无 hook 场景）。
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
4. 拿到决策 → 输出 `hookSpecificOutput`；超时（默认 1800s，对齐 headless
   bridge）→ deny（fail-safe 与 headless 一致：绝不 fail-open）。

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
  `ask_only`。
- 非 gate 集合内的工具若仍触发原生提示，行为同 v1：橙卡提示"终端确认"+
  等待态，终端处理后回传（needs 清空）。
- hook 超时 deny 后卡片仍在：此时再点选会写入决策文件但无人消费（drain 会
  清理孤儿决策）；卡片翻面显示的结果与实际 deny 存在错位，属 30 分钟级
  边缘场景，不做额外机制。
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
