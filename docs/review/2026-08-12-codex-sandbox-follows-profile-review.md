# Deep Review 综合结论

VERDICT: NEEDS_FIX

**轮次**：1 / 2
**类型**：code

> 范围：channel-launched codex 线程的 sandbox 兜底从硬编码 `read-only` 改为跟随 codex profile 自己的 `sandbox_mode`（未提交工作区 diff）。重点：权限放宽的安全影响、向后兼容、`thread/start` 省略 sandbox key 时 app-server 的真实语义。
> Review engine：codex codex-cli 0.144.5（host: claude；engine_source: auto；模型：gpt-5.6-sol effort=xhigh）
> Cursor：disabled（composer-2.5 smoke test failed）
> 维度：基础 4（correctness / goalfit / maintainability / conventions）+ 信号触发 1（security）；correctness 前两次 540s 超时，第三次加时间预算约束后成功
> Phase 2 验证：4 条已派（0 条免验）；结果 4 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode
> HeadSHA: 715e8e65425b88c670fdfd1927661e1d9e9d567e
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T//deep-review-walkcode-715e8e6-1786514837.QoD1
> 规模：174 行 / 5 文件
> 模式：默认报告模式（未 --fix，未动被审代码）

## 本方补充的真实协议实证（report 阶段追加，用于校正 F3）

review 期间用真实 `codex app-server --stdio` + 一次性 `CODEX_HOME` 实测了四种组合，
这是本次变更最核心的未知项（省略 sandbox key 到底回落到哪）：

| config.toml 的 sandbox_mode | thread/start 发送 | ThreadStartResponse 回显的生效沙箱 |
|---|---|---|
| `danger-full-access` | 省略 | `{"type":"dangerFullAccess"}` |
| `read-only` | 省略 | `{"type":"readOnly","networkAccess":false}` |
| **完全没有这一项** | 省略 | `{"type":"readOnly","networkAccess":false}` |
| `danger-full-access` | `read-only` | `{"type":"readOnly","networkAccess":false}` |

结论：省略 key 确实回落到 profile；配置缺失时 app-server 回落到 **readOnly**（fail-closed，
不是 workspace-write）；显式覆盖仍然优先。变更的核心语义成立。探针脚本见 `/tmp/sandbox_fallback_probe.py`。

## 🔴🔴 顶级必修

### 1. [Warning] tests/test_channel_native_runtime.py:5101-5122 (Symbol: test_launch_omits_sandbox_when_unset_so_the_profile_decides)
> **一句话**：权限默认值已经放宽，却没有用真实服务验证配置回落。

- **Category**: Convention / Completeness
- **Confidence**: dim-conventions 0.99, dim-maintainability 0.99, dim-goalfit 0.99
- **来源**: conventions + maintainability + goalfit（3 个维度独立命中）
- **证据**：新增测试只用自制假客户端记录请求，最终仅断言 `assertNotIn("sandbox", client.requests[0][1])`，没有启动真实 codex app-server。
- **问题**：`AGENTS.md:10` 明文写着 "After completing code changes, you MUST perform thorough E2E verification in the real environment. Unit tests and local simulation alone are NOT sufficient."。假客户端只能证明 walkcode 省略了字段，不能证明 codex 会读 profile 的 `config.toml`。
- **修复**：把上面那份真实协议探针固化为一条 live-gated 测试（临时 `CODEX_HOME` 写不同 `sandbox_mode`，断言 `ThreadStartResponse.sandbox` 与配置对应），挂在仓库既有的显式 live 开关下。
- **回证**：VERIFIED @ 5101-5122，AGENTS.md:10 原文成立

## 🔴 高置信

### 2. [Warning，原报 Critical → 调整 Warning] src/walkcode/channel_native_runtime.py:5378 (Symbol: _build_transports)
> **一句话**：未配置消息白名单时，任何发信人都可驱动主机执行命令。

