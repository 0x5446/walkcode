# ADR 0069: 删除 Telegram 渠道，飞书/Lark 成为唯一渠道

Date: 2026-09-30

Status: Accepted; implemented

Supersedes: ADR 0033、0034、0035、0036、0038、0040（全部，Telegram 专属）；
ADR 0037、0039、0042 里 Telegram 专属的部分（机制本身与渠道无关，保留）。
修订 ADR 0027（`telegram` 不再是合法的 `WALKCODE_CHANNEL`）和 ADR 0044
（Telegram 从"降级保留"变为删除）。

## Context

ADR 0044 把首发渠道从 Telegram 换成飞书/Lark，Telegram 降级成"架构验证通道：
代码和测试保留，不再打磨 UX"。2026-09-29 核实的现状：

- 本机 6 个实例全是 `WALKCODE_CHANNEL=lark`；两个 Telegram 实例 2026-07-04 就已
  退役（plist 归档在 `~/.walkcode-attic/20260704-telegram/`）。env 文件里没有任何
  `TELEGRAM_*`。
- 6 个状态文件里没有 `channel_kind: "telegram"` 的绑定。唯一一处 `"telegram"`
  是 work-claude 一个飞书会话绑定的 `capabilities.origin`，只是历史数据。
- 但 Telegram 代码仍占 runtime 的一大块：`TelegramBotApi` / `TelegramChannelAdapter`、
  配置解析、长轮询和 offset 确认、forum topic 创建、命令菜单安装、
  `native debug telegram` 的入站诊断、`native serve --once/--poll-timeout/--limit`、
  E2E 门禁、`scripts/telegram_grant_manage_topics.py`，外加一百多个把
  `WALKCODE_CHANNEL=telegram` 当测试夹具用的用例。每次改共享逻辑都要顺带维护一条
  没人用的路径，Lark 路径还借用着一批名叫 `_telegram_*` 的函数，读代码的人很容易
  误删。

用户 2026-09-29 决定删除 Telegram。

## Decision

1. **删除 Telegram 专属代码**：适配器与 Bot API 客户端、Markdown→HTML 渲染、
   `_telegram_config_from_env` 与全部 `TELEGRAM_*` / `WALKCODE_TELEGRAM_*` 配置、
   `WALKCODE_E2E_TELEGRAM*` 门禁、`serve_telegram_polling` / `poll_telegram_once` /
   `process_telegram_update`、offset 确认与轮询重试、forum topic 创建与
   `TELEGRAM_FORUM_TOPIC_ICON_COLORS`、命令菜单安装、"typing"/✅ 预回执、
   `diagnose_telegram_ingress` 及只为它服务的 `_summarize_submit_gate`、
   状态卡的 `pin_status_card` / `static_status_card` 能力位（只有 Telegram 置真）、
   TUI 观测的 Telegram 绑定分支、`allowed_actor_ids`（只有 Telegram 配置写它）。
   CLI 去掉 `native debug telegram` 和 `native serve --once/--poll-timeout/--limit`
   （launchd 只跑裸 `native serve`）；`scripts/channel_native_debug.py` 去掉
   `telegram` 子命令；删除 `scripts/telegram_grant_manage_topics.py`。
2. **共享代码保留并改成中性名字**：Lark 路径在用的命令与维护函数改名，行为不变：

   | 旧名 | 新名 |
   | --- | --- |
   | `_handle_telegram_bot_command` | `_handle_bot_command` |
   | `_telegram_bot_command` | `_parse_bot_command` |
   | `_resolve_telegram_command_session` | `_resolve_command_session` |
   | `_telegram_command_reply_binding` | `_command_reply_binding` |
   | `_telegram_runtime_status_view` | `_runtime_status_view` |
   | `_handle_telegram_model_command` | `_handle_model_command` |
   | `_telegram_model_status_text` | `_model_status_text` |
   | `_telegram_unknown_slash_command` | `_unknown_slash_command` |
   | `_telegram_agent_command_text` / `_telegram_agent_command_aliases` | `_agent_command_text` / `_agent_command_aliases` |
   | `_telegram_commands_help_text` / `_telegram_native_command_menu` / `_WALKCODE_TELEGRAM_COMMANDS` | `_commands_help_text` / `_command_menu` / `_WALKCODE_COMMANDS` |
   | `_telegram_message_is_empty` | `_inbound_is_empty` |
   | `_telegram_session_topic_name` | `_session_topic_name` |
   | `_tui_telegram_chat_id`（被 `_tui_lark_chat_id` 调用） | 并入 `_tui_lark_chat_id` |
   | `_start/_stop_telegram_maintenance_tasks`（Lark 主维护循环） | `_start/_stop_maintenance_tasks` |

   命令处理结果的 reason `telegram_bot_command` 改为 `bot_command`（只出现在
   `SubmitResult` 和测试里，不落盘）。`render_view_text` 是 Lark 卡片的纯文本回落，
   保留。
