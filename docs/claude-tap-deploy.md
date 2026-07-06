# claude-tap 调试代理部署

用 [claude-tap](https://github.com/liaohch3/claude-tap)（本地反向代理 + trace
viewer）观测各 profile 的 headless Claude 会话实际发给上游的 system prompt、
工具调用、token 用量。所有 trace 汇总在一个本地 dashboard
（http://127.0.0.1:19527），按 profile 分会话展示。

机制与取舍（为什么走 `--settings`、为什么合并 profile env、为什么 WalkCode 不
托管 tap 进程）见 [ADR 0047](adr/0047-claude-tap-debug-proxy-passthrough.md)。

## 架构

每个 claude profile 一个独立的 tap sidecar（launchd 常驻），各自指向该 profile
真实的上游；WalkCode 侧只在 profile env 里加一行
`WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 指向本地端口：

```text
飞书消息 → walkcode serve → claude-agent-sdk 子进程
                              └─ --settings 覆盖（profile env + 本地 tap 地址，0600 文件）
                                   → 127.0.0.1:<port>（claude-tap，只读转发+记录）
                                       → 该 profile 真实上游（Anthropic / Vertex 网关 / Google Vertex）
```

不同 profile 的上游可能完全不同（订阅 OAuth、公司 Vertex 网关、真 Google
Vertex），所以**一个 profile 一个 tap 实例、一个端口**，不能共享。

## 安装

```bash
uv tool install claude-tap
./scripts/claude-tap-setup.sh init      # 生成 ~/.walkcode/claude-tap/taps.conf 模板
vi ~/.walkcode/claude-tap/taps.conf     # 按注释给每个 profile 填端口/上游
./scripts/claude-tap-setup.sh apply     # 生成 launchd plist、起 tap、写 env、重启实例
```

`taps.conf` 是唯一的配置源，一行一个 profile：

```text
# profile   端口    上游target                              额外放行路径
work        18901   auto                                    -
work2       18902   auto                                    /projects
personal    18903   https://aiplatform.googleapis.com/v1    /projects
```

三种上游形态的填法：

| profile 上游形态 | target | 放行路径 | 说明 |
|---|---|---|---|
| OAuth 订阅 / 官方 API | `auto` | `-` | tap 默认探测到 api.anthropic.com |
| Vertex 代理网关（settings.json 配了 `ANTHROPIC_VERTEX_BASE_URL`） | `auto` | `/projects` | apply 会把网关地址快照进 plist 供 tap 探测；网关路径通常没有 `/v1` 前缀，claude-tap 内置白名单会拦（日志见 `Blocked non-API path`），必须放行 |
| 真 Google Vertex（SA 认证，无显式 base url） | `https://aiplatform.googleapis.com/v1` | `/projects` | tap 探测不到这种形态，显式指定；target 带 `/v1` 是因为客户端发来的路径不带 |

## 日常使用

```bash
open http://127.0.0.1:19527              # dashboard，实时看各 profile 的请求
./scripts/claude-tap-setup.sh status     # 各 tap 端口/进程状态
./scripts/claude-tap-setup.sh remove     # 一键全部关掉、恢复直连
./scripts/claude-tap-setup.sh apply      # 改过 taps.conf 或 profile 上游后重新生效
```

## 可用性与保活

配置了 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 后，该 profile 由 walkcode 拉起的
**每一轮新会话都硬依赖本地 tap**（挂了就连接失败）。setup.sh 生成的 plist 已按
此前提处理：

- `RunAtLoad=true`：登录/开机自启；
- `KeepAlive=true`：进程挂掉 launchd 秒级拉起（实测 kill -9 后 3 秒恢复）；
- plist 带 `--tap-no-update-check --tap-no-auto-update`：tap 不会自动更新中途退出。

影响面：只有 walkcode 拉起的 headless 会话走代理；终端里自己开的 Claude Code
TUI 不受影响（覆盖是 spawn 时注入的，不改 profile 配置本身）。dashboard 挂掉不
影响转发，它只是查看器。

剩余风险窗口：launchd 重启间隔的几秒内来的消息会失败一轮，重发即可；
`uv tool upgrade claude-tap` 之后要跑一次 `apply` 重启 tap；端口被占会 crash
loop，`status` 里 `listening=no` 一眼可见。长期不用时建议 `remove` 解除依赖。

## 排障

| 现象 | 原因 / 处理 |
|---|---|
| dashboard 里没有新 trace，tap 日志见 `Blocked non-API path` | 上游路径不在 claude-tap 白名单（常见于无 `/v1` 前缀的 Vertex 网关），给该 profile 加放行路径列（如 `/projects`）后 `apply` |
| 会话报 `Not logged in` | 覆盖丢了 profile 认证。v0.10.60 起不应出现（覆盖会合并 profile settings.json 的 env）；若出现，确认 walkcode ≥ 0.10.60、`{CLAUDE_CONFIG_DIR}/settings.json` 可正常解析 |
| 会话报 `TransportUnavailable: unreadable or invalid JSON` | 该 profile 的 settings.json 坏了，这是刻意的响亮失败（不静默带错误配置继续跑），修好 settings.json 即可 |
| `status` 显示 `listening=no` | 端口被占或 tap crash loop，看 `~/.walkcode/logs/tap-<profile>.err.log` |
| 改了 profile 上游但 tap 还打到旧地址 | plist 里的探测 env 是 apply 时的快照，重跑 `apply` |

## 约束

- `WALKCODE_CLAUDE_SETTINGS` 与 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 不能同时配
  置在同一个 profile（启动时报错，原因见 ADR 0047）。
- Codex profile（`WALKCODE_AGENT=codex`）暂不支持同等透传。
- 覆盖生效时会在 `{CLAUDE_CONFIG_DIR}/walkcode-tap-override-settings.json` 生成
  0600 的合并配置文件（密钥不上命令行），由每次 spawn 自动重写，无需手动管理。
