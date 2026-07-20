#!/usr/bin/env bash
set -euo pipefail

# WalkCode V3 upgrade primitive.
# It upgrades the CLI package and restarts the V3 launchd runtimes:
# WALKCODE_V3_LAUNCHD_LABELS when set, otherwise the loaded com.walkcode.*
# services (com.walkcode.tap-* debug proxies are never touched — they carry
# live Claude API traffic). It does not install legacy hooks, tmux wrappers,
# or restart old `walkcode serve/start` daemons.
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

discover_v3_labels() {
  # Loaded com.walkcode.* services are the V3 runtimes. tap-* is excluded on
  # purpose: the debug proxies carry live Claude API traffic, kickstarting
  # them would sever every local session's in-flight request.
  launchctl list 2>/dev/null | awk '{print $NF}' \
    | grep -E '^com\.walkcode\.' | grep -v '^com\.walkcode\.tap-' | sort || true
}

# Self-driver guard (ADR 0058, "自杀陷阱"): when this upgrade runs INSIDE a
# session that a com.walkcode.* runtime is driving (Feishu takeover / headless
# worker), kickstarting that runtime kills our own driver mid-turn — the
# session goes silent with no reply and no error (observed 2026-07-19 and
# again 2026-07-20 15:13). Detect the driving runtime and defer its restart
# to a detached process instead of restarting it under our own feet.
SELF_RESTART_DELAY="${WALKCODE_SELF_RESTART_DELAY:-120}"

self_driver_label() {
  # Priority 1: env marker exported by `walkcode native serve` (v0.14.10+)
  # and inherited by every worker/tool subprocess it spawns.
  if [ -n "${WALKCODE_DRIVER_LABEL:-}" ]; then
    printf '%s\n' "$WALKCODE_DRIVER_LABEL"
    return
  fi
  # Priority 2 (pre-marker runtimes / SDK-spawned workers): climb the process
  # tree for a `walkcode native serve` ancestor and map its PID to a launchd
  # label. LC_ALL=C: day-first locales broke ps parsing before (v0.14.4).
  local pid=$$ depth=0 cmd ppid label
  while [ "$depth" -lt 25 ]; do
    case "$pid" in ''|*[!0-9]*) break ;; esac
    [ "$pid" -gt 1 ] || break
    cmd="$(LC_ALL=C ps -o command= -p "$pid" 2>/dev/null || true)"
    case "$cmd" in
      *"walkcode native serve"*)
        label="$(launchctl list 2>/dev/null | LC_ALL=C awk -v p="$pid" '$1 == p {print $NF}' || true)"
        if [ -n "$label" ]; then
          printf '%s\n' "$label"
          return
        fi
        ;;
    esac
    ppid="$(LC_ALL=C ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true)"
    [ -n "$ppid" ] && [ "$ppid" != "$pid" ] || break
    pid="$ppid"
    depth=$((depth + 1))
  done
  printf ''
}

