#!/usr/bin/env bash
set -euo pipefail

# WalkCode local deploy primitive: upgrade the installed code to the latest
# GitHub Release and restart + verify the launchd instances.
#
# `walkcode upgrade` does NOT restart under launchd here (the plist runs
# `walkcode serve` directly and writes no pid file, so its built-in pid-based
# restart is skipped). So we kickstart both instances explicitly and verify they
# came up and aren't crash-looping.
#
#   ./upgrade.sh [--dry-run]
#
# Override the (machine-specific) launchd labels / logs via env if needed:
#   WALKCODE_LAUNCHD_LABEL, WALKCODE_LAUNCHD_LABEL_CODEX, LOG_CLAUDE, LOG_CODEX

DRY_RUN=false
[ "${1:-}" = "--dry-run" ] && DRY_RUN=true

LABEL_CLAUDE="${WALKCODE_LAUNCHD_LABEL:-com.alpha.walkcode}"
LABEL_CODEX="${WALKCODE_LAUNCHD_LABEL_CODEX:-com.alpha.walkcode-codex}"
LOG_CLAUDE="${LOG_CLAUDE:-$HOME/.walkcode/launchd.claude.err.log}"
LOG_CODEX="${LOG_CODEX:-$HOME/.walkcode/launchd.codex.err.log}"
CODEX_ENV="${WALKCODE_CODEX_ENV:-$HOME/.walkcode/codex.env}"
READY_RE="Feishu WebSocket|WebSocket client started|connected|listening"
UID_NUM="$(id -u)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[upgrade]${NC} $*"; }
warn()  { echo -e "${YELLOW}[upgrade]${NC} $*"; }
error() { echo -e "${RED}[upgrade]${NC} $*" >&2; }
is_zh() { case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac; return 1; }
msg()   { if is_zh; then echo "$2"; else echo "$1"; fi; }
die()   { error "$1"; exit 1; }
run()   { if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

command -v walkcode >/dev/null 2>&1 || die "$(msg "walkcode not found in PATH" "PATH 中找不到 walkcode")"

wc_version() {  # just the X.Y.Z (walkcode --version prints "walkcode X.Y.Z")
  walkcode --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true
}

instance_pid() {  # echo the launchd-reported PID for a label, or empty
  launchctl list "$1" 2>/dev/null | sed -n 's/.*"PID" = \([0-9]*\);.*/\1/p' | head -1
}

verify_instance() {  # label log offset -> 0 ok / 1 fail
  local label="$1" log="$2" offset="${3:-0}"
  if $DRY_RUN; then echo "  [dry-run] verify $label (PID stable) + grep ready in $log after byte $offset"; return 0; fi
  local p1 p2
  sleep 2
  p1=$(instance_pid "$label")
  [ -n "$p1" ] || { error "$(msg "$label not running" "$label 未运行")"; return 1; }
  sleep 3
  p2=$(instance_pid "$label")
  if [ -z "$p2" ] || [ "$p1" != "$p2" ]; then
    error "$(msg "$label is crash-looping (PID $p1 -> ${p2:-gone})" "$label 在 crash-loop（PID $p1 -> ${p2:-没了}）")"
    return 1
  fi
  # Only scan log bytes written AFTER this run's restart (offset captured
  # pre-kickstart), so a stale "connected" line from the old process can't make
  # a freshly-restarted instance look healthy.
  if tail -c "+$((offset + 1))" "$log" 2>/dev/null | grep -Eq "$READY_RE"; then
    info "$(msg "$label up (PID $p2), Feishu connected" "$label 已起（PID ${p2}），飞书已连接")"
    return 0
  fi
  warn "$(msg "$label PID stable ($p2) but no NEW ready marker in log yet — check $log" \
              "$label PID 稳定（${p2}）但重启后日志暂无 ready 标记 — 看 $log")"
  return 0  # PID stable is the hard signal; missing marker is a soft warning
}

old_ver=$(wc_version); old_ver=${old_ver:-unknown}
info "$(msg "Current version: $old_ver" "当前版本: $old_ver")"

# 1) install latest released code (+ claude hooks); no restart under launchd
run walkcode upgrade
# 2) codex hooks (upgrade only refreshed the default/claude agent's hooks).
#    MUST carry WALKCODE_ENV_FILE=codex.env — otherwise install-hooks writes the
#    codex hooks pointing at the default port (3001) and without the codex creds,
#    wiring the codex instance back to the claude instance. Hard error, not warn.
rc_hooks=0
if $DRY_RUN; then
  echo "  [dry-run] WALKCODE_ENV_FILE=$CODEX_ENV walkcode install-hooks --agent codex"
elif [ ! -f "$CODEX_ENV" ]; then
  error "$(msg "codex env file not found: $CODEX_ENV (set WALKCODE_CODEX_ENV)" "codex env 文件不存在: $CODEX_ENV（用 WALKCODE_CODEX_ENV 指定）")"
  rc_hooks=1
elif ! WALKCODE_ENV_FILE="$CODEX_ENV" walkcode install-hooks --agent codex; then
  error "$(msg "codex install-hooks failed" "codex install-hooks 失败")"
  rc_hooks=1
fi

# 3) capture pre-restart log sizes so verify scans only this run's new output
off_claude=0; off_codex=0
if ! $DRY_RUN; then
  [ -f "$LOG_CLAUDE" ] && off_claude=$(wc -c < "$LOG_CLAUDE" | tr -d ' ')
  [ -f "$LOG_CODEX" ]  && off_codex=$(wc -c < "$LOG_CODEX" | tr -d ' ')
fi

# 4) restart both launchd instances cleanly
run launchctl kickstart -k "gui/$UID_NUM/$LABEL_CLAUDE"
run launchctl kickstart -k "gui/$UID_NUM/$LABEL_CODEX"

# 5) verify both came up and are stable (codex hook failure already folded in)
rc=$rc_hooks
verify_instance "$LABEL_CLAUDE" "$LOG_CLAUDE" "$off_claude" || rc=1
verify_instance "$LABEL_CODEX"  "$LOG_CODEX"  "$off_codex"  || rc=1

if $DRY_RUN; then new_ver="$old_ver"; else new_ver=$(wc_version); new_ver=${new_ver:-unknown}; fi
echo
if [ "$rc" -eq 0 ]; then
  info "$(msg "Upgrade complete: $old_ver -> $new_ver; both instances healthy." \
              "升级完成: $old_ver -> ${new_ver}；两个实例均正常。")"
else
  error "$(msg "Upgrade finished but an instance is unhealthy. Rollback:" \
              "升级完成但有实例异常。回滚：")"
  echo "    uv tool install 'git+https://github.com/0x5446/walkcode@v$old_ver' --force"
  echo "    launchctl kickstart -k gui/$UID_NUM/$LABEL_CLAUDE"
  echo "    launchctl kickstart -k gui/$UID_NUM/$LABEL_CODEX"
  exit 1
fi