- **Category**: Security
- **Confidence**: dim-security 0.98
- **来源**: security（单维度）
- **证据**：白名单判定函数在对应白名单为空时 `return True`；传输层 `approval_policy` 固定 `"never"`；本次 diff 移除了 walkcode 侧的 read-only 兜底。
- **问题**：三者叠加时，"谁都能发消息 + 无沙箱 + 从不询问"构成远程任意命令执行入口。
- **修复**：生效沙箱为 `danger-full-access` 时，要求至少配置一个白名单，否则拒绝启动；完全放行需显式危险开关。
- **回证**：VERIFIED，但降级为 Warning——空白名单放行与 `never` 都是 diff 之前就有的行为，read-only profile 不受影响，且本机两个实例都已配白名单。属配置组合风险，不是本次新增的 Critical 暴露。

### 3. [Suggestion，原报 Warning → 本方实证后降级] src/walkcode/channel_native/__init__.py:8194-8213 (Symbol: launch)
> **一句话**：生效的沙箱没有被记录，权限变化在日志和状态卡上看不见。

- **Category**: Security / Observability
- **Confidence**: dim-security 0.96（回证 VERIFIED，AdjustedSeverity "-"）
- **证据**：`launch` 只从响应里取 thread id，`ThreadStartResponse.sandbox` 回显的生效策略被丢弃。
- **问题**：原报的核心论点是"配置缺失会静默退回可写模式（workspace-write）"。**本方真实协议实测推翻了这一半**：app-server 在没有 `sandbox_mode` 时回落 `readOnly`，失败方向是收紧不是放宽。剩下成立的部分只有可观测性——生效沙箱不记录、不校验，权限漂移没有痕迹。
- **修复**：解析 `ThreadStartResponse.sandbox`，写进结构化日志与会话状态；显式设了 `WALKCODE_CODEX_SANDBOX` 却与回显不一致时告警。
- **回证**：VERIFIED（回证依据的是 CLI 默认 workspace-write，未实测 app-server；以本方实测为准降级）

### 4. [Warning] docs/lark-profile-deploy.md:144-146
> **一句话**：部署文档把新建线程的沙箱规则误写成所有线程的规则。

- **Category**: Consistency
- **Confidence**: dim-conventions 0.94
- **证据**：新增段落称"codex 实例的沙箱默认跟随 profile……只有显式设 WALKCODE_CODEX_SANDBOX 才会覆盖"，未限定作用范围；而 `resume_thread`（`__init__.py:8218`）只发 `threadId` 和 `cwd`。
- **问题**：新建与恢复是两条协议路径，环境变量目前只作用于 `thread/start`。运维会误以为 read-only 覆盖同样约束恢复出来的线程。`docs/adr/0061-never-close-a-turn-in-silence.md:79-80` 有同类过度断言。
- **修复**：文档限定为"新建线程"，同步修正 ADR 0061；或者干脆让 resume 也带上覆盖值（见存量项）。
- **回证**：VERIFIED @ 144-146，ADR 0061:79-80 的援引经逐字核对属实

## 💡 Suggestion（未回证，参考）

### 5. src/walkcode/channel_native/__init__.py:8132 (Symbol: CodexAppServerTransport.__init__)
> **一句话**：用空文本表示不表态，后续容易被误当成最终生效的沙箱。

- **Category**: Clarity｜**Confidence**: dim-maintainability 0.96
- **修复**：改成 `sandbox_override: str | None = None`，用 `is not None` 判断。要展示最终策略时另存响应里的 `sandbox`，不要复用覆盖字段。

## 🟣 Pre-existing（存量问题，不计入本次裁决）

### 6. src/walkcode/channel_native/__init__.py:8218 (Symbol: resume_thread)
> **一句话**：显式的沙箱限制在线程恢复后会失效。

- **Category**: Bug / Security｜**来源**: correctness 0.98 + security 0.91（两维度独立命中，均自标 PreExisting: yes）
- **问题**：`launch` 透传 `self.sandbox`，`resume_thread` 直接丢弃。app-server 重启后冷恢复会按当前 profile 重建权限，`WALKCODE_CODEX_SANDBOX=read-only` 的显式收紧因此可能被绕过。
- **修复**：`launch` 与 `resume_thread` 共用同一套参数构造；`self.sandbox` 非空时 resume 也发。
- 注：这是本次改动之前就存在的缺口（改动前 launch 恒发 read-only，恢复路径同样不发），但它和 F4 的文档问题是同一个根，一起修最省事。

## 维度元信息

