#!/usr/bin/env bash
set -euo pipefail

# WalkCode V3 upgrade primitive.
# It upgrades the CLI package and restarts only explicitly configured V3
# launchd labels. It does not install legacy hooks, tmux wrappers, or restart
# old `walkcode serve/start` daemons.
#
# Usage:
#   ./upgrade.sh [--dry-run]
#
# Optional env:
#   WALKCODE_V3_LAUNCHD_LABELS="com.walkcode.telegram-claude,com.walkcode.telegram-codex"
#   WALKCODE_ENV_FILE=~/.walkcode/telegram-claude.env

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

REPO="0x5446/walkcode"
GITHUB_URL="https://github.com/${REPO}.git"
PYTHON_SPEC="${WALKCODE_PYTHON:-3.13}"
ENV_FILE="${WALKCODE_ENV_FILE:-$HOME/.walkcode/telegram-claude.env}"
LABELS_RAW="${WALKCODE_V3_LAUNCHD_LABELS:-}"
UID_NUM="$(id -u)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[upgrade]${NC} $*"; }
warn()  { echo -e "${YELLOW}[upgrade]${NC} $*"; }
error() { echo -e "${RED}[upgrade]${NC} $*" >&2; }
is_zh() { case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac; return 1; }
msg()   { if is_zh; then echo "$2"; else echo "$1"; fi; }
die()   { error "$1"; exit 1; }
run()   { if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

command -v uv >/dev/null 2>&1 || die "$(msg "uv not found in PATH" "PATH 中找不到 uv")"

LOCK_DIR="${TMPDIR:-/tmp}/walkcode-upgrade.lock"
if ! $DRY_RUN; then
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
      die "$(msg "another upgrade is running (pid $owner)" "已有升级在运行（pid ${owner}）")"
    fi
    warn "$(msg "reclaiming stale upgrade lock" "清理残留升级锁")"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || die "$(msg "cannot acquire upgrade lock" "无法获取升级锁")"
  fi
  echo "$$" > "$LOCK_DIR/pid"
  trap 'if [ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then rm -rf "$LOCK_DIR" 2>/dev/null; fi' INT TERM HUP EXIT
fi

current_version() {
  walkcode --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true
}

latest_tag() {
  python3 - "$REPO" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request

repo = sys.argv[1]
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/releases/latest",
    headers={"Accept": "application/vnd.github+json"},
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(json.loads(resp.read()).get("tag_name", ""))
PY
}

detect_legacy_remnants() {
  local found=0
  local wrapper_path="$HOME/.agent-control-plane/agent-wrappers.sh"
  local wrapper_is_legacy=0

  if ls "$HOME"/Library/LaunchAgents/com.walkcode*.plist >/dev/null 2>&1; then
    for plist in "$HOME"/Library/LaunchAgents/com.walkcode*.plist; do
      if grep -Eq 'walkcode([[:space:]]|</string>[[:space:]]*<string>)(serve|start)' "$plist" 2>/dev/null; then
        warn "$(msg \
          "Legacy LaunchAgent still points at walkcode serve/start: $plist" \
          "仍有旧版 LaunchAgent 指向 walkcode serve/start: $plist")"
        found=1
      fi
    done
  fi

  for hook_file in "$HOME/.claude/settings.json" "$HOME/.codex/hooks.json"; do
    if [ -f "$hook_file" ] && grep -q 'walkcode hook' "$hook_file" 2>/dev/null; then
      warn "$(msg \
        "Legacy hook config still points at walkcode hook: $hook_file" \
        "仍有旧版 hook 配置指向 walkcode hook: $hook_file")"
      found=1
    fi
  done

  if [ -f "$wrapper_path" ] && grep -Eq 'tmux|walkcode[[:space:]]+(hook|serve|start|status|test-inject)|WALKCODE_PORT|WALKCODE_INSTANCE|\.walkcode/codex\.env|FEISHU_' "$wrapper_path" 2>/dev/null; then
    wrapper_is_legacy=1
    warn "$(msg \
      "Legacy shell wrapper behavior still exists: $wrapper_path" \
      "仍有旧版 shell wrapper 行为: $wrapper_path")"
    found=1
  fi

  for shell_rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bashrc"; do
    if [ "$wrapper_is_legacy" -eq 1 ] && grep -q 'agent-wrappers.sh' "$shell_rc" 2>/dev/null; then
      warn "$(msg \
        "$shell_rc still sources a legacy agent-wrappers.sh" \
        "$shell_rc 仍引用旧版 agent-wrappers.sh")"
      found=1
    fi
  done

  if [ -d "$HOME/.walkcode" ] && ls "$HOME/.walkcode"/*.env >/dev/null 2>&1; then
    for env_path in "$HOME/.walkcode"/*.env; do
      if grep -q 'FEISHU_' "$env_path" 2>/dev/null; then
        warn "$(msg \
          "Legacy FEISHU_* env still exists: $env_path" \
          "仍有旧版 FEISHU_* env: $env_path")"
        found=1
      fi
    done
  fi

  if env | grep -q '^FEISHU_'; then
    warn "$(msg \
      "Current shell exports FEISHU_* variables" \
      "当前 shell 导出了 FEISHU_* 变量")"
    found=1
  fi

  return "$found"
}

restart_v3_labels() {
  if [ -z "$LABELS_RAW" ]; then
    warn "$(msg \
      "WALKCODE_V3_LAUNCHD_LABELS is empty; no runtime was restarted." \
      "WALKCODE_V3_LAUNCHD_LABELS 为空；未重启任何 runtime。")"
    return
  fi

  local label
  IFS=',' read -r -a labels <<< "$LABELS_RAW"
  for label in "${labels[@]}"; do
    label="$(echo "$label" | xargs)"
    [ -n "$label" ] || continue
    run launchctl kickstart -k "gui/$UID_NUM/$label"
  done
}

old_ver="$(current_version)"
old_ver="${old_ver:-unknown}"
info "$(msg "Current version: $old_ver" "当前版本: ${old_ver}")"

if detect_legacy_remnants; then
  :
else
  die "$(msg \
    "legacy remnants must be cleaned before upgrading to the V3 runtime" \
    "升级到 V3 runtime 前必须先清理旧版残留")"
fi

tag="$(latest_tag)"
if [ -n "$tag" ]; then
  source="walkcode @ git+${GITHUB_URL}@${tag}"
  info "$(msg "Latest release: $tag" "最新版本: ${tag}")"
else
  source="walkcode @ git+${GITHUB_URL}"
  warn "$(msg "No release tag detected; installing from main." "未检测到 release tag；从 main 安装。")"
fi

run uv tool install --python "$PYTHON_SPEC" --with claude-agent-sdk "$source" \
  --force --reinstall --refresh-package walkcode

restart_v3_labels

if command -v walkcode >/dev/null 2>&1; then
  if $DRY_RUN; then
    echo "  [dry-run] WALKCODE_ENV_FILE=$ENV_FILE walkcode native doctor"
  else
    WALKCODE_ENV_FILE="$ENV_FILE" walkcode native doctor || warn "$(msg \
      "native doctor failed; check $ENV_FILE before starting the runtime." \
      "native doctor 失败；启动 runtime 前请检查 $ENV_FILE。")"
  fi
fi

new_ver="$(current_version)"
new_ver="${new_ver:-unknown}"
info "$(msg "Upgrade complete: $old_ver -> $new_ver." "升级完成: ${old_ver} -> ${new_ver}。")"
