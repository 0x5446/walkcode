#!/usr/bin/env bash
set -euo pipefail

# WalkCode V3 channel-native installer.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash

REPO="0x5446/walkcode"
GITHUB_URL="https://github.com/${REPO}.git"
CONFIG_DIR="${WALKCODE_DIR:-$HOME/.walkcode}"
ENV_FILE="${WALKCODE_ENV_FILE:-$CONFIG_DIR/work-claude.env}"
PYTHON_SPEC="${WALKCODE_PYTHON:-3.13}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[walkcode]${NC} $*"; }
warn()  { echo -e "${YELLOW}[walkcode]${NC} $*"; }
error() { echo -e "${RED}[walkcode]${NC} $*" >&2; }
die()   { error "$1"; exit 1; }

is_zh() {
  case "${LANG:-}${LANGUAGE:-}" in zh*) return 0 ;; esac
  return 1
}

msg() {
  if is_zh; then echo "$2"; else echo "$1"; fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  info "$(msg "Installing uv..." "正在安装 uv...")"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
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

install_package() {
  local tag source
  tag="$(latest_tag)"
  if [ -n "$tag" ]; then
    source="walkcode @ git+${GITHUB_URL}@${tag}"
    info "$(msg "Installing WalkCode ${tag}..." "正在安装 WalkCode ${tag}...")"
  else
    source="walkcode @ git+${GITHUB_URL}"
    warn "$(msg "No release tag found; installing from main." "未找到 release tag；从 main 安装。")"
  fi
  uv tool install --python "$PYTHON_SPEC" --with claude-agent-sdk --with lark-oapi "$source" \
    --force --reinstall --refresh-package walkcode
}

write_env_template() {
  mkdir -p "$CONFIG_DIR/workspace"
  if [ -f "$ENV_FILE" ]; then
    info "$(msg "$ENV_FILE already exists; leaving it unchanged." "$ENV_FILE 已存在；保持不变。")"
    return
  fi

  cat > "$ENV_FILE" <<'ENVFILE'
# WalkCode V3 channel-native runtime
#
# One env file configures one runtime instance:
#   1 runtime = 1 profile = 1 channel = 1 bot/app identity = 1 coding agent
#
# Standard deployment is four Lark/Feishu instances:
#   {work, personal} x {claude, codex}  (see docs/lark-profile-deploy.md)

WALKCODE_PROFILE=work
WALKCODE_CHANNEL=lark
WALKCODE_AGENT=claude
# WALKCODE_AGENT=codex

LARK_APP_ID=
LARK_APP_SECRET=
# Company Feishu tenant vs personal Lark tenant.
LARK_OPENAPI_DOMAIN=https://open.feishu.cn
# LARK_OPENAPI_DOMAIN=https://open.larksuite.com

# Recommended before real use.
# LARK_ALLOWED_CHAT_IDS=
# LARK_ALLOWED_OPEN_IDS=

WALKCODE_CWD=~/.walkcode/workspace
# Enable /repo <dir> <task> inside this allowlist (colon-separated).
# WALKCODE_WORKSPACE_ROOTS=

# Per-profile agent isolation.
# WALKCODE_CLAUDE_CONFIG_DIR=~/.claude-profiles/work
# WALKCODE_CODEX_HOME=~/.codex-profiles/work

# Telegram peer channel (architecture-validation only) uses a separate env:
# WALKCODE_CHANNEL=telegram
# TELEGRAM_BOT_TOKEN=
ENVFILE

  warn "$(msg \
    "$ENV_FILE created. Fill LARK_APP_ID/LARK_APP_SECRET and WALKCODE_AGENT before starting." \
    "$ENV_FILE 已创建。启动前请填写 LARK_APP_ID/LARK_APP_SECRET 和 WALKCODE_AGENT。")"
}

detect_legacy_remnants() {
  local found=0
  local wrapper_path="$HOME/.agent-control-plane/agent-wrappers.sh"
  local wrapper_is_legacy=0

  if ls "$HOME"/Library/LaunchAgents/com.walkcode*.plist >/dev/null 2>&1; then
    for plist in "$HOME"/Library/LaunchAgents/com.walkcode*.plist; do
      if grep -Eq 'walkcode([[:space:]]|</string>[[:space:]]*<string>)(serve|start)' "$plist" 2>/dev/null; then
        warn "$(msg \
          "Legacy LaunchAgent detected: $plist. Unload it before sharing a bot with V3." \
          "检测到旧版 LaunchAgent: ${plist}。和 V3 共用机器人前请先卸载。")"
        found=1
      fi
    done
  fi

  for hook_file in "$HOME/.claude/settings.json" "$HOME/.codex/hooks.json"; do
    if [ -f "$hook_file" ] && grep -q 'walkcode hook' "$hook_file" 2>/dev/null; then
      warn "$(msg \
        "Legacy hook config detected: $hook_file. Replace it with walkcode native hook for V3 TUI observation." \
        "检测到旧版 hook 配置: ${hook_file}。V3 TUI 观测应改为 walkcode native hook。")"
      found=1
    fi
  done

  if [ -f "$wrapper_path" ] && grep -Eq 'tmux|walkcode[[:space:]]+(hook|serve|start|status|test-inject)|WALKCODE_PORT|WALKCODE_INSTANCE|\.walkcode/codex\.env|FEISHU_' "$wrapper_path" 2>/dev/null; then
    wrapper_is_legacy=1
    warn "$(msg \
      "Legacy shell wrapper behavior detected at ~/.agent-control-plane/agent-wrappers.sh." \
      "检测到 ~/.agent-control-plane/agent-wrappers.sh 里仍有旧版 wrapper 行为。")"
    found=1
  fi

  for shell_rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bashrc"; do
    if [ "$wrapper_is_legacy" -eq 1 ] && grep -q 'agent-wrappers.sh' "$shell_rc" 2>/dev/null; then
      warn "$(msg \
        "$shell_rc sources a legacy agent-wrappers.sh. Replace it with the V3 pass-through helper." \
        "$shell_rc 引用了旧版 agent-wrappers.sh。请替换为 V3 纯转发 helper。")"
      found=1
    fi
  done

  if ls "$CONFIG_DIR"/*.env >/dev/null 2>&1; then
    for env_path in "$CONFIG_DIR"/*.env; do
      if grep -q 'FEISHU_' "$env_path" 2>/dev/null; then
        warn "$(msg \
          "Legacy FEISHU_* env detected: $env_path. V3 uses TELEGRAM_* or LARK_* and a dedicated state path." \
          "检测到旧版 FEISHU_* env: ${env_path}。V3 使用 TELEGRAM_* 或 LARK_*，且需要独立 state path。")"
        found=1
      fi
    done
  fi

  if env | grep -q '^FEISHU_'; then
    warn "$(msg \
      "Current shell exports FEISHU_* variables. Start V3 from a clean Telegram/Lark env file." \
      "当前 shell 导出了 FEISHU_* 变量。V3 应从干净的 Telegram/Lark env 文件启动。")"
    found=1
  fi

  return "$found"
}

main() {
  echo ""
  echo "  WalkCode V3"
  echo "  Channel-native coding agent runtime"
  echo ""

  if detect_legacy_remnants; then
    info "$(msg "No legacy runtime remnants detected." "未检测到旧版 runtime 残留。")"
  else
    die "$(msg \
      "Legacy remnants were reported above. Clean them before installing the V3 runtime." \
      "上面列出了旧版残留。安装 V3 runtime 前需要先清理。")"
  fi

  ensure_uv
  install_package
  write_env_template

  echo ""
  info "$(msg "Installation complete." "安装完成。")"
  if is_zh; then
    echo "  后续步骤:"
    echo "  1. 编辑 ${ENV_FILE}，填写 TELEGRAM_BOT_TOKEN 和 WALKCODE_AGENT"
    echo "  2. 运行: WALKCODE_ENV_FILE=$ENV_FILE walkcode native doctor"
    echo "  3. 如果在仓库 checkout 中，运行模块检查: python scripts/channel_native_debug.py --env-file $ENV_FILE runtime"
    echo "  4. 启动: WALKCODE_ENV_FILE=$ENV_FILE walkcode native serve"
  else
    echo "  Next steps:"
    echo "  1. Edit $ENV_FILE with TELEGRAM_BOT_TOKEN and WALKCODE_AGENT"
    echo "  2. Run: WALKCODE_ENV_FILE=$ENV_FILE walkcode native doctor"
    echo "  3. From a repository checkout, run module checks: python scripts/channel_native_debug.py --env-file $ENV_FILE runtime"
    echo "  4. Start: WALKCODE_ENV_FILE=$ENV_FILE walkcode native serve"
  fi
  echo ""
}

main "$@"
