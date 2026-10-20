# ADR 0070: 加入 Slack 渠道，渠道改为可插拔

Date: 2026-10-20

Status: Accepted; 第 0 期已发布（v0.14.44），第 1 期实现中

修订 ADR 0069 第 3 条（"飞书是唯一渠道"改为"飞书和 Slack 两个渠道，一个实例仍只跑一个"）。
ADR 0044 的会话放置规则（一个话题一个会话，根消息是状态卡）原样沿用到 Slack。

## Context

用户要求 Slack 支持，体验不低于飞书，以模块/插件形式接入。

核心本来就是渠道中立的：`ChannelAdapter` 是接缝，绑定键带 `channel_kind`，outbox、
交互令牌、会话路由都不认识飞书。但飞书写死在约十几处，集中在
`channel_native_runtime.py`：

- 配置只认 `lark`（`config.from_env`）；`_build_channels` 只会建飞书适配器；
- 服务入口是 `serve_lark_ws`，收件箱叫 `<state>.lark-inbox.d`，入站处理叫
  `process_lark_event`，里面 `isinstance(channel, LarkChannelAdapter)`；
- 合并转发展开、话题历史回读、建话题（`_place_lark_new_session`）、
  白名单（`_lark_chat_allowed` 对非 lark 直接返回 False）、TUI 镜像绑定
  （五处 `channel_kind != "lark"`）；
- 编排器里：`_new_binding_capabilities` 只给 lark 设 `status_card/native_topic`，
  回执表情池 `_ACK_REACTIONS` 按 `"lark"` 取，gate 卡片超时文案写死"飞书上"；
- 适配器协议只声明 5 个方法，`parse_event`、`react_to_message`、`fetch_message`、
  `fetch_thread_messages`、`render_message_log` 在用却没声明。

调研（2026-10-20，来源见文末）的结论：

- 同类产品（Claude Tag、Cursor、Codex、Devin）都是 **一个 thread 一个会话**；频道里
  @bot 开始，thread 里直接回复继续，DM 不用 @。命令用 `!cmd` 文本形式。
- Slack 官方明确：**自定义斜杠命令不能在 thread 里用**。
- Slack 2026-08 推出 agent 会话原语：`agents.sessions.setStatus`
  （active/processing/suspended/closed）、`agents.sessions.rename`、订阅
  `agent_session_stopped` 后原生 Stop 按钮。
- `markdown` block 支持标准 Markdown（含管道表格），一条消息合计 12k 字符；
  `chat.update` 文本超 4000 报 `msg_too_long`。
- Socket Mode：回 `envelope_id` 即确认，3 秒内；未确认会重发；同一条 @ 消息会同时
  推 `message` 和 `app_mention` 两个事件，event_id 不同。
- 流式接口 `chat.startStream/appendStream/stopStream` 只能在 thread 里用，频道里要带
  接收人，用户可中途停止，流期间不能 `chat.update`。

## Decision

### 1. 渠道接缝（第 0 期，飞书行为不变）

不建通用插件框架。两个渠道，各一组模块，运行时按 `WALKCODE_CHANNEL` 选一个：

| 渠道 | 配置 | 适配器 | 实时 API / 入站桥 | 渲染 |
| --- | --- | --- | --- | --- |
| lark | `_lark_config_from_env` | `lark_adapter.py` | `lark_live.py` | `lark_cards.py` |
| slack | `_slack_config_from_env` | `slack_adapter.py` | `slack_live.py` | `slack_blocks.py` |

具体改动：

1. `ChannelAdapter` 协议补上实际在用的方法：`parse_event`、`react_to_message`、
   `fetch_thread_messages`、`render_message_log`、`expand_inbound`（飞书：合并转发；
   Slack：分享消息附带的原文），以及属性 `ack_reactions`（回执表情池，取代
   `_ACK_REACTIONS`）、`display_name`（文案里的渠道名）。`FakeChannelAdapter` 同步实现。
