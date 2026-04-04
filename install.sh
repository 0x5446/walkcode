#!/usr/bin/env bash
set -euo pipefail

# WalkCode one-click installer
# Usage: curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash

REPO="0x5446/walkcode"
GITHUB_URL="https://github.com/${REPO}.git"
CONFIG_DIR="${WALKCODE_DIR:-$HOME/.walkcode}"
SHELL_RC=""

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[walkcode]${NC} $*"; }
warn()  { echo -e "${YELLOW}[walkcode]${NC} $*"; }
error() { echo -e "${RED}[walkcode]${NC} $*" >&2; }

# --- i18n ---
is_zh() {
  case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac
  return 1
}
# msg "English text" "中文文本"
msg() { if is_zh; then echo "$2"; else echo "$1"; fi; }

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

  if ! command -v tmux &>/dev/null; then
    if command -v brew &>/dev/null; then
      info "$(msg "Installing tmux via Homebrew..." "正在通过 Homebrew 安装 tmux...")"
      brew install tmux
    else
      missing+=("tmux (brew install tmux)")
    fi
  fi

  if ! command -v uv &>/dev/null; then
    info "$(msg "Installing uv..." "正在安装 uv...")"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  fi

  if [ ${#missing[@]} -gt 0 ]; then
    error "$(msg "Missing prerequisites: ${missing[*]}" "缺少前置依赖: ${missing[*]}")"
    exit 1
  fi
}

# --- Get latest release tag ---
get_latest_tag() {
  local tag
  tag=$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" 2>/dev/null \
        | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/') || true
  echo "$tag"
}

# --- Install Python package via uv tool ---
install_package() {
  local tag
  tag=$(get_latest_tag)

  if [ -n "$tag" ]; then
    info "$(msg "Installing WalkCode ${tag}..." "正在安装 WalkCode ${tag}...")"
    uv tool install "git+${GITHUB_URL}@${tag}" --force 2>/dev/null \
      || uv tool install "git+${GITHUB_URL}@${tag}"
  else
    info "$(msg "No releases found, installing from main branch..." "未找到正式版本，从 main 分支安装...")"
    uv tool install "git+${GITHUB_URL}" --force 2>/dev/null \
      || uv tool install "git+${GITHUB_URL}"
  fi
}

# --- Setup config directory and .env ---
setup_config() {
  mkdir -p "$CONFIG_DIR/workspace"

  if [ ! -f "$CONFIG_DIR/.env" ]; then
    cat > "$CONFIG_DIR/.env" << 'ENVFILE'
# WalkCode Configuration
# See: https://github.com/0x5446/walkcode

# Feishu App credentials (required)
FEISHU_APP_ID=
FEISHU_APP_SECRET=

# Who receives notifications
# Use open_id for direct messages, or chat_id for group chats
# Run "walkcode serve" to discover your open_id
FEISHU_RECEIVE_ID=
FEISHU_RECEIVE_ID_TYPE=open_id

# Server port (optional, default 3001)
# PORT=3001
ENVFILE
    warn "$(msg \
      ".env created — edit $CONFIG_DIR/.env with your Feishu credentials" \
      ".env 已创建 — 请编辑 $CONFIG_DIR/.env 填入你的飞书凭证")"
  else
    info "$(msg ".env already exists, skipping" ".env 已存在，跳过")"
  fi
}

# --- Install shell wrapper ---
install_wrapper() {
  if ! command -v claude &>/dev/null; then
    warn "$(msg \
      "Claude Code CLI not found. Install it first: https://docs.anthropic.com/en/docs/claude-code" \
      "未找到 Claude Code CLI，请先安装: https://docs.anthropic.com/en/docs/claude-code")"
    warn "$(msg "Skipping shell wrapper installation." "跳过 shell wrapper 安装。")"
    return
  fi

  local marker="# >>> walkcode claude wrapper >>>"
  if grep -q "$marker" "$SHELL_RC" 2>/dev/null; then
    info "$(msg "Shell wrapper already installed in $SHELL_RC" "Shell wrapper 已安装在 $SHELL_RC 中")"
    return
  fi

  info "$(msg "Adding claude wrapper to $SHELL_RC..." "正在将 claude wrapper 添加到 $SHELL_RC...")"
  cat >> "$SHELL_RC" << 'WRAPPER'

# >>> walkcode claude wrapper >>>
claude() {
  if [ -z "$TMUX" ]; then
    # non-interactive flags: run directly without tmux
    case "$1" in
      --version|-v|--help|-h|-p|--print)
        command claude "$@"
        return
        ;;
    esac
    local session="claude-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command claude $(printf '%q ' "$@")"
  else
    command claude "$@"
  fi
}
# <<< walkcode claude wrapper <<<
WRAPPER

  info "$(msg "Shell wrapper installed. Run: source $SHELL_RC" "Shell wrapper 已安装。请执行: source $SHELL_RC")"
}

