#!/usr/bin/env bash
set -euo pipefail

# WalkCode one-click installer
# Usage: curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash

REPO="https://github.com/0x5446/walkcode.git"
INSTALL_DIR="${WALKCODE_DIR:-$HOME/walkcode}"
SHELL_RC=""

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[walkcode]${NC} $*"; }
warn()  { echo -e "${YELLOW}[walkcode]${NC} $*"; }
error() { echo -e "${RED}[walkcode]${NC} $*" >&2; }

# --- Detect shell rc file ---
detect_shell_rc() {
  if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "zsh" ]; then
    SHELL_RC="$HOME/.zshrc"
  elif [ -n "${BASH_VERSION:-}" ] || [ "$(basename "$SHELL")" = "bash" ]; then
    SHELL_RC="$HOME/.bashrc"
  else
    SHELL_RC="$HOME/.profile"
  fi
}

# --- Check prerequisites ---
check_prereqs() {
  local missing=()

  if ! command -v git &>/dev/null; then
    missing+=("git")
  fi

  if ! command -v tmux &>/dev/null; then
    if command -v brew &>/dev/null; then
      info "Installing tmux via Homebrew..."
      brew install tmux
    else
      missing+=("tmux (brew install tmux)")
    fi
  fi

  if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    error "Missing prerequisites: ${missing[*]}"
    exit 1
  fi
}

# --- Clone or update repo ---
clone_or_update() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only
  else
    info "Cloning WalkCode..."
    git clone "$REPO" "$INSTALL_DIR"
  fi
}

# --- Install Python package ---
install_package() {
  info "Installing dependencies..."
  cd "$INSTALL_DIR"
  uv sync
}

# --- Setup .env ---
setup_env() {
  if [ ! -f "$INSTALL_DIR/.env" ]; then
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    warn ".env created from template — edit $INSTALL_DIR/.env with your Feishu credentials"
  else
    info ".env already exists, skipping"
  fi
}

# --- Install shell wrapper ---
install_wrapper() {
  detect_shell_rc

  local marker="# >>> walkcode claude wrapper >>>"
  if grep -q "$marker" "$SHELL_RC" 2>/dev/null; then
    info "Shell wrapper already installed in $SHELL_RC"
    return
  fi

  info "Adding claude wrapper to $SHELL_RC..."
  cat >> "$SHELL_RC" << 'WRAPPER'

# >>> walkcode claude wrapper >>>
claude() {
  if [ -z "$TMUX" ]; then
    local session="claude-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command claude $@"
  else
    command claude "$@"
  fi
}
# <<< walkcode claude wrapper <<<
WRAPPER

  info "Shell wrapper installed. Run: source $SHELL_RC"
}

# --- Configure tmux ---
configure_tmux() {
  local tmux_conf="$HOME/.tmux.conf"
  local marker="# >>> walkcode tmux config >>>"

  if grep -q "$marker" "$tmux_conf" 2>/dev/null; then
    info "tmux config already present in $tmux_conf"
    return
  fi

  info "Adding tmux scrollback config to $tmux_conf..."
  cat >> "$tmux_conf" << 'TMUXCFG'

# >>> walkcode tmux config >>>
# Disable alternate screen so TUI output (e.g. Claude Code) stays in scrollback
# Use Ctrl-b [ to scroll back through history
set-option -ga terminal-overrides ',*:smcup@:rmcup@'
# <<< walkcode tmux config <<<
TMUXCFG

  # Hot-reload if tmux server is running
  tmux source-file "$tmux_conf" 2>/dev/null || true
  info "tmux config installed"
}

# --- Install Claude Code hooks ---
install_hooks() {
  local settings="$HOME/.claude/settings.json"
  if [ ! -f "$settings" ]; then
    warn "$settings not found — skipping hook installation"
    warn "Run 'uv run walkcode install-hooks' after Claude Code is set up"
    return
  fi

  info "Installing Claude Code hooks..."
  cd "$INSTALL_DIR"
  uv run walkcode install-hooks
}

# --- Main ---
main() {
  echo ""
  echo "  ╦ ╦╔═╗╦  ╦╔═╔═╗╔═╗╔╦╗╔═╗"
  echo "  ║║║╠═╣║  ╠╩╗║  ║ ║ ║║║╣ "
  echo "  ╚╩╝╩ ╩╩═╝╩ ╩╚═╝╚═╝═╩╝╚═╝"
  echo "  Code is cheap. Show me your talk."
  echo ""

  check_prereqs
  clone_or_update
  install_package
  setup_env
  install_wrapper
  configure_tmux
  install_hooks

  echo ""
  info "Installation complete!"
  echo ""
  echo "  Next steps:"
  echo "  1. Edit $INSTALL_DIR/.env with your Feishu credentials"
  echo "  2. source $SHELL_RC"
  echo "  3. cd $INSTALL_DIR && uv run walkcode serve"
  echo "  4. Send a message to your Feishu bot to get your open_id"
  echo "  5. Add open_id to .env, restart, and go for a walk"
  echo ""
}

main "$@"