| 来源 | VERDICT | issues | exit | 备注 |
|---|---|---|---|---|
| dim-correctness | NEEDS_FIX | 1 | 0 | 前两次 540s 超时，第三次加时间预算后完成 |
| dim-goalfit | NEEDS_FIX | 1 | 0 | 首次超时，重跑成功 |
| dim-maintainability | NEEDS_FIX | 2 | 0 | — |
| dim-conventions | NEEDS_FIX | 2 | 0 | — |
| dim-security | NEEDS_FIX | 3 | 0 | 其中 1 条自标 PreExisting |

`run_parallel.sh` 每次收尾都在 AUTH_SUSPECT 那行报 `CODEX_MODEL?: unbound variable`（脚本
自身在 `LANG=C.UTF-8` 下的多字节解析问题，出现在所有子任务完成之后）。不影响任何维度产物。

## 原始报告

- 各维度：`$RUN_DIR/dim-{name}.md`；回证：`$RUN_DIR/verify-{1..4}.md`
- 元信息：`$RUN_DIR/meta-*.txt`；身份：`$RUN_DIR/run.json`

---

## 修复记录（同日，报告后追加）

6 条全部处理，全量测试 1135 passed（含 `WALKCODE_E2E_CODEX_APP_SERVER=1` 的 4 条真实协议用例）。

| # | 处理 |
|---|---|
| 1 | 新增 `CodexAppServerSandboxLiveTests`（`tests/test_channel_native_codex.py`），真实 `codex app-server` + 一次性 `CODEX_HOME`，断言回显的生效沙箱：跟随 profile、缺 `sandbox_mode` 时 fail-closed、显式覆盖仍优先。挂 `WALKCODE_E2E_CODEX_APP_SERVER` 门禁 |
| 2 | 新增白名单闸：生效沙箱为 `dangerFullAccess` 且频道无任何白名单时 `launch`/`resume` 抛 `ChannelConfigError`；逃生门 `WALKCODE_CODEX_ALLOW_UNRESTRICTED_WITHOUT_ALLOWLIST=1` |
| 3 | `_apply_effective_sandbox` 解析 `ThreadStartResponse.sandbox` / `ThreadResumeResponse.sandbox`，记入 `transport.effective_sandbox`；显式覆盖未被采纳时打 `walkcode degrade=codex_sandbox_override_ignored` |
| 4 | `docs/lark-profile-deploy.md` 限定作用范围并补白名单闸说明；`docs/adr/0061:79` 的"沙箱由环境变量决定"改为"由 profile 决定、环境变量覆盖" |
| 5 | `sandbox: str = ""` → `sandbox_override: str | None = None`，`is not None` 判断；新增 `_CODEX_SANDBOX_POLICY_TYPES` 统一请求侧 SandboxMode 与响应侧 SandboxPolicy 的映射 |
| 6 | （存量）`resume_thread` 与 `launch` 共用 `_with_sandbox_override`，显式覆盖不再在冷恢复时失效 |

突变验证（防假绿）：把 `_with_sandbox_override` 改回恒发 `read-only`、并把白名单闸短路成 `if False`，
4 条测试转红（omit×2、refuse×1、resume boundary×1），确认新断言真的会在代码坏掉时失败。

### 修复过程中自查发现的一条（不在原报告里）

第 2 条的白名单闸最初抛的是 `ChannelConfigError`。追调用链时发现 lark / telegram 的
ingress 循环对这个异常类型是 `except ChannelConfigError: raise`——它被当作致命配置错误
用来结束进程。而这个闸是**每条入站消息**都会触发的运行期检查，在 launchd 下等于崩溃循环，
飞书那头还什么都看不到。

改成新的 `UnsafeSandboxError(RuntimeError)`：拒绝这一个线程，实例存活，拒绝理由进日志。
真正静态可判的那一半（显式 `WALKCODE_CODEX_SANDBOX=danger-full-access` 且无白名单）
移到 `_build_transports` 里抛 `ChannelConfigError`——启动期致命才是对的爆炸半径。
新增 `test_refusal_is_not_a_channel_config_error` 锁住这个区分。

本机两个 codex 实例实测均 `allowlist=True`，不受这条闸影响。
