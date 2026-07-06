# Deep Review 综合结论：claude-tap 终端 TUI 接入文档

**VERDICT**: NEEDS_FIX（8 条 findings，无 Critical；已在后续 commit 全部修复）
**轮次**：1 / 3
**类型**：design（纯文档）

> 范围：commit 05c5765 —— docs/claude-tap-deploy.md 新增「可选：终端 TUI 会话接入」+ 影响面改写 + README 中英指向句
> Review engine：codex codex-cli 0.142.5（host: claude; engine_source: auto）
> Cursor：disabled（design-only target 不启用）
> 维度：6 个 codex 并行（completeness / clarity / feasibility / consistency / extensibility / risk，全部成功）
> Phase 2 验证：5 条已派（另 3 条高共识高自信免回证）；结果 5 VERIFIED / 0 FALSE_POSITIVE / 0 UNVERIFIABLE
> Repo: /Users/alpha/workspace/walkcode/.claude/worktrees/tap-tui-docs
> HeadSHA: 05c576500011c64f5867088cf5dd6b4664b8eaed
> RunDir: /var/folders/00/s7tt4dgj53v123y8671yb3b00000gn/T/deep-review-tap-tui-docs-05c5765-1783340815.Fw7l
> 规模：3 文件 / +69-7 行
> plan-only 模式：skill 未自动 fix；修复由主 agent 在后续 commit 应用

## 🔴🔴 顶级必修（多维度共识 + 回证/免回证）

### 1. [Warning] docs/claude-tap-deploy.md「可选：终端 TUI 会话接入」
> **一句话**：接入说明只教改最后一行启动命令，日常裸启动的后台会话路径会被漏掉，主路径仍然直连。

- **来源**: risk + clarity + feasibility（3 维度）；回证 VERIFIED
- **问题**: 本仓库 profile wrapper 是 daemon-native，裸启动 = `claude --bg` 创建 worker + attach；真正发起上游请求的是 `--bg` 调用。文档只说替换最终 `exec claude "$@"`。
- **修复**: 明确"所有会创建 worker 的 claude 调用都要带 `${tap_settings[@]}`"，至少 `claude --bg` 与最终 `exec` 两处；attach 不需要。

### 2. [Warning] 参考片段 `except Exception: pass` 吞配置错误
> **一句话**：配置文件损坏时片段静默生成只有代理地址的覆盖文件，会话丢认证却看不出原因。

- **来源**: risk + consistency + completeness + clarity（4 维度，conf ≥0.9 免回证）
- **问题**: settings.json 存在但损坏时生成残缺覆盖 → `Not logged in`，与 walkcode 侧"损坏的 settings.json 响亮失败"（ADR 0047）语义相反。
- **修复**: 区分"文件不存在"（空 env 继续）与"存在但不可读/解析失败"（打印错误并非零退出 → wrapper 不注入，回退直连）。

### 3. [Warning] "tap 挂了自动回退直连"表述过宽
> **一句话**：自动回退只发生在启动那一刻，会话跑起来之后代理挂掉并不会自动改回直连。

- **来源**: risk + consistency + clarity（3 维度，conf ≥0.9 免回证）
- **修复**: 全部改为"启动时软依赖"语义（部署文档影响面 + TUI 节 + 中英 README）。

### 4. [Warning] 端口手填与"taps.conf 唯一配置源"矛盾
> **一句话**：端口要手工抄到每个启动脚本里，之后改配置文件不会同步，容易连错或漏接。

- **来源**: consistency + extensibility（2 维度，conf ≥0.9 免回证；cursor 加权规则不适用，同引擎）
- **修复**: 片段改为用 awk 从 `~/.walkcode/claude-tap/taps.conf` 按 profile 动态读端口，taps.conf 保持唯一配置源。

## 🔴 高置信必修（单维度 + 回证 VERIFIED）

### 5. [Warning] `nc -z -G 1` 跨实现不兼容
- **来源**: feasibility；回证 VERIFIED（实测本机 PATH 命中 Homebrew GNU netcat，`-G` 是 source-routing pointer 而非连接超时）
- **修复**: 改用 BSD/GNU 都支持的 `nc -z -w 1`。

### 6. [Suggestion] README_EN 引用不存在的英文小节名
- **来源**: consistency；回证 VERIFIED
- **修复**: 英文 README 直接引用实际中文标题并附英文注释。

### 7. [Warning] 排障表缺 TUI 场景条目
- **来源**: completeness；回证 VERIFIED
- **修复**: 排障表新增"终端 TUI 会话不进 dashboard"一行（重启会话 / 端口探测 / nc 与 python3 依赖 / --settings 是否实际传入）。

### 8. [Warning] 停用后 TUI 覆盖文件密钥副本残留
- **来源**: risk；回证 VERIFIED（`setup.sh remove` 不清理该文件）
- **修复**: TUI 节补充：长期停用时手动删除 `{CLAUDE_CONFIG_DIR}/tui-tap-override-settings.json`。

## ❌ 已驳回

无（Phase 2 零 FALSE_POSITIVE）。

## 维度元信息

| 来源 | VERDICT | issues | exit |
|---|---|---|---|
| dim-completeness | NEEDS_FIX | 2 | 0 |
| dim-clarity | NEEDS_FIX | 3 | 0 |
| dim-feasibility | NEEDS_FIX | 2 | 0 |
| dim-consistency | NEEDS_FIX | 4 | 0 |
| dim-extensibility | NEEDS_FIX | 1 | 0 |
| dim-risk | NEEDS_FIX | 4 | 0 |

注：各维度间高度重叠（同一问题被多维度独立命中），归并后净 findings 为上述 8 条。
维度日志中 grep 到的 "Not logged in" 为文档正文内容误命中，非认证故障。

## 原始报告

- 各维度：`$RUN_DIR/dim-{completeness,clarity,feasibility,consistency,extensibility,risk}.md`
- 各回证：`$RUN_DIR/verify-{A,E,F,G,H}.md`
- 元信息：`$RUN_DIR/meta.txt`、`$RUN_DIR/run.json`