# --- Configure tmux ---
configure_tmux() {
  local tmux_conf="$HOME/.tmux.conf"
  local marker_start="# >>> walkcode tmux config >>>"
  local marker_end="# <<< walkcode tmux config <<<"

  # Remove old walkcode config block if present (handles upgrade)
  if [ -f "$tmux_conf" ] && grep -q "$marker_start" "$tmux_conf" 2>/dev/null; then
    info "$(msg "Removing old WalkCode tmux config..." "正在移除旧的 WalkCode tmux 配置...")"
    sed -i.walkcode-bak "/$marker_start/,/$marker_end/d" "$tmux_conf"
    rm -f "${tmux_conf}.walkcode-bak"
  fi

  info "$(msg "Adding tmux config to $tmux_conf..." "正在将 tmux 配置添加到 $tmux_conf...")"
  cat >> "$tmux_conf" << 'TMUXCFG'

# >>> walkcode tmux config >>>
# Increase scrollback buffer for Claude Code sessions
set-option -g history-limit 50000
# Enable mouse — scroll events pass through to Claude Code in alternate screen
set-option -g mouse on
# Drag end: copy to clipboard, keep highlight, mark selection active
bind-key -T copy-mode MouseDragEnd1Pane send-keys -X copy-pipe-no-clear "pbcopy" \; set -p @has_selection 1
bind-key -T copy-mode-vi MouseDragEnd1Pane send-keys -X copy-pipe-no-clear "pbcopy" \; set -p @has_selection 1
# Click: has selection → exit copy mode; no selection → position cursor for new drag
bind-key -T copy-mode MouseDown1Pane if-shell -F "#{@has_selection}" "set -p @has_selection 0 ; send-keys -X cancel" "select-pane ; send-keys -X clear-selection"
bind-key -T copy-mode-vi MouseDown1Pane if-shell -F "#{@has_selection}" "set -p @has_selection 0 ; send-keys -X cancel" "select-pane ; send-keys -X clear-selection"
# Scroll: clear selection to prevent extension, stay in copy mode
bind-key -T copy-mode WheelUpPane set -p @has_selection 0 \; send-keys -X clear-selection \; send-keys -X -N 5 scroll-up
bind-key -T copy-mode WheelDownPane set -p @has_selection 0 \; send-keys -X clear-selection \; send-keys -X -N 5 scroll-down
bind-key -T copy-mode-vi WheelUpPane set -p @has_selection 0 \; send-keys -X clear-selection \; send-keys -X -N 5 scroll-up
bind-key -T copy-mode-vi WheelDownPane set -p @has_selection 0 \; send-keys -X clear-selection \; send-keys -X -N 5 scroll-down
# Selection highlight style
set-option -g mode-style "bg=colour240,fg=white"
# <<< walkcode tmux config <<<
TMUXCFG

  # Hot-reload if tmux server is running
  if tmux list-sessions &>/dev/null 2>&1; then
    tmux set-option -g history-limit 50000 2>/dev/null || true
    tmux set-option -g mouse on 2>/dev/null || true
    # Undo old smcup@:rmcup@ overrides — reset terminal-overrides then re-source
    tmux set-option -gu terminal-overrides 2>/dev/null || true
    tmux source-file "$tmux_conf" 2>/dev/null || true
  fi

  # Warn if smcup@:rmcup@ still exists outside walkcode markers
  if grep -q 'smcup@:rmcup@' "$tmux_conf" 2>/dev/null; then
    warn "$(msg \
      "Found smcup@:rmcup@ in $tmux_conf outside WalkCode markers. This disables alternate screen and causes scrollback corruption with TUI apps. Please remove it manually." \
      "在 $tmux_conf 中发现 WalkCode 标记之外的 smcup@:rmcup@ 配置。这会禁用备用屏幕导致 TUI 应用滚动异常，请手动删除。")"
  fi

  info "$(msg "tmux config installed" "tmux 配置已安装")"
}

# --- Install Claude Code hooks ---
install_hooks() {
  local settings="$HOME/.claude/settings.json"
  if [ ! -f "$settings" ]; then
    warn "$(msg \
      "$settings not found — skipping hook installation" \
      "$settings 未找到 — 跳过 hook 安装")"
    warn "$(msg \
      "Run 'walkcode install-hooks' after Claude Code is set up" \
      "请在 Claude Code 设置好后执行 'walkcode install-hooks'")"
    return
  fi

  info "$(msg "Installing Claude Code hooks..." "正在安装 Claude Code hooks...")"
  walkcode install-hooks
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
  detect_shell_rc
  install_package
  setup_config
  install_wrapper
  configure_tmux
  install_hooks

  # Restart daemon if already running (upgrade scenario)
  if command -v walkcode &>/dev/null && walkcode status &>/dev/null; then
    info "$(msg "Restarting WalkCode daemon..." "正在重启 WalkCode 守护进程...")"
    walkcode restart
  fi

  echo ""
  info "$(msg "Installation complete!" "安装完成！")"
  echo ""
  if is_zh; then
    echo "  后续步骤:"
    echo "  1. 编辑 $CONFIG_DIR/.env 填入飞书凭证 (APP_ID & APP_SECRET)"
    echo "  2. source $SHELL_RC"
    echo "  3. walkcode serve"
    echo "  4. 向飞书机器人发送一条消息 — open_id 会显示在控制台"
    echo "  5. 将 open_id 添加到 .env，然后执行: walkcode restart"
  else
    echo "  Next steps:"
    echo "  1. Edit $CONFIG_DIR/.env with your Feishu credentials (APP_ID & APP_SECRET)"
    echo "  2. source $SHELL_RC"
    echo "  3. walkcode serve"
    echo "  4. Send a message to your Feishu bot — open_id will be printed"
    echo "  5. Add open_id to .env, then run: walkcode restart"
  fi
  echo ""
  if is_zh; then
    echo "  建议: 在接通电源时禁止 macOS 休眠以保持网络连接"
    echo "  (屏幕仍会关闭):"
  else
    echo "  Recommended: prevent macOS from sleeping on AC power so the network"
    echo "  stays up while you're away (display can still turn off):"
  fi
  echo ""
  echo "    sudo pmset -c sleep 0 && sudo pmset -c disksleep 0 \\"
  echo "         && sudo pmset -c standby 0 && sudo pmset -c hibernatemode 0"
  echo ""
}

main "$@"