3. **核心保持渠道中立**：`ChannelAdapter` 仍是接缝，`channel_kind` 仍在绑定键里，
   将来加渠道是加适配器而不是改核心。
4. **升级安全**：
   - `WALKCODE_CHANNEL=telegram` 启动即报 `ChannelConfigError`，消息点名本 ADR，
     不静默当成未知渠道；
   - 状态文件里 `channel_kind: "telegram"` 的会话、绑定、outbox 照常加载，不会让
     启动失败。运行时本来就只建配置的那一个渠道，所以这些条目和以前一样没有渠道
     可投递；TUI 绑定刷新只扫 `lark` 会话。

## Consequences

- 源码删掉约 1700 行（`channel_native/__init__.py`、`channel_native_runtime.py`、
  `__main__.py`），外加一个脚本；测试从 Telegram 夹具迁到 Lark 夹具或
  `FakeChannelAdapter`，只测 Telegram 行为的用例删除。
- `serve --once` 随轮询一起消失；Lark 本来就不支持（WebSocket 推送没有拉取语义），
  预检用 `native doctor` + `native debug lark`。
- 部署文档：`docs/channel-native-local-deploy.md` 改写为渠道无关的运行时参考
  （命令、`/reload`、TUI hook、调试门禁），部署步骤只在 `docs/lark-profile-deploy.md`。
- 如果将来要重做 Telegram，从本次删除前的提交（`chore/round1` 分支）找回代码，
  按那时的核心接口重做适配器。

## Verification

- 全量单测 1144 → 1112 个，全部通过（skipped=9，expected failures=1，见下）。
  删掉的 32 个只测 Telegram 行为：轮询/offset/重试、forum topic 与图标、topic URL、
  命令菜单安装、typing 与 ✅ 预回执、服务消息、入站诊断（7 个）、webhook 配置、
  Telegram 适配器解析/拆分/HTML/429（13 个）、附件解析、HTTP 线程分支、
  rich_messages、`debug telegram` 与 `serve --once` CLI。
- 新增：`WALKCODE_CHANNEL=telegram` 报错点名 ADR 0069；`native debug telegram` 与
  `serve --once/--poll-timeout/--limit` 被 argparse 拒绝；telegram E2E 门禁是
  unknown gate；带 telegram 绑定/授权/outbox 的旧状态能加载，维护循环不会替它们
  往 Lark 发任何东西。
- 共享命令路径（`/status`、`/sessions`、`/model`、`/reload`、未知斜杠、agent
  选择器、空消息）、维护循环（改走 `serve_lark_ws` 空闲入口）、TUI 观测与接管的
  断言迁到 Lark 后保留。
- 迁移暴露了一个 Lark 上本来就有的问题：观测会话每来一个 hook，
  `_ensure_tui_observed_binding_capabilities` 都把 `lifecycle_state` 重置成
  `EXTERNAL_OBSERVED_READONLY`，`permission-request` 之后的 Notification 因此
  不再被抑制，会多发一条"需要权限"提示。Telegram 私聊没有 thread id，从来走不到
  这段，所以旧用例一直是绿的。本次不改行为，用
  `test_notification_after_permission_request_is_suppressed`（`expectedFailure`）
  记下，修复时去掉装饰器。
- 只读核对本机状态文件：`grep -c '"telegram"' ~/.walkcode/*-state.json` 只有
  work-claude 命中 1 次（飞书绑定的 `capabilities.origin`），用当前代码加载 6 个
  状态文件的副本均成功。
