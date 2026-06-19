---
name: walkcode-release
version: 1.0.0
description: >
  WalkCode 发布与本地升级编排（带门禁，全自动）。先 release 再 upgrade：release =
  bump 版本 + 跑测试 + /deep-review 过关(无 Critical) + 合并 main + 打 tag + 建 GitHub
  Release；upgrade = walkcode upgrade + 重启并验证两个 launchd 实例。触发：发版、
  release、上线、ship、cut a release、升级 walkcode、部署 walkcode、bump 版本。
metadata:
  scripts: ["release.sh", "upgrade.sh"]
---

# walkcode-release

把 WalkCode 的发布和本地升级固化成可重复、带门禁的流程。脚本只做机械步骤；本 skill 负责编排和门禁。

## 铁律（违反即停）

- **顺序不可换：先 release，再 upgrade**。`walkcode upgrade` 拉的是 GitHub **Release**（Releases API），所以必须先把改动合并进 `main` 并建好 Release，本地 upgrade 才拿得到新代码。
- **账号必须是 `0x5446`**（脚本会校验 `gh` 当前账号）。
- **门禁**：单测必须全绿、`/deep-review` 必须过且**无 Critical**，才能合并 PR。
- **tag 打在合并后的 `main`**，不在分支上发版。
- **两个 launchd 实例**（claude `com.alpha.walkcode` + codex `com.alpha.walkcode-codex`）都要升、都要验。
- 版本单一真源是 `pyproject.toml`（`__init__.py` 从安装元数据派生，不要手改）。

## 全自动带门禁流程

前提：要发布的代码改动已经写好（在工作区或已 commit）。

1. **prepare**：`./release.sh prepare [VERSION] -m "<type(scope): 描述 (vX.Y.Z)>"`
   - 不传 VERSION 默认 patch 自增。会 bump `pyproject.toml`、跑测试（挂了就中止）、建 `release/vX.Y.Z` 分支、`git add -A` 提交、push、开 PR。
   - 记下输出的 PR 编号/URL。
2. **门禁 deep-review**：对本次 diff 跑 `/deep-review`。命中 **Critical** → 修复 → 重跑，直到过。Warning 酌情修。**没过不许进下一步。**
3. **合并**（门禁通过后）：`gh pr merge <PR#> --merge --delete-branch`，然后 `git checkout main && git pull --ff-only`。
4. **publish**：`./release.sh publish [VERSION]`
   - 在 `main` 打 `vX.Y.Z` tag、push、`gh release create --latest`（release notes 自动取上个 tag 到 HEAD 的 commit）。
5. **本地升级**：`./upgrade.sh`
   - `walkcode upgrade` + 补 codex hooks + kickstart 两个实例 + 验证（PID 稳定不 crash-loop + 日志出现 Feishu 连接标记）。
6. **报告**：版本号、PR URL、Release URL、两个实例是否 healthy。

预演任意一步可加 `--dry-run`（打印将执行的动作，无副作用），例如 `./release.sh prepare --dry-run`。

## 回滚

`./upgrade.sh` 报实例异常时（或发版后线上不对）：

```
uv tool install 'git+https://github.com/0x5446/walkcode@v<上个好版本>' --force
launchctl kickstart -k gui/$(id -u)/com.alpha.walkcode
launchctl kickstart -k gui/$(id -u)/com.alpha.walkcode-codex
```

## 脚本速查

| 命令 | 作用 | 何时 |
|---|---|---|
| `./release.sh prepare [VER] -m MSG` | bump + 测试 + 分支 + commit + push + PR | 改动写好后 |
| `./release.sh publish [VER]` | main 打 tag + 建 GitHub Release | PR 合并进 main 后 |
| `./upgrade.sh` | walkcode upgrade + 重启验证两个 launchd 实例 | Release 建好后 |

合并（`gh pr merge`）刻意不在脚本里——它是 deep-review 门禁点，由本流程在第 3 步手动执行。
