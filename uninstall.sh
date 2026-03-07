#!/usr/bin/env bash
set -euo pipefail

# WalkCode one-click uninstaller
# Usage: bash uninstall.sh
#   or:  curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/uninstall.sh | bash

INSTALL_DIR="${WALKCODE_DIR:-$HOME/walkcode}"
RUNTIME_DIR="$HOME/.walkcode"

# All candidate shell rc files (same strategy as rustup/nvm/uv)
RC_CANDIDATES=(
  "$HOME/.zshrc"
  "$HOME/.zshenv"
  "$HOME/.zprofile"
  "$HOME/.bashrc"
  "$HOME/.bash_profile"
  "$HOME/.bash_login"
  "$HOME/.profile"
)

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[walkcode]${NC} $*"; }
warn()  { echo -e "${YELLOW}[walkcode]${NC} $*"; }
error() { echo -e "${RED}[walkcode]${NC} $*" >&2; }

# --- Stop daemon if running ---
stop_daemon() {
  local pid_file="$RUNTIME_DIR/walkcode.pid"
  if [ -f "$pid_file" ]; then
    local pid
    pid=$(cat "$pid_file" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      info "Stopping WalkCode daemon (pid $pid)..."
      kill "$pid" 2>/dev/null || true
      sleep 1
      kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
      info "Daemon stopped"
    fi
    rm -f "$pid_file"
  fi
}

# --- Remove shell wrapper from ALL candidate rc files ---
remove_shell_wrapper() {
  local marker_start="# >>> walkcode claude wrapper >>>"
  local marker_end="# <<< walkcode claude wrapper <<<"
  local found=0

  for rc in "${RC_CANDIDATES[@]}"; do
    [ -f "$rc" ] || continue
    if grep -q "$marker_start" "$rc" 2>/dev/null; then
      info "Removing shell wrapper from $rc..."
      sed -i.walkcode-bak "/$marker_start/,/$marker_end/d" "$rc"
      rm -f "${rc}.walkcode-bak"
      found=1
    fi
  done

  if [ "$found" -eq 0 ]; then
    info "No shell wrapper found in any shell rc file, skipping"
  fi
}

# --- Remove tmux config ---
remove_tmux_config() {
  local tmux_conf="$HOME/.tmux.conf"
  local marker_start="# >>> walkcode tmux config >>>"
  local marker_end="# <<< walkcode tmux config <<<"

  if [ -f "$tmux_conf" ] && grep -q "$marker_start" "$tmux_conf" 2>/dev/null; then
    info "Removing tmux config from $tmux_conf..."
    sed -i.walkcode-bak "/$marker_start/,/$marker_end/d" "$tmux_conf"
    rm -f "${tmux_conf}.walkcode-bak"
    # Remove file if empty (only whitespace left)
    if [ ! -s "$tmux_conf" ] || ! grep -q '[^[:space:]]' "$tmux_conf" 2>/dev/null; then
      rm -f "$tmux_conf"
      info "Removed empty $tmux_conf"
    fi
    tmux source-file "$tmux_conf" 2>/dev/null || true
  else
    info "No WalkCode tmux config found, skipping"
  fi
}

# --- Remove Claude Code hooks ---
remove_hooks() {
  local settings="$HOME/.claude/settings.json"
  if [ ! -f "$settings" ]; then
    info "No Claude Code settings found, skipping hooks removal"
    return
  fi

  if ! command -v python3 &>/dev/null; then
    warn "python3 not found, cannot auto-remove hooks from $settings"
    warn "Please manually remove the \"hooks\" section from $settings"
    return
  fi

  # Only remove hooks that contain "walkcode" commands
  if grep -q "walkcode" "$settings" 2>/dev/null; then
    info "Removing WalkCode hooks from $settings..."
    python3 -c "
import json, sys
path = '$settings'
with open(path) as f:
    data = json.load(f)
hooks = data.get('hooks', {})
changed = False
for event in list(hooks.keys()):
    entries = hooks[event]
    filtered = []
    for entry in entries:
        cmds = entry.get('hooks', [])
        cmds = [c for c in cmds if 'walkcode' not in c.get('command', '')]
        if cmds:
            entry['hooks'] = cmds
            filtered.append(entry)
    if filtered:
        hooks[event] = filtered
    else:
        del hooks[event]
        changed = True
if not hooks and 'hooks' in data:
    del data['hooks']
    changed = True
if changed or hooks != data.get('hooks'):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    print('Hooks removed')
else:
    print('No WalkCode hooks found')
"
  else
    info "No WalkCode hooks found in $settings, skipping"
  fi
}

# --- Remove runtime directory ---
remove_runtime() {
  if [ -d "$RUNTIME_DIR" ]; then
    info "Removing runtime directory $RUNTIME_DIR..."
    rm -rf "$RUNTIME_DIR"
    info "Runtime directory removed"
  fi
}

# --- Remove install directory ---
remove_install_dir() {
  if [ -d "$INSTALL_DIR" ]; then
    info "Removing install directory $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
    info "Install directory removed"
  else
    info "Install directory $INSTALL_DIR not found, skipping"
  fi
}

# --- Main ---
main() {
  echo ""
  echo "  ╦ ╦╔═╗╦  ╦╔═╔═╗╔═╗╔╦╗╔═╗"
  echo "  ║║║╠═╣║  ╠╩╗║  ║ ║ ║║║╣ "
  echo "  ╚╩╝╩ ╩╩═╝╩ ╩╚═╝╚═╝═╩╝╚═╝"
  echo "  Uninstaller"
  echo ""

  echo "This will remove:"
  echo "  1. WalkCode daemon (if running)"
  echo "  2. Shell wrapper from all rc files (.zshrc, .bashrc, .profile, etc.)"
  echo "  3. tmux config from ~/.tmux.conf"
  echo "  4. Claude Code hooks from ~/.claude/settings.json"
  echo "  5. Runtime directory ($RUNTIME_DIR)"
  echo "  6. Install directory ($INSTALL_DIR)"
  echo ""
  printf "Continue? [y/N] "
  read -r answer
  if [ "$answer" != "y" ] && [ "$answer" != "Y" ]; then
    echo "Aborted."
    exit 0
  fi

  echo ""
  stop_daemon
  remove_shell_wrapper
  remove_tmux_config
  remove_hooks
  remove_runtime
  remove_install_dir

  echo ""
  info "WalkCode has been completely removed."
  echo ""
  echo "  Restart your shell or run 'exec \$SHELL' to apply changes."
  echo ""
}

main "$@"