2. 运行时去掉 `"lark"` 字面量，改看 `self.config.channel.kind`：
   - `process_lark_event` → `process_channel_event`；`serve_lark_ws` → `serve_channel`，
     入站桥由渠道 live 模块的 `build_ingress_bridge(...)` 提供；
   - 收件箱 `<state>.<kind>-inbox.d`（飞书算出来还是原路径，不用迁移）；
   - `_place_lark_new_session` / `_expand_lark_merge_forward` /
     `_maybe_seed_lark_thread_context` / 白名单 / TUI 镜像绑定改为中性名字和中性逻辑；
   - `last_lark_event_error` → `last_channel_event_error`。
3. 配置：`from_env` 用一张 `{kind: parser}` 表分发；白名单选项统一叫
   `allowed_chat_ids` / `allowed_sender_ids`（飞书的 `LARK_ALLOWED_OPEN_IDS` 映射到后者），
   TUI 镜像会话所在的聊天统一叫 `tui_chat_id`。
4. 编排器：`ChannelCapabilities`（`models.py`）新增字段 `topic_sessions: bool = False`，
   飞书、Slack 置真，Fake 保持假。`_new_binding_capabilities` 原来判断
   `channel_kind == "lark"`，改为判断这个字段，命中时照旧往绑定的 `capabilities` 字典里写
   `status_card` / `native_topic`。gate 超时文案用 `display_name`。
5. doctor / `native debug <kind>` / `scripts/channel_native_debug.py` 按渠道分发。

只加一个能力位。根卡"编辑失败就降级"的逻辑保留不动，Slack 同样走它：Slack 编辑没有
次数上限，但根消息被删、`cant_update_message`、`edit_window_closed` 等永久错误照样会
触发降级。

### 2. Slack 适配器（第 1 期）

**身份与绑定**：`account_id = team_id`；`chat_id` = 频道 ID；`thread_id = root_message_id`
= thread 父消息 ts；`message_id` = ts（编辑时用绑定里的频道 ID 加 ts）。

**新会话放置**：复用第 0 期改成中性的 `_place_new_session`，不写 Slack 专属分支：
频道顶层（或 DM）收到开会话的消息 → 先在顶层发状态卡 → 用卡片 ts 作 `thread_ts` /
`root_message_id` → 把用户原文以"👤 …"贴进 thread。发卡失败时退回以用户原消息为根，
和飞书一致。

**入站**（`slack_live.py`）：

- 只依赖 `slack-sdk`（写进 `dependencies`，理由同 pyproject 里 2026-07 断联事故的注释），
  用它自带的同步 `SocketModeClient`，跑在独立线程，和飞书的 WS 线程同构；
  不用 Bolt——Bolt 自己决定何时确认，和"先落盘再确认"冲突。
- 每个信封：先原子写入 `<state>.slack-inbox.d`，再回 `envelope_id`。按钮回调同样处理，
  不需要飞书的 `AckRegistry`：Slack 的确认不带提示文字，处理结果靠编辑原消息展示。
  写盘失败就不回 `envelope_id`，让 Slack 重发。
- 收件箱的消费、恢复规则与飞书共用同一份代码（第 0 期的 `serve_channel`），语义不变：
  启动时重放 `.json`；`.processing` 绝不重新提交给 agent，只发"结果未能确认"的通知后归档
  为 `.failed`；通知持久化失败就保留 `.processing`，恢复失败让消费者退出由 launchd 重启。
- 事件 ID 取 `slack:<team>:<channel>:<ts>`，不用信封的 event_id。一层去重同时挡住
  Slack 重发和 `message`/`app_mention` 双推。按钮回调用
  `slack:action:<team>:<container ts>:<action_ts>`。
- 过滤：bot 自己的消息（`bot_id`）、`message_changed/deleted` 等子类型；
  外部组织（`team_id` 不等于本工作区，或 Slack Connect 用户）默认拒绝，除非在白名单。
- 在频道顶层，只有 @bot 的消息才开会话；bot 自己的 thread 里、DM 里不用 @。
  `<@BOTID>` 提及从文本里去掉。
- `agent_session_stopped` 映射为中断当前回合；`agent_session_title_changed` 映射为
  最高优先级的会话标题来源。

**出站**（`slack_live.py` + `slack_blocks.py`）：

- `send_view`：有 `root_message_id` 则带 `thread_ts`，否则顶层发送；`edit_view` 用
  `chat.update`。
