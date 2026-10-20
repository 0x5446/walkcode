# Slack Profile Deploy

Slack 渠道（ADR 0070）的部署步骤。和飞书一样：1 个实例 = 1 个 Slack app =
1 个 agent = 1 份 env = 1 个状态文件 + 1 个 launchd 服务。Claude 和 Codex 各建一个
app，不能共用 token。

## 1. 建 Slack app（每个 agent 一次）

1. https://api.slack.com/apps → **Create New App → From a manifest** → 选工作区。
2. 粘贴 `docs/slack/app-manifest.yaml`，按 agent 改 `name` / `display_name`
   （本机命名见下表：`cc` = Claude Code，`ccp` = 走 llm-proxy 的 Claude Code，
   `cx` = Codex，后缀 `_tf` 是使用者）。manifest 已开启 Socket Mode、
   交互、agent 视图（`features.agent_view`，**不可逆**），订阅了
   `app_mention`、`message.{channels,groups,im,mpim}` 和两个 agent 会话事件。
3. **Basic Information → App-Level Tokens → Generate**，scope 选
   `connections:write`，得到 `xapp-…`。
4. **Install App**。工作区要求审批时点 *Request to Workspace Install*，管理员批准后
   回来取 **Bot User OAuth Token** `xoxb-…`。

| Slack app | App ID | 工作区 | walkcode 实例 | 对应飞书实例 |
|---|---|---|---|---|
| cc_tf | `A0CAH7XTJR1` | Plaud | work-slack-claude | work-claude（订阅） |
| ccp_tf | `A0CAMV3Q7T8` | Plaud | work2-slack-claude | work2-claude（llm-proxy） |
| cx_tf | `A0CAH8TLQBV` | Plaud | work-slack-codex | work-codex（llm-proxy） |

已有 app 改名/改配置可以不走网页：在 Your Apps 页生成 App Configuration Token
（12 小时有效），用 `apps.manifest.export` / `apps.manifest.update` 改 manifest。

## 2. Env 文件

`~/.walkcode/{profile}-{agent}.env`，权限 600。profile 用 `<对应飞书 profile>-slack`，
agent 配置目录沿用对应飞书实例的那一个（同一套登录与 provider）：

```bash
WALKCODE_CHANNEL=slack
WALKCODE_PROFILE=work-slack       # 状态文件 ~/.walkcode/work-slack-claude-state.json
WALKCODE_AGENT=claude             # 或 codex
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_ALLOWED_USER_IDS=U0A5JN16E4E   # 必填，逗号分隔
# SLACK_ALLOWED_CHANNEL_IDS=C...     # 可选，留空 = 不限频道
WALKCODE_CLAUDE_CONFIG_DIR=~/.claude-profiles/work   # codex 用 WALKCODE_CODEX_HOME
WALKCODE_CWD=~/.walkcode/workspace
```

- **用户白名单必填**。Slack app 对整个工作区可见，谁都能私信它；飞书那种"白名单
  留空 = 所有人"的引导方式在这里会直接拒绝启动。按钮点击同样受白名单约束，
  名单外的人点了只会收到一条仅自己可见的提示。
- Slack Connect 共享频道里其他组织的人一律忽略。
- 自己的 user id：Slack 里点头像 → Profile → ⋯ → *Copy member ID*。
- TUI 镜像默认不走 Slack（终端会话仍镜像到飞书实例）。要镜像到 Slack，给
  `WALKCODE_SLACK_TUI_CHANNEL_ID` 设一个频道，并让该 agent 配置目录的 hook 指向这份 env。

## 3. launchd

`~/Library/LaunchAgents/com.walkcode.work-slack-claude.plist`，结构与飞书实例相同
（见 `docs/lark-profile-deploy.md` 第 4 节），把 env 文件和日志名换成实例名：

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.walkcode.work-slack-claude.plist
```

## 4. 验收

```bash
export WALKCODE_ENV_FILE=$HOME/.walkcode/work-slack-claude.env
walkcode native doctor
walkcode native debug slack     # auth.test + Socket Mode 连接探测 + 白名单
```

真人验收（在频道里先 `/invite @cc_tf`）：

- 频道里 `@cc_tf 任务` → 顶层出现状态卡，任务在它的 thread 里跑；
- thread 里直接回复（不用 @）→ 同一会话继续；DM 里直接发 → 开新会话；
- `!status`、`!model`、`!reload`、`!sessions`、`!commands`；`!!compact` 把
  `/compact` 交给 agent；
- 权限卡允许/拒绝/始终允许（始终允许会弹 Slack 确认框）；AskUserQuestion 单题按钮
  与多题表单；
- 发图片/文件 → agent 收到本地路径；
- 回答超过 12k 字 → 拆成多条，代码块不断；
- 白名单外的人点按钮 → 只有他看到"没有生效"；
- `launchctl kickstart -k` 重启后，重启前未处理的消息按收件箱规则恢复。

## 5. 与飞书的差异

| | 飞书 | Slack |
|---|---|---|
| 开会话 | 群里发消息 | 频道里 @bot；DM 直接发 |
| 命令 | `/status` | `!status`（Slack 拦截 `/`，且 thread 里不能用自定义斜杠命令） |
| 长回答 | 28k 字截断 | 按 12k 拆多条，不截断 |
| 根卡编辑 | 有次数/时间窗限制 | 无次数限制 |
| 运行状态 | 状态卡 | 状态卡 + Slack 原生 agent 会话状态 |
| 白名单 | 留空 = 所有人 | 用户白名单必填 |
