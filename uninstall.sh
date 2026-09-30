#!/usr/bin/env bash
set -euo pipefail

# WalkCode V3 uninstaller.
#
# Removes, in order:
#   1. loaded/installed com.walkcode.* LaunchAgents (bootout + plist) —
#      com.walkcode.tap-* debug proxies are never touched: they carry live
#      Claude API traffic;
#   2. the `walkcode` uv tool;
#   3. WalkCode hook entries (`walkcode native hook`, legacy `walkcode hook`)
#      from Claude settings.json and codex hooks.json files — other hooks in
#      the same files stay, and every modified file is backed up first.
#
# It never deletes ~/.walkcode wholesale: env files, backups and the default
# workspace (WALKCODE_CWD=~/.walkcode/workspace — user code) are always kept.
# State/log files are only removed after an interactive "y" (default No;
# skipped when there is no terminal).
#
# Usage:
#   ./uninstall.sh [--dry-run] [--yes]
#     --dry-run  print what would be done, change nothing
#     --yes      skip the initial confirmation (the state/log prompt still
#                needs a terminal and still defaults to No)

DRY_RUN=false
ASSUME_YES=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --yes|-y) ASSUME_YES=true ;;
    *) echo "usage: $0 [--dry-run] [--yes]" >&2; exit 2 ;;
  esac
done