- 回答用 `markdown` block；超过 12k 按段落切成多条，切开的代码块补齐围栏。
  状态卡、工具进度卡、权限卡、AskUserQuestion、接管卡、会话选择、模型选择用 Block Kit。
  高风险按钮带 Slack 原生 `confirm` 对话框。工具进度卡渲染后控制在 4000 字以内。
- AskUserQuestion 批量表单：消息里放 `input` block（单选 `static_select`、多选
  `multi_static_select`、其他 `plain_text_input`），点"提交"时从 `state.values` 读取，
  折叠成与飞书相同的 `callback["form"]` 形状，核心不变。
- 回执：`reactions.add`，表情池 `white_check_mark / ok_hand / +1 / muscle / clap`。
- 会话原生状态：回合开始 `processing`，等审批或回答 `suspended`，空闲 `active`，
  结束 `closed`；标题变化时 `agents.sessions.rename`。全部 best-effort，失败只记日志，
  不进 outbox。
- 错误分类：429 → `TransientDeliveryError(retry_after)`；`channel_not_found`、
  `not_in_channel`、`is_archived`、`invalid_auth`、`account_inactive`、
  `token_revoked`、`msg_too_long`、`invalid_blocks` → `PermanentDeliveryError`；
  `internal_error`、`request_timeout`、网络超时属于"可能已生效"。`chat.postMessage` 没有
  幂等键，所以每条发送都带 `metadata`（`event_type=walkcode_delivery`，payload 是 outbox 的
  幂等键）。这类错误按可重试处理，重发前先用 `conversations.replies`
  （`include_all_metadata`）查目标 thread 最近的消息，已存在同一幂等键就直接记为成功并
  取它的 ts。既不丢回答，也不重复。顶层消息（状态根卡）查 `conversations.history`。
  `chat.update` 天然幂等，直接重试。
- 无权限的人点按钮：`chat.postEphemeral` 只给他看提示。
- 附件：`files[]` 用 `url_private_download` + Bearer 流式下载到
  `attachment_download_dir()`；Slack Connect 的 `file_access: check_file_info`
  先调 `files.info`。出站文件不做（飞书也没有）。

**命令**：Slack 里打 `/status` 会被客户端拦截，所以命令前缀是 `!`：`!status`、
`!sessions`、`!model`、`!reload`、`!takeover`、`!repo <dir> <task>`、`!commands`。
`parse_event` 把消息开头的 `!` 改写成 `/`（`!!` 改写成 `//`），之后整条命令路径——
WalkCode 命令、未知命令转交 agent、`//` 转义——原样复用，核心不为 Slack 加分支。
帮助文本里的命令前缀取自适配器。

**配置**：

| 变量 | 说明 |
| --- | --- |
| `WALKCODE_CHANNEL=slack` | |
| `SLACK_BOT_TOKEN` | `xoxb-`，必填 |
| `SLACK_APP_TOKEN` | `xapp-`，`connections:write`，必填 |
| `SLACK_ALLOWED_CHANNEL_IDS` | 频道白名单，逗号分隔，空 = 不限 |
| `SLACK_ALLOWED_USER_IDS` | 用户白名单，空 = 不限 |
| `WALKCODE_SLACK_TUI_CHANNEL_ID` | 终端会话镜像到哪个频道 |
| `WALKCODE_E2E_SLACK` / `WALKCODE_E2E_SLACK_CHANNEL_ID` | 真实环境测试门禁 |

一个 runtime 一个 Slack app：Claude 和 Codex 各建一个，manifest 见
`docs/slack/app-manifest.yaml`。部署步骤（建 app、安装、取 token、写 env、launchd、
白名单引导、`native doctor` / `native debug slack`）写在 `docs/slack-profile-deploy.md`，
与 `docs/lark-profile-deploy.md` 对应。

### 3. 暂不做

- **流式输出**（`chat.startStream`）：它是 outbox 之外的第二条发送路径，崩溃恢复、
  去重、补发都要重新设计，和 ADR 0061/持久投递约定冲突。第 1 期稳定后另起 ADR，
  按"能流就流、失败退回发送加编辑"的方式接入。
- **Slack Code 会话频道**（一个任务一个频道）：发布不久，套餐可用范围查不到。
- 出站文件、❌ 表情中断、App Home。

## Consequences

