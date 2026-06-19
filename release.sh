#!/usr/bin/env bash
set -euo pipefail

# WalkCode release primitive (mechanical steps only — gates/orchestration live in
# the `walkcode-release` skill). Two phases around the PR merge:
#
#   release.sh prepare [VERSION] [-m MSG] [--dry-run]
#       bump pyproject version, run tests, branch release/vX.Y.Z, commit, push, open PR
#   ... run /deep-review, merge the PR, then on main ...
#   release.sh publish [VERSION] [--dry-run]
#       tag the merged main and create the GitHub Release (--latest)
#
# Merge is intentionally NOT done here — it is the gate.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ACCOUNT="0x5446"
PYPROJECT="pyproject.toml"
DRY_RUN=false

# --- Colors / logging ---
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[release]${NC} $*"; }
warn()  { echo -e "${YELLOW}[release]${NC} $*"; }
error() { echo -e "${RED}[release]${NC} $*" >&2; }
is_zh() { case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac; return 1; }
msg()   { if is_zh; then echo "$2"; else echo "$1"; fi; }
die()   { error "$1"; exit 1; }

# --- Helpers ---
need() { command -v "$1" >/dev/null 2>&1 || die "$(msg "missing required tool: $1" "缺少必需工具: $1")"; }

require_account() {
  local who
  who=$(gh api user --jq .login 2>/dev/null || true)
  if [ "$who" != "$ACCOUNT" ]; then
    die "$(msg "gh active account is '${who:-none}', must be $ACCOUNT (walkcode repo rule)" \
            "gh 当前账号是 '${who:-无}'，必须用 $ACCOUNT 操作 walkcode 仓库")"
  fi
}

pyproject_version() { sed -n 's/^version = "\(.*\)"/\1/p' "$PYPROJECT" | head -1; }

valid_semver() { [[ "$1" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; }

bump_patch() {
  local v="$1" major minor patch
  IFS=. read -r major minor patch <<<"$v"
  echo "${major}.${minor}.$((patch + 1))"
}

# new > current (and not equal)
version_gt() {
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | tail -1)" = "$1" ]
}

set_pyproject_version() {
  local ver="$1"
  if $DRY_RUN; then echo "  [dry-run] set $PYPROJECT version -> $ver"; return; fi
  sed -i.bak "s/^version = \".*\"/version = \"$ver\"/" "$PYPROJECT" && rm -f "$PYPROJECT.bak"
}

run() {  # echo + run, or just echo under --dry-run (simple commands only, no pipes)
  if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi
}

tag_exists() { git rev-parse -q --verify "refs/tags/$1" >/dev/null 2>&1 \
               || git ls-remote --exit-code --tags origin "$1" >/dev/null 2>&1; }

# --- prepare ---
cmd_prepare() {
  local version="" pr_msg=""
  while [ $# -gt 0 ]; do
    case "$1" in
      -m) pr_msg="$2"; shift 2 ;;
      --dry-run) DRY_RUN=true; shift ;;
      -*) die "unknown flag: $1" ;;
      *) version="$1"; shift ;;
    esac
  done

  need git; need gh; need uv
  require_account

  local cur; cur=$(pyproject_version)
  [ -n "$cur" ] || die "cannot read current version from $PYPROJECT"
  [ -n "$version" ] || version=$(bump_patch "$cur")
  valid_semver "$version" || die "invalid version: $version (want X.Y.Z)"
  version_gt "$version" "$cur" || die "version $version must be greater than current $cur"
  tag_exists "v$version" && die "tag v$version already exists"
  [ -z "$pr_msg" ] && pr_msg="release v$version"

  local branch="release/v$version"
  info "$(msg "Preparing $branch (from $cur)" "准备 ${branch}（当前 ${cur}）")"

  set_pyproject_version "$version"

  info "$(msg "Running tests..." "运行测试...")"
  if $DRY_RUN; then
    echo "  [dry-run] uv run python -m unittest discover -s tests -p 'test_*.py'"
  else
    uv run python -m unittest discover -s tests -p "test_*.py" \
      || die "$(msg "tests failed — aborting (version bump left uncommitted)" \
                    "测试失败 — 中止（版本号改动留在工作区未提交）")"
  fi

  run git checkout -b "$branch"
  run git add -A
  run git commit -m "$pr_msg"
  run git push -u origin "$branch"
  run gh pr create --base main --head "$branch" --title "$pr_msg" \
        --body "$(printf 'Release v%s\n\n🤖 prepared by release.sh' "$version")"

  info "$(msg "PR opened. Next: run /deep-review, merge the PR, then:" \
              "PR 已创建。下一步：跑 /deep-review，合并 PR，然后：")"
  echo "    git checkout main && git pull --ff-only"
  echo "    ./release.sh publish $version"
}

