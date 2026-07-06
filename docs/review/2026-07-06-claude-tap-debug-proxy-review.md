# Deep Review 综合结论：claude-tap 调试代理透传配置

**VERDICT**: SAFE（fix 后）
**轮次**：2（Phase 1 8 维度 → fix → 针对性 re-verify）
**类型**：code

> 范围：WalkCode 新增 `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL`，让某个 profile 的
> headless Claude 会话把上游 base URL 指向本地反向代理（如 claude-tap），用于调
> 试观测。
> Review engine：codex-cli 0.142.5（host: claude; engine_source: auto）
> Cursor：disabled（未检测 cursor-agent 登录态，跳过）
> 维度：8 个 review engine 并行（correctness / errors / security / concurrency
> / data / observability / design / tests）
> Phase 2 验证：手工针对性 re-verify（非全自动 Phase 2 回证），逐条核对旧问题是
> 否解决 + 独立找新问题
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA（fix 前基线）: a960a0d2e1202e7db10de81f992cdc208511cd59
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-linear-imagining-scroll-a960a0d-1783312077.iXEQ
> 规模：约 350 行 / 8 文件（fix 前）；--plan-only 未使用，本次为手工 full-auto 流程

## 🔴🔴 顶级必修（已修复）

### 1. [Warning→已修复] settings 合并会把密钥暴露到进程列表
- **Category**: Security
- **来源**: dim-security（Confidence 0.9）
- **问题**：原实现在 `_settings_with_anthropic_base_url_override()` 里读取
  `WALKCODE_CLAUDE_SETTINGS` 指向的文件内容，重新序列化后整份塞进 `--settings`
  命令行参数。如果该文件的 `env` 块里有 `ANTHROPIC_API_KEY` 等密钥（这台机器的
  真实 profile 就是这么配的），会被同机器上能看进程列表的其他用户读到。
- **修复**：不再读取/合并任何已有 settings 文件内容；`_configured_agent_options`
  在配置解析阶段直接拒绝 `WALKCODE_CLAUDE_SETTINGS` 与
  `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 同时配置（`ChannelConfigError`）。
- **回证**：针对性 re-verify RESOLVED —— 运行时 helper 不读文件，两者共存在配置
  阶段就报错，不会等到运行时才发现。

### 2. [Warning→已修复] settings 解析失败静默丢弃 profile 全部配置
- **Category**: ErrorHandling / DataIntegrity / Observability
- **来源**: dim-errors（0.92）+ dim-data（0.93）+ dim-observability（0.91）—— 三
  个独立维度命中同一处代码，高共识
- **问题**：`self.settings` 解析失败（格式错误 JSON、文件不存在、不可读）时静默
  退化成空对象，原有 `model`/`hooks`/`permissions` 配置无声消失，没有任何日志或
  报错。
- **修复**：同上——不再尝试解析/合并，问题连同它的诱因一起消失。
- **回证**：RESOLVED。

### 3. [Warning→已修复] Vertex 判断口径不统一
- **Category**: Bug / Design
- **来源**: dim-correctness（0.88）+ dim-design（0.86）
- **问题**：判断是否覆盖 `ANTHROPIC_VERTEX_BASE_URL` 时只读 `os.environ`，没看
  合并后 settings 里的 `CLAUDE_CODE_USE_VERTEX`，如果该开关只在 settings.json 里
  会漏判。
- **修复**：不再合并 settings，`os.environ` 检查保留（跟 WalkCode 现有其它
  provider 配置一样，要求这类开关体现在真实进程环境里）。
- **回证**：PARTIALLY_RESOLVED——如果某个 profile 把 `CLAUDE_CODE_USE_VERTEX`
  只放在 `CLAUDE_CONFIG_DIR/settings.json`（不在真实进程 env）里，这个覆盖仍然
  不会生效。这不是新增限制，是 WalkCode 现有架构的既有假设（`_load_native_env`
  不会把 env file 内容写回 `os.environ`），已在 ADR 0047 的 Consequences 里记录
  为已知限制，不额外处理。

## 🟡 中置信（已修复）

### 4. URL 校验用了 `netloc` 而不是 `hostname`
- **Category**: Bug
- **来源**: re-verify 复核时新发现（0.9）
- **问题**：`http://:18899` 这种 netloc 非空但没有实际主机名的地址会通过校验，
  和报错信息"must have a host"矛盾。
- **修复**：改用 `urlsplit(...).hostname` 判断，已加对应测试。

## ❌ 已驳回 / 范围外

- dim-data ISSUE_2（`_configured_agent_options` 里 `WALKCODE_CLAUDE_SETTINGS`
  无条件 `Path().expanduser()`，会破坏内联 JSON 里的 `https://`）——确认是这次改
  动之前就存在的代码，不属于本次 diff，且新实现已不再需要把 `self.settings`
  当 JSON 解析，不再受影响。留作独立的、范围外的既有问题，未在本 PR 修复。

## 🟢 SAFE 维度

- dim-concurrency：未发现问题，新增方法是纯函数，不写共享可变状态。

## 测试补充

按 dim-tests 的 3 条建议全部补齐：
- `WALKCODE_CLAUDE_SETTINGS` + `WALKCODE_CLAUDE_ANTHROPIC_BASE_URL` 组合拒绝
- URL 校验边界值（空 host、`http://:port` 无主机名、大小写 scheme）
- `_build_transports` 传参的集成测试

## 端到端验证

- 起本地 `claude-tap --tap-no-launch --tap-client claude --tap-port 18899
  --tap-allow-path /projects`（针对这台机器非标准 Vertex 网关路径），继承真实
  Vertex profile 的进程 env。
- 用独立脚本直接调 `ClaudeHeadlessTransport(anthropic_base_url=...)` 跑一次真实
  headless turn（`session_id` 随机、`cwd=/tmp`，不碰生产 state/launchd 实例）。
- fix 前：claude-tap trace sqlite `record_count=0`（两次真实调用都绕过了代理，
  直接命中真实 Vertex 端点——这正是触发本次 `--settings` vs `env` 排查和最终简化
  方案的原始证据）。
- fix 后（含本次安全简化）：`record_count=2`，确认覆盖生效且简化没有破坏功能。
- `uv run --with pytest python -m pytest tests/test_channel_native_*.py`：534
  passed。
- `uv run python -m compileall`：通过。
- `ruff check`：无新增问题（仓库里预先存在的 2 条 E731/F401 与本次改动无关，未
  触碰对应代码）。

## 原始产物

- 各维度：`$RUN_DIR/dim-{name}.md`
- 针对性 re-verify：`$RUN_DIR/reverify.md`