- 飞书路径行为不变；改名涉及测试夹具，按 ADR 0069 的做法整体迁移。
- 新依赖 `slack-sdk`（纯 Python，无传递依赖）。
- Slack 与飞书的体验差异：命令用 `!` 前缀；频道里开会话要 @。
- Slack 好于飞书的地方：长回答不截断；根卡编辑不受次数限制；原生会话状态与 Stop
  按钮；高风险按钮二次确认；按钮无权限时有私密提示；按工作区区分绑定。

## Verification

- 第 0 期：全量单测。上线逐个实例来：先升级 `work2-claude`，`native doctor` 通过并用
  真实飞书消息（新话题、回复、权限卡、AskUserQuestion、附件、重启后收件箱恢复）回归，
  再升级其余 4 个。任一步失败就停：回滚 = 重装上一个 Release（`uv tool install
  walkcode@<上一版本 tag>`）再重启该实例。收件箱路径、状态文件格式都不变，回滚不需要
  迁移数据。
- 第 1 期：适配器单测（解析、去重、渲染、错误分类、长文切分）；真实 Slack 工作区
  E2E：频道 @ 开会话、DM、thread 续聊、权限卡、AskUserQuestion 表单、接管、附件、
  `!status/!model/!reload`、Stop 按钮、进程重启后的收件箱恢复。

## 待实测

1. 官方把 `input` block 列为可用于消息，但没写点按钮时 `state.values` 是否带齐全部值；
   不行则 AskUserQuestion 批量模式改为按钮打开 modal，用 `view_submission` 回读，
   再折叠成同样的 `callback["form"]`。
2. bot 能否无限期编辑自己的消息；不行则沿用根卡降级逻辑（已存在）。

## Sources

- https://docs.slack.dev/ai/agent-sessions
- https://docs.slack.dev/reference/methods/agents.sessions.setStatus
- https://docs.slack.dev/reference/methods/chat.startStream
- https://docs.slack.dev/reference/block-kit/blocks/markdown-block
- https://docs.slack.dev/interactivity/implementing-slash-commands
- https://docs.slack.dev/apis/events-api/using-socket-mode
- https://docs.slack.dev/apis/web-api/rate-limits
- https://claude.com/docs/claude-tag/users/commands
- https://cursor.com/docs/integrations/slack
- https://github.com/vercel/chat
- https://github.com/openclaw/openclaw/tree/main/extensions/slack/src

## 实现记录（第 1 期，2026-10-20）

与上文设计的出入，以这里为准：

1. **用户白名单必填**：`SLACK_ALLOWED_USER_IDS` 为空时拒绝启动。Slack app 对整个工作区
   可见、谁都能私信，"白名单留空 = 所有人"的引导方式不安全。
2. **按钮点击也过发送者白名单**（共享代码，飞书同样生效）：名单外的人点卡片，回调被拒，
   Slack 用 `chat.postEphemeral` 只给他看提示，飞书无提示。飞书白名单为空时行为不变。
   这是一处有意的收紧：按钮能批准工具执行，和发消息同等权力。
3. **外部组织**：Slack Connect 里其他组织的人的消息和点击在入站桥直接丢弃（不落盘）。
4. **频道里的非 @ 消息**：`InboundEvent.addressed=False`，只能续接已绑定该 thread 的会话，
   否则静默丢弃并记账；飞书永远是 `True`，行为不变。
5. **Stop 按钮、Slack 内改标题暂不接**：核心没有"中断当前回合"的渠道入口，
   `agent_session_stopped` / `agent_session_title_changed` 订阅保留但入站桥不落盘。
   会话原生状态（processing/suspended/active/closed）和标题已同步。
6. **"可能已生效"的发送**：没有用 `metadata` 幂等键（不想让每条消息多带元数据），
   改为在适配器内存里记住超时/5xx 的发送，重试前到 thread（或频道最近 20 条）里找同文本的
   bot 消息，找到就直接用它的 ts。进程重启后这段记忆丢失，极端情况下可能重复一条。
7. **三个 app**（`docs/slack-profile-deploy.md`）：`cc_tf`（订阅 Claude Code）、
   `ccp_tf`（llm-proxy Claude Code）、`cx_tf`（Codex），各对应一个飞书实例并沿用其
   agent 配置目录；profile 名 `<飞书 profile>-slack`。