INSTALL_DIR="${WALKCODE_DIR:-$HOME/.walkcode}"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
UID_NUM="$(id -u)"
STAMP="$(date +%Y%m%d-%H%M%S)"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[walkcode]${NC} $*"; }
warn()  { echo -e "${YELLOW}[walkcode]${NC} $*"; }
error() { echo -e "${RED}[walkcode]${NC} $*" >&2; }
is_zh() { case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac; return 1; }
msg()   { if is_zh; then echo "$2"; else echo "$1"; fi; }
run()   { if $DRY_RUN; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

# Succeeds only with a controlling terminal (curl | bash keeps one; launchd,
# CI and test subprocesses started in a new session do not).
has_tty() { { : </dev/tty; } 2>/dev/null; }

ask_yes() {
  # $1 = prompt. Default No; no terminal → No.
  local answer=""
  has_tty || return 1
  printf '%s' "$1" >/dev/tty
  read -r answer </dev/tty || return 1
  [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

# --- 1. LaunchAgents ---------------------------------------------------------

walkcode_labels() {
  # Loaded labels plus installed-but-unloaded plists (they would load again
  # at next login). tap-* excluded — same rule as upgrade.sh.
  {
    LC_ALL=C launchctl list 2>/dev/null | LC_ALL=C awk '{print $NF}' || true
    for plist in "$LAUNCH_AGENTS"/com.walkcode.*.plist; do
      [ -e "$plist" ] || continue
      basename "$plist" .plist
    done
  } | LC_ALL=C grep -E '^com\.walkcode\.' | LC_ALL=C grep -v '^com\.walkcode\.tap-' | LC_ALL=C sort -u || true
}

# Set when this script runs inside a session a WalkCode runtime drives
# (exported by `walkcode native serve`); booting that runtime out kills us,
# so it goes last (ADR 0058).
SELF_LABEL="${WALKCODE_DRIVER_LABEL:-}"
DEFERRED_SELF_LABEL=""

remove_launch_agents() {
  local label found=0
  while IFS= read -r label; do
    [ -n "$label" ] || continue
    found=1
    if [ -f "$LAUNCH_AGENTS/${label}.plist" ]; then
      run rm -f "$LAUNCH_AGENTS/${label}.plist"
    fi
    if [ "$label" = "$SELF_LABEL" ]; then
      DEFERRED_SELF_LABEL="$label"
      continue
    fi
    info "$(msg "Stopping LaunchAgent ${label}" "停止 LaunchAgent ${label}")"
    run launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
  done < <(walkcode_labels)
  if [ "$found" -eq 0 ]; then
    info "$(msg "No com.walkcode.* LaunchAgent found" "未发现 com.walkcode.* LaunchAgent")"
  fi
}

# --- 2. uv tool ---------------------------------------------------------------

remove_cli() {
  if ! command -v uv >/dev/null 2>&1; then
    warn "$(msg "uv not found; skipping 'uv tool uninstall walkcode'" "找不到 uv；跳过 uv tool uninstall walkcode")"
    return
  fi
  info "$(msg "Removing the walkcode uv tool" "移除 walkcode uv tool")"
  run uv tool uninstall walkcode || warn "$(msg \
    "uv tool uninstall walkcode failed (not installed?)" \
    "uv tool uninstall walkcode 失败（可能未安装）")"
}

# --- 3. hooks -----------------------------------------------------------------

hook_files() {
  local f
  for f in "$HOME/.claude/settings.json" \
           "$HOME"/.claude-profiles/*/settings.json \
           "$HOME"/.codex*/hooks.json \
           "$HOME"/.codex-profiles/*/hooks.json; do
    [ -f "$f" ] && printf '%s\n' "$f"
  done
  return 0
}

# Prints one status line per file: "changed <n>", "clean", or "error <why>".
# Only hook commands matching `walkcode native hook` / `walkcode hook` are
# dropped; an entry is dropped only when that left it empty, an event only
# when all its entries went. The file is backed up before it is rewritten.
HOOK_FILTER_PY='
import json, re, shutil, sys
path, mode, backup = sys.argv[1:4]
pat = re.compile(r"\bwalkcode\s+(native\s+)?hook\b")
try:
    with open(path) as f:
        data = json.load(f)
except Exception as exc:
    print("error", type(exc).__name__)
    sys.exit(0)
hooks = data.get("hooks") if isinstance(data, dict) else None
removed = 0
if isinstance(hooks, dict):
    for event in list(hooks):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        kept_entries = []
        event_removed = 0
        for entry in entries:
            cmds = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(cmds, list):
                kept_entries.append(entry)
                continue
            kept = [c for c in cmds
                    if not (isinstance(c, dict) and pat.search(str(c.get("command", ""))))]
            event_removed += len(cmds) - len(kept)
            if kept or not cmds:
                entry["hooks"] = kept
                kept_entries.append(entry)
        if event_removed:
            removed += event_removed
            if kept_entries:
                hooks[event] = kept_entries
            else:
                del hooks[event]
    if removed and not hooks:
        del data["hooks"]
if not removed:
    print("clean")
    sys.exit(0)
if mode == "apply":
    shutil.copy2(path, backup)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
print("changed", removed)
'

remove_hooks() {
  local file result mode backup any=0
  if ! command -v python3 >/dev/null 2>&1; then
    warn "$(msg \
      "python3 not found; remove 'walkcode native hook' entries from Claude settings.json / codex hooks.json by hand" \
      "找不到 python3；请手动删除 Claude settings.json / codex hooks.json 里的 walkcode native hook 条目")"
    return
  fi
  mode=apply
  $DRY_RUN && mode=dry
  while IFS= read -r file; do
    grep -q 'walkcode' "$file" 2>/dev/null || continue
    backup="${file}.walkcode-uninstall-${STAMP}.bak"
    result="$(python3 -c "$HOOK_FILTER_PY" "$file" "$mode" "$backup")"
    case "$result" in
      changed*)
        any=1
        if $DRY_RUN; then
          printf '  [dry-run] remove %s WalkCode hook(s) from %s (backup %s)\n' "${result#changed }" "$file" "$backup"
        else
          info "$(msg \
            "Removed ${result#changed } WalkCode hook(s) from ${file} (backup: ${backup})" \
            "已从 ${file} 移除 ${result#changed } 个 WalkCode hook（备份: ${backup}）")"
        fi
        ;;
      error*)
        warn "$(msg "Could not parse ${file} (${result#error }); left untouched" "无法解析 ${file}（${result#error }）；未改动")"
        ;;
    esac
  done < <(hook_files)
  if [ "$any" -eq 0 ]; then
    info "$(msg "No WalkCode hooks found" "未发现 WalkCode hook")"
  fi
}

# --- 4. data (opt-in, state/logs only) ------------------------------------------

state_paths() {
  local p
  for p in "$INSTALL_DIR"/*-state.json "$INSTALL_DIR"/*-state.json.*.d \
           "$INSTALL_DIR"/.*-state.json.*.tmp "$INSTALL_DIR/logs"; do
    [ -e "$p" ] && printf '%s\n' "$p"
  done
  return 0
}

handle_data() {
  local -a paths=()
  local p
  [ -d "$INSTALL_DIR" ] || return 0
  while IFS= read -r p; do
    [ -n "$p" ] && paths+=("$p")
  done < <(state_paths)

  if [ "${#paths[@]}" -gt 0 ]; then
    if $DRY_RUN; then
      printf '  [dry-run] would ask before deleting state/log files:\n'
      printf '    %s\n' "${paths[@]}"
    elif ask_yes "$(msg \
        "Delete WalkCode state/log files (${#paths[@]} paths under ${INSTALL_DIR})? [y/N] " \
        "删除 WalkCode 状态/日志文件（${INSTALL_DIR} 下 ${#paths[@]} 项）？[y/N] ")"; then
      for p in "${paths[@]}"; do
        rm -rf -- "$p"
      done
      info "$(msg "State/log files deleted" "状态/日志文件已删除")"
    else
      info "$(msg "State/log files kept" "状态/日志文件已保留")"
    fi
  fi

  info "$(msg \
    "Kept ${INSTALL_DIR}: env files (bot secrets), backups and the workspace directory are never deleted by this script. Remove them by hand if you are sure." \
    "已保留 ${INSTALL_DIR}：env 文件（含机器人密钥）、备份和 workspace 目录本脚本一律不删；确认不要再手动删除。")"
}

main() {
  if is_zh; then
    echo "WalkCode V3 卸载：停止并移除 com.walkcode.* LaunchAgent（tap-* 除外）、"
    echo "卸载 walkcode uv tool、从 Claude/codex 配置中移除 WalkCode hook（先备份）。"
    echo "不会删除 ${INSTALL_DIR} 下的 env 文件和 workspace。"
  else
    echo "WalkCode V3 uninstall: stop and remove com.walkcode.* LaunchAgents (except tap-*),"
    echo "uninstall the walkcode uv tool, remove WalkCode hooks from Claude/codex config (backed up first)."
    echo "Env files and the workspace under ${INSTALL_DIR} are kept."
  fi
  if $DRY_RUN; then
    info "$(msg "Dry run: nothing will be changed" "dry-run：不做任何改动")"
  elif ! $ASSUME_YES; then
    if ! ask_yes "$(msg "Continue? [y/N] " "继续？[y/N] ")"; then
      echo "$(msg "Aborted (use --yes when there is no terminal)." "已取消（无终端时用 --yes）。")"
      exit 1
    fi
  fi

  remove_launch_agents
  remove_cli
  remove_hooks
  handle_data

  info "$(msg "WalkCode uninstall finished." "WalkCode 卸载完成。")"

  if [ -n "$DEFERRED_SELF_LABEL" ]; then
    info "$(msg \
      "Stopping ${DEFERRED_SELF_LABEL} last — it drives this session, which ends now." \
      "最后停止 ${DEFERRED_SELF_LABEL}——它驱动着当前会话，会话将随之结束。")"
    run launchctl bootout "gui/${UID_NUM}/${DEFERRED_SELF_LABEL}" 2>/dev/null || true
  fi
}

main