schedule_deferred_self_restart() {
  local label="$1"
  if $DRY_RUN; then
    printf '  [dry-run] deferred self restart: sleep %s; launchctl kickstart -k gui/%s/%s\n' \
      "$SELF_RESTART_DELAY" "$UID_NUM" "$label"
    return
  fi
  # start_new_session=True detaches from our process group: the restart must
  # survive the very SIGTERM it is about to deliver to our ancestry.
  python3 - "$UID_NUM" "$label" "$SELF_RESTART_DELAY" <<'PY'
import subprocess
import sys

uid, label, delay = sys.argv[1:4]
subprocess.Popen(
    ["/bin/sh", "-c", 'sleep "$1"; exec launchctl kickstart -k "gui/$2/$3"', "sh", delay, uid, label],
    start_new_session=True,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
PY
  warn "$(msg \
    "this upgrade is running inside a session driven by ${label}; its restart is deferred by ${SELF_RESTART_DELAY}s (detached). This session's driver WILL restart then — wrap up the final reply now; the session revives on the next message." \
    "检测到本次升级正运行在 ${label} 驱动的会话里；已安排 ${SELF_RESTART_DELAY}s 后脱管重启（立即重启会切断当前会话自己的驱动进程）。届时本会话会短暂中断，请提前说完收尾结论；之后发消息会触发复活。")"
}

RESTARTED_LABELS=()
DEFERRED_SELF_LABEL=""

restart_v3_labels() {
  local label self_label
  local -a labels=()
  self_label="$(self_driver_label)"
  if [ -n "$LABELS_RAW" ]; then
    IFS=',' read -r -a labels <<< "$LABELS_RAW"
  else
    while IFS= read -r label; do
      [ -n "$label" ] && labels+=("$label")
    done < <(discover_v3_labels)
    if [ "${#labels[@]}" -eq 0 ]; then
      warn "$(msg \
        "WALKCODE_V3_LAUNCHD_LABELS is empty and no loaded com.walkcode.* service was found; no runtime was restarted." \
        "WALKCODE_V3_LAUNCHD_LABELS 为空且未发现已加载的 com.walkcode.* 服务；未重启任何 runtime。")"
      return
    fi
    info "$(msg \
      "WALKCODE_V3_LAUNCHD_LABELS is empty; restarting discovered labels: ${labels[*]}" \
      "WALKCODE_V3_LAUNCHD_LABELS 为空；重启自动发现的实例: ${labels[*]}")"
  fi
  for label in "${labels[@]}"; do
    label="$(echo "$label" | xargs)"
    [ -n "$label" ] || continue
    case "$label" in
      com.walkcode.tap-*)
        # Hard guard even against explicit configuration: taps proxy live
        # Claude API traffic; kickstarting one severs every local session's
        # in-flight request.
        warn "$(msg \
          "refusing to restart tap proxy ${label} (carries live Claude API traffic)." \
          "拒绝重启 tap 代理 ${label}（承载本机 Claude 会话实时 API 流量）。")"
        continue
        ;;
    esac
    if [ -n "$self_label" ] && [ "$label" = "$self_label" ]; then
      # Restarting our own driver here is the suicide trap; defer it.
      DEFERRED_SELF_LABEL="$label"
      continue
    fi
    if run launchctl kickstart -k "gui/$UID_NUM/$label"; then
      RESTARTED_LABELS+=("$label")
    else
      warn "$(msg \
        "kickstart failed for ${label}; continuing with the remaining labels." \
        "kickstart ${label} 失败；继续处理其余实例。")"
    fi
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

run uv tool install --python "$PYTHON_SPEC" --with claude-agent-sdk --with lark-oapi "$source" \
  --force --reinstall --refresh-package walkcode

restart_v3_labels

if [ -n "$DEFERRED_SELF_LABEL" ]; then
  schedule_deferred_self_restart "$DEFERRED_SELF_LABEL"
  # It WILL be restarted shortly — run its per-env doctor with the others.
  RESTARTED_LABELS+=("$DEFERRED_SELF_LABEL")
fi

doctor_one() {
  # NB: braces on ${...} inside the zh message are load-bearing — macOS
  # bash 3.2 misparses `$VAR` immediately followed by a CJK character as part
  # of the variable name ("ENV_FILE?: unbound variable" under set -u).
  local env_file="$1"
  if $DRY_RUN; then
    echo "  [dry-run] WALKCODE_ENV_FILE=$env_file walkcode native doctor"
    return
  fi
  WALKCODE_ENV_FILE="$env_file" walkcode native doctor || warn "$(msg \
    "native doctor failed; check ${env_file} before starting the runtime." \
    "native doctor 失败；启动 runtime 前请检查 ${env_file}。")"
}

if command -v walkcode >/dev/null 2>&1; then
  if [ -n "${WALKCODE_ENV_FILE:-}" ]; then
    doctor_one "$ENV_FILE"
  elif [ "${#RESTARTED_LABELS[@]}" -gt 0 ]; then
    # One doctor per restarted instance, bound to its own env file — a bare
    # doctor without WALKCODE_ENV_FILE only reports a config error.
    for label in ${RESTARTED_LABELS[@]+"${RESTARTED_LABELS[@]}"}; do
      label_env="$HOME/.walkcode/${label#com.walkcode.}.env"
      if [ -f "$label_env" ]; then
        doctor_one "$label_env"
      else
        warn "$(msg \
          "no env file for ${label} (expected ${label_env}); doctor skipped." \
          "${label} 没有对应 env 文件（应为 ${label_env}）；跳过 doctor。")"
      fi
    done
  elif [ -f "$ENV_FILE" ]; then
    doctor_one "$ENV_FILE"
  else
    warn "$(msg \
      "no runtime restarted and default env file missing; run WALKCODE_ENV_FILE=<env> walkcode native doctor manually." \
      "未重启任何 runtime 且默认 env 文件不存在；请手动运行 WALKCODE_ENV_FILE=<env> walkcode native doctor。")"
  fi
fi

new_ver="$(current_version)"
new_ver="${new_ver:-unknown}"
info "$(msg "Upgrade complete: $old_ver -> $new_ver." "升级完成: ${old_ver} -> ${new_ver}。")"
