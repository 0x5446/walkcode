# ADR 0047: claude-tap 调试代理——被动 env 透传，不做 sidecar 托管

Date: 2026-07-06

Status: Accepted; implemented 2026-07-06.

## Context

用户想用 [claude-tap](https://github.com/liaohch3/claude-tap)（本地反向代理 +
trace viewer）查看某个 profile 实际发给 Claude Code 上游的 system prompt、工具
调用、token 用量。顾虑：WalkCode 本身通过 `claude-agent-sdk`
(`ClaudeSDKClient`/`ClaudeAgentOptions`) 拉起 Claude Code headless 会话
(`ClaudeHeadlessTransport`, `channel_native/__init__.py`)，真正的 `claude` 二进
制 spawn 发生在 SDK 内部；claude-tap 默认也是"自己拉起 client 再代理"
(`claude-tap`/`run_client()`)。两者若都想当 Claude 进程的发起者/env 提供者，会产
生冲突。

调研确认 claude-tap 本身就支持纯代理、不拉客户端的模式：
`claude-tap --tap-no-launch --tap-port <port>`，upstream 目标在代理**启动那一刻**
从进程环境（`ANTHROPIC_VERTEX_BASE_URL` / `CLAUDE_CODE_USE_VERTEX` /
`ANTHROPIC_BASE_URL` 等）自动探测；其官方文档的用法就是
`ANTHROPIC_BASE_URL=http://127.0.0.1:8080 claude`。这条思路本身与 WalkCode 已有
的机制（`ClaudeHeadlessTransport` 往 `ClaudeAgentOptions` 里塞覆盖值,SDK 合并到
子进程 env）方向一致，不需要 claude-tap 来 spawn 任何东西——具体走 `env` 还是
`settings` 字段,见下面 Decision 里真机验证后的结论。

原本考虑让 WalkCode 自动拉起并看护 claude-tap 这个 sidecar 进程（探活、崩溃重
启、多 profile 端口分配、退出清理）。放弃的原因：

- `ChannelNativeRuntime.from_config`/`_build_transports` 是同步工厂方法，构造
  期间做异步子进程探活会很别扭。
- 仓库里没有任何"拉起并看护外部子进程"的既有代码可复用——`ClaudeDaemonTransport`
  与 `CodexAppServerTransport` 都只连接**用户/外部已起好**的进程或 socket，从不
  自己 spawn。为一个可选调试功能新增一整类子进程 supervisor 基础设施，代价明显
  大于收益，也不符合本项目"干净边界、不引入 shell wrapper"的一贯取向（见
  ARCHITECTURE.md「Reliability」一节）。

## Decision

新增 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL`：`_configured_agent_options`
(`channel_native/__init__.py`) 解析该 env（校验 `http://`/`https://` 前缀），经
`_build_transports` (`channel_native_runtime.py`) 传给 `ClaudeHeadlessTransport`
新增的 `anthropic_base_url` 构造参数。

**实现细节踩过一个坑，记录一下**：最初实现是把它和 `config_dir` 一起塞进
`option_kwargs["env"]`（走 `ClaudeAgentOptions(env=...)`）。`claude-agent-sdk`
的源码（`_internal/transport/subprocess_cli.py`）证实 `options.env` 确实会被
merge 进实际子进程的 `os.environ`——但真机验证（起本地 `claude-tap
--tap-no-launch`、跑一次真实 headless turn、查 claude-tap 的 trace sqlite）发现
代理侧收到的请求数始终是 0，两次真实调用都直接命中了未经代理的真实 Vertex 端
点。根因：这个 profile 的 `CLAUDE_CONFIG_DIR/settings.json` 本身就带一个 `env`
块（硬编码了 `ANTHROPIC_VERTEX_BASE_URL` 等），Claude Code CLI 会用它覆盖回继承
的进程 env——单纯的 env 覆盖对 `ANTHROPIC_BASE_URL`/`ANTHROPIC_VERTEX_BASE_URL`
这两个 key 不生效。这也解释了为什么 claude-tap 自己对 "claude" 这个 client 专门
标了 `inject_settings_env=True`（`cli_clients.py`）——它的作者显然也踩过同一个
坑，用 `--settings '{"env": {...}}'` 而不是纯 env 来强行覆盖。

最终实现改为：`_option_kwargs` 里，当 `self.anthropic_base_url` 设置时，改走一个
新的辅助方法 `_anthropic_base_url_settings_override()`，生成一个固定形状的
`{"env": {"ANTHROPIC_BASE_URL": ...}}`（Vertex 模式下再加
`ANTHROPIC_VERTEX_BASE_URL`），作为 `option_kwargs["settings"]`（即 CLI 的
`--settings` 参数）。**这个辅助方法不读取、不合并 `self.settings` 的任何已有内
容**——`_configured_agent_options` 里，如果同一个 profile 同时配置了
`WALKCODE_CLAUDE_SETTINGS` 和 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL`，直接在配置解
析阶段抛 `ChannelConfigError` 拒绝，而不是尝试合并。

（最初的实现确实尝试过合并：解析 `self.settings` 的 JSON/文件内容,只覆盖其中的
`env` 子字段,其余原样保留。`deep-review` 的多个维度独立指出了这个合并逻辑的三个
真实问题：① 解析/读取失败时静默退化成空对象,原有 `model`/`hooks`/`permissions`
配置会无声丢失,没有任何日志或报错；② 会把整份 settings 文件内容重新序列化塞进
`--settings` 命令行参数,如果该文件的 `env` 块里有 `ANTHROPIC_API_KEY` 这类密
钥,会被同机器上能看到进程列表的其他用户读到——这是这次改动新增的泄漏面,不是调
试代理功能本身的固有权衡；③ Vertex 判断只读 `os.environ`,没看合并后的 settings
来源,判断口径不统一。三个问题的根因都是"试图合并一份可能包含敏感内容、格式不
可控的外部文件"。改成"配置阶段直接拒绝这个组合、运行时只生成固定最小 payload"
之后,三个问题同时消失,不需要分别打补丁。）

`CLAUDE_CONFIG_DIR` 仍然走 `option_kwargs["env"]`——它只需要在进程启动前告诉 CLI
去哪个目录找凭证/设置，这条路径本身没问题，产线 5 个实例都依赖它。

真机复测：改用 `--settings` 之后，claude-tap 的 trace sqlite 里 `record_count`
从 0 变成 2（一问一答两条记录），确认覆盖生效；简化实现后重新跑了一遍同样的真机
验证，`record_count` 依然是 2，行为不变。复测过程中还发现这个机器的 Vertex
网关路径是 `/projects/.../publishers/anthropic/models/...:rawPredict`（没有
`/v1` 前缀），跟 claude-tap 内置的 Vertex 正则
(`^/v1/projects/.../rawPredict$`) 不匹配，会被路径白名单拦成 "Blocked non-API
path"；这是 claude-tap 自身对这类网关的已知限制，不是 WalkCode 这边的问题，需要
在起 sidecar 时加 `--tap-allow-path /projects` 放行——已经写进 README/.env.example。

claude-tap（或任何其他反向代理）的进程生命周期完全由用户自己管理（例如另起一个
launchd job），WalkCode 只做只读透传，不感知、不依赖 claude-tap 这个具体工具的存
在与否。

## Consequences

- 零新增运行时依赖、零新增子进程管理代码；未配置该 env 时行为完全不变。
- 用户需要自己保证代理进程用「这个 profile 本该用的上游 env」启动，否则 claude-tap
  会探测成默认的 `api.anthropic.com`——这一点在 README/.env.example 里已注明。
- `WALKCODE_CLAUDE_SETTINGS` 和 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 不能同时配
  置在同一个 profile 上（配置阶段直接报错）。需要两者兼得的话，目前得二选一,或
  者把 `WALKCODE_CLAUDE_SETTINGS` 里想要的 model/hooks/permissions 配置临时挪到
  `CLAUDE_CONFIG_DIR/settings.json` 默认路径里,调试完再挪回来。
- 覆盖同时写 `ANTHROPIC_BASE_URL` 和 `ANTHROPIC_VERTEX_BASE_URL` 两个变量（v0.10.59
  起无条件；v0.10.58 曾按进程 env 的 `CLAUDE_CODE_USE_VERTEX` 门控——真机部署发现
  launchd 跑的 serve 进程环境里根本没有这个变量，Vertex 开关只在 profile 的
  `settings.json` 里，导致 Vertex profile 的流量静默绕过代理）。两个变量都指向同
  一个代理地址，非 Vertex 模式下多出来的 `ANTHROPIC_VERTEX_BASE_URL` 会被 Claude
  Code 忽略，无副作用。
- **v0.10.60：覆盖必须携带合并后的 profile env，且经 0600 文件传递。** 真机部署
  验证（launchd 等价的"干净"进程环境）发现：`--settings` 的 `env` 映射会**整体替
  换**掉 profile `settings.json` 的 `env` 映射，不是按 key 合并——只带两个 base
  URL 的覆盖会把 profile 自己的 `ANTHROPIC_API_KEY` 等认证变量一起顶掉，每一轮都
  报 "Not logged in"（v0.10.58/59 的真机验证之所以通过，是因为验证会话的进程环境
  里恰好继承了这些认证变量做了兜底）。对照实验：同样干净环境下，不带覆盖认证正
  常；覆盖 env 换成 profile env + base URL 合并结果后，认证成功且流量过代理。所
  以 `_anthropic_base_url_settings_override()` 现在读取
  `{config_dir}/settings.json` 的 `env` 块合并进覆盖。合并结果可能含密钥，绝不上
  argv（deep-review 安全结论）：写进 `{config_dir}/walkcode-tap-override-settings.json`
  （0600，与 settings.json 同目录同属主同威胁模型），`--settings` 只传路径。
  settings.json 存在但解析失败时抛 `TransportUnavailable` 响亮失败（不静默降级、
  不带着错误认证继续跑）；文件不存在（OAuth 型 profile）则覆盖只含两个 base URL。
- 非标准 Vertex 网关（路径没有 `/v1` 前缀）需要额外的 `--tap-allow-path`，这是
  claude-tap 自身路径白名单的限制，文档里已注明排查方法（看 sidecar 日志里的
  `Blocked non-API path`）。
- Codex agent（`CodexAppServerTransport`）暂不支持同等透传；如果需要，预期是同样
  的模式（`WALKCODE_CODEX_OPENAI_BASE_URL` + claude-tap 的 `--tap-client codex`），
  且需要重新走一遍这次的"env 不一定生效，可能要走 --settings 或等价机制"的真机
  验证，不能假设和 Claude 一致。