# --- publish (run after the PR is merged into main) ---
cmd_publish() {
  local version=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --dry-run) DRY_RUN=true; shift ;;
      -*) die "unknown flag: $1" ;;
      *) version="$1"; shift ;;
    esac
  done

  need git; need gh
  require_account

  local cur_branch; cur_branch=$(git rev-parse --abbrev-ref HEAD)
  [ "$cur_branch" = "main" ] || die "$(msg "publish must run on main (now on $cur_branch)" \
                                          "publish 必须在 main 上运行（当前 ${cur_branch}）")"
  run git pull --ff-only

  # Gate: HEAD must equal origin/main, so a local-only commit that bypassed the
  # "merge into main first" review gate can never be tagged + released.
  if ! $DRY_RUN; then
    git fetch origin main -q
    local lh rh; lh=$(git rev-parse HEAD); rh=$(git rev-parse origin/main)
    [ "$lh" = "$rh" ] || die "$(msg "HEAD ($lh) != origin/main ($rh) — merge into main before publishing" \
                                    "HEAD ($lh) 与 origin/main ($rh) 不一致 — 先合并进 main 再发布")"
  fi

  local cur; cur=$(pyproject_version)
  [ -n "$version" ] || version="$cur"
  valid_semver "$version" || die "invalid version: $version"
  [ "$version" = "$cur" ] || die "$(msg "version $version != merged pyproject $cur — is the bump merged?" \
                                        "版本 $version 与 main 上 pyproject 的 $cur 不一致 — bump 合并了吗？")"
  tag_exists "v$version" && die "tag v$version already exists"

  local prev; prev=$(git describe --tags --abbrev=0 2>/dev/null || true)
  local notes
  if [ -n "$prev" ]; then
    notes=$(git log "${prev}..HEAD" --oneline)
  else
    notes=$(git log --oneline -20)
  fi

  info "$(msg "Tagging v$version on main" "在 main 打 tag v$version")"
  run git tag -a "v$version" -m "v$version"
  run git push origin "v$version"

  if $DRY_RUN; then
    echo "  [dry-run] gh release create v$version --latest --title v$version --notes <<"
    printf '%s\n' "$notes" | sed 's/^/      /'
  else
    gh release create "v$version" --latest --title "v$version" --notes "$notes"
  fi
  info "$(msg "Release v$version published. Now run ./upgrade.sh locally." \
              "Release v$version 已发布。本地执行 ./upgrade.sh 升级。")"
}

usage() {
  cat <<EOF
Usage:
  ./release.sh prepare [VERSION] [-m MSG] [--dry-run]   bump+test+branch+commit+push+PR
  ./release.sh publish [VERSION] [--dry-run]            tag merged main + create GitHub Release

VERSION defaults to a patch bump of pyproject.toml ($(pyproject_version)).
Merge the PR (after /deep-review) between prepare and publish — that is the gate.
EOF
}

case "${1:-}" in
  prepare) shift; cmd_prepare "$@" ;;
  publish) shift; cmd_publish "$@" ;;
  -h|--help|"") usage ;;
  *) error "unknown command: $1"; usage; exit 1 ;;
esac
