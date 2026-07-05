---
name: walkcode-release
version: 1.0.0
description: >
  WalkCode V3 发布与本地升级编排（带门禁，全自动）。先 release 再 upgrade：release =
  bump 版本 + 跑测试 + deep-review skill 过关(无 Critical) + 合并 main + 打 tag + 建 GitHub
  Release；upgrade = 安装最新 V3 CLI + 重启 WALKCODE_V3_LAUNCHD_LABELS 指定的 native runtime
  + walkcode native doctor 验证。触发：发版、release、上线、ship、cut a release、升级
  walkcode、部署 walkcode、bump 版本。
metadata:
  scripts: ["release.sh", "upgrade.sh"]
---

# walkcode-release

把 WalkCode 的发布和本地升级固化成可重复、带门禁的流程。脚本只做机械步骤；本 skill 负责编排和门禁。

## 铁律（违反即停）

- **顺序不可换：先 release，再 upgrade**。`walkcode upgrade` 拉的是 GitHub **Release**（Releases API），所以必须先把改动合并进 `main` 并建好 Release，本地 upgrade 才拿得到新代码。
- **账号必须是 `0x5446`**（脚本会校验 `gh` 当前账号）。
- **门禁**：单测必须全绿、deep-review skill 必须过且**无 Critical**，才能合并 PR。
  Review 门禁由 deep-review skill 执行；不要用普通 `codex review` / `claude review` 替代。
- **tag 打在合并后的 `main`**，不在分支上发版。
- **V3 launchd 实例显式列出**：只重启 `WALKCODE_V3_LAUNCHD_LABELS` 中的
  native runtime，例如 `com.walkcode.telegram-claude,com.walkcode.telegram-codex`。
  不重启旧 `walkcode serve/start` daemon。
- **旧版残留是阻断项**：旧 LaunchAgent、`walkcode hook`、shell wrapper source、
  `FEISHU_*` env 存在时先清理；不要带着残留进入 upgrade 或真实 E2E。
- **一个 runtime 一个身份**：一份 env 只能有一个 `WALKCODE_CHANNEL`、一个
  `WALKCODE_AGENT`、一个 bot/app identity、一个 `WALKCODE_STATE_PATH`。Claude 和
  Codex 必须拆成两份 env、两个 bot、两个 state。
- 版本单一真源是 `pyproject.toml`（`__init__.py` 从安装元数据派生，不要手改）。
- **prepare 前置**（脚本强制）：当前在 `main`、本地 `main` == `origin/main`、**无未跟踪文件**。本次要发的新文件先 `git add`，其余杂物清掉——否则 `git add -A` 会把别的东西卷进发布分支。**多个 agent 别共用一个 checkout**，并行任务各用独立 git worktree。

## 全自动带门禁流程

前提：要发布的代码改动已经写好（在工作区或已 commit）。

1. **prepare**：`./release.sh prepare [VERSION] -m "<type(scope): 描述 (vX.Y.Z)>"`
   - 前置（脚本会拒绝不满足的）：在 `main`、`main`==`origin/main`、无未跟踪文件；本次新文件先 `git add`。
   - 不传 VERSION 默认 patch 自增。会 bump `pyproject.toml`、跑测试（挂了就中止）、建 `release/vX.Y.Z` 分支、`git add -A` 提交、push、开 PR。
   - 记下输出的 PR 编号/URL。
2. **门禁 deep-review**：调用 deep-review skill 对本次 diff 做 CR。命中 **Critical** → 修复 → 重跑，直到过。Warning 酌情修。**没过不许进下一步。**
3. **合并**（门禁通过后）：`gh pr merge <PR#> --merge --delete-branch`，然后 `git checkout main && git pull --ff-only`。
4. **publish**：`./release.sh publish [VERSION]`
   - 打 tag 前校验 `HEAD`==`origin/main`（防发未合并的本地提交）。在 `main` 打 `vX.Y.Z` tag、push、`gh release create --latest`（release notes 自动取上个 tag 到 HEAD 的 commit）。
   - **可重入**：若 tag 已在 HEAD 但 Release 没建成（如 `gh` 中途失败），重跑会跳过打 tag、续建 Release；tag 指向别处才报错。
5. **本地升级**：`./upgrade.sh`
   - 安装最新 Release。
   - 不写 legacy `walkcode hook`，不安装 tmux wrapper。
   - 发现旧 LaunchAgent、old hook、shell wrapper、`FEISHU_*` env 会直接失败。
   - 只 kickstart `WALKCODE_V3_LAUNCHD_LABELS`。
   - 运行 `walkcode native doctor`；真实验收继续跑模块 gate：
     `config`、`runtime`、`state`、`outbox`、`agent`、`telegram`、必要时
     `agent-smoke --live`。
   - 并发升级被目录锁挡住。
6. **报告**：版本号、PR URL、Release URL、每个 V3 runtime label 的 doctor/gate 状态。

预演任意一步可加 `--dry-run`（打印将执行的动作，无副作用），例如 `./release.sh prepare --dry-run`。

## 回滚

`./upgrade.sh` 报实例异常时（或发版后线上不对）：

```
uv tool install 'git+https://github.com/0x5446/walkcode@v<上个好版本>' --force --reinstall --refresh-package walkcode
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-claude
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-codex
```

## 脚本速查

| 命令 | 作用 | 何时 |
|---|---|---|
| `./release.sh prepare [VER] -m MSG` | bump + 测试 + 分支 + commit + push + PR | 改动写好后 |
| `./release.sh publish [VER]` | main 打 tag + 建 GitHub Release | PR 合并进 main 后 |
| `./upgrade.sh` | 安装最新 V3 + 重启验证显式配置的 native launchd 实例 | Release 建好后 |

合并（`gh pr merge`）刻意不在脚本里——它是 deep-review 门禁点，由本流程在第 3 步手动执行。
