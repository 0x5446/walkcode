#!/usr/bin/env bash
set -euo pipefail

# claude-tap sidecar 管理：为每个 claude profile 起一个"纯代理"claude-tap
# （launchd 常驻、开机自启、崩溃自动拉起），并把
# WALKCODE_CLAUDE_ANTHROPIC_BASE_URL 写进对应 profile 的 walkcode env。
# 完整说明见 docs/claude-tap-deploy.md；机制与取舍见 ADR 0047。
#
#   claude-tap-setup.sh init      生成 ~/.walkcode/claude-tap/taps.conf 模板
#   claude-tap-setup.sh apply     生成 plist + 起 tap + 写 env + 重启 walkcode 实例（幂等）
#   claude-tap-setup.sh remove    卸掉全部 tap + 删 env 行 + 重启实例（恢复直连）
#   claude-tap-setup.sh status    看各 tap 端口/进程状态
#
# 唯一配置源是 ~/.walkcode/claude-tap/taps.conf。改了某 profile 的
# settings.json 上游后要重跑 apply（plist 里的上游探测 env 是生成时的快照）。
# 只抽取 URL/开关类变量进 plist，绝不写入 API key（认证头由 Claude 客户端
# 自带，tap 只透传）。

CONF_DIR="$HOME/.walkcode/claude-tap"
CONF="$CONF_DIR/taps.conf"
TAP_BIN="$(command -v claude-tap || echo "$HOME/.local/bin/claude-tap")"
UID_NUM="$(id -u)"
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/.walkcode/logs"
RUN_DIR="$CONF_DIR/run"

die() { echo "$*" >&2; exit 1; }

profile_env_file() { echo "$HOME/.walkcode/$1-claude.env"; }

# profile 的 CLAUDE_CONFIG_DIR：优先读 walkcode env 文件，回退到约定路径
profile_config_dir() {
  local envf; envf="$(profile_env_file "$1")"
  local dir=""
  [ -f "$envf" ] && dir="$(sed -n 's/^WALKCODE_CLAUDE_CONFIG_DIR=//p' "$envf" | tail -1)"
  echo "${dir:-$HOME/.claude-profiles/$1}"
}

# 从 profile settings.json 的 env 块抽取上游探测所需变量（不含任何密钥）
upstream_env_pairs() {
  python3 - "$(profile_config_dir "$1")/settings.json" <<'PY'
import json, sys
KEYS = ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_BEDROCK_BASE_URL")
try:
    env = json.load(open(sys.argv[1])).get("env", {})
except Exception:
    env = {}
for k in KEYS:
    v = env.get(k)
    if isinstance(v, str) and v.strip():
        print(f"{k}\t{v.strip()}")
PY
}

write_plist() {
  local profile="$1" port="$2" target="$3" allow="$4"
  local label="com.walkcode.tap-$profile"
  local plist="$PLIST_DIR/$label.plist"

  local args="        <string>$TAP_BIN</string>
        <string>--tap-no-launch</string>
        <string>--tap-client</string><string>claude</string>
        <string>--tap-port</string><string>$port</string>
        <string>--tap-host</string><string>127.0.0.1</string>
        <string>--tap-no-open</string>
        <string>--tap-no-update-check</string>
        <string>--tap-no-auto-update</string>"
  if [ "$target" != "auto" ]; then
    args="$args
        <string>--tap-target</string><string>$target</string>"
  fi
  if [ "$allow" != "-" ]; then
    local p; for p in ${allow//,/ }; do
      args="$args
        <string>--tap-allow-path</string><string>$p</string>"
    done
  fi

  local env_xml=""
  while IFS=$'\t' read -r k v; do
    [ -n "$k" ] && env_xml="$env_xml
        <key>$k</key><string>$v</string>"
  done < <(upstream_env_pairs "$profile")

  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$label</string>
    <key>ProgramArguments</key>
    <array>
$args
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key><string>$HOME</string>$env_xml
    </dict>
    <key>WorkingDirectory</key><string>$RUN_DIR</string>
    <key>KeepAlive</key><true/>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$LOG_DIR/tap-$profile.out.log</string>
    <key>StandardErrorPath</key><string>$LOG_DIR/tap-$profile.err.log</string>
</dict>
</plist>
PLIST
  echo "$plist"
}

set_env_line() {  # profile port
  local envf; envf="$(profile_env_file "$1")"
  local line="WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=http://127.0.0.1:$2"
  [ -f "$envf" ] || { echo "  ⚠️  $envf 不存在，跳过"; return; }
  if grep -q "^WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=" "$envf"; then
    sed -i '' "s|^WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=.*|$line|" "$envf"
  else
    printf '\n# claude-tap debug proxy (managed by claude-tap-setup.sh)\n%s\n' "$line" >> "$envf"
  fi
  echo "  env: $envf ← $line"
}

unset_env_line() {  # profile
  local envf; envf="$(profile_env_file "$1")"
  [ -f "$envf" ] || return 0
  sed -i '' '/^# claude-tap debug proxy (managed by/d;/^WALKCODE_CLAUDE_ANTHROPIC_BASE_URL=/d' "$envf"
  echo "  env: $envf 已移除 WALKCODE_CLAUDE_ANTHROPIC_BASE_URL"
}

kick_walkcode() {  # profile
  launchctl kickstart -k "gui/$UID_NUM/com.walkcode.$1-claude" 2>/dev/null \
    && echo "  restarted com.walkcode.$1-claude" \
    || echo "  ⚠️  com.walkcode.$1-claude 未加载，跳过重启"
}

each_conf() {  # 回调：profile port target allow
  [ -f "$CONF" ] || die "缺少 $CONF —— 先跑：$0 init"
  local cb="$1" profile port target allow
  while read -r profile port target allow _; do
    case "$profile" in ''|\#*) continue ;; esac
    "$cb" "$profile" "$port" "$target" "$allow"
  done < "$CONF"
}

reload_tap() {  # label plist —— bootout 是异步的，等服务真正卸载再 bootstrap，
                # 否则紧跟着的 bootstrap 会竞态失败（Input/output error）
  local label="$1" plist="$2" i
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  for i in $(seq 1 50); do
    launchctl print "gui/$UID_NUM/$label" >/dev/null 2>&1 || break
    sleep 0.2
  done
  launchctl bootstrap "gui/$UID_NUM" "$plist"
}

do_apply() {
  local profile="$1" port="$2" target="$3" allow="$4"
  echo "── $profile (port $port, target $target)"
  local plist; plist=$(write_plist "$profile" "$port" "$target" "$allow")
  reload_tap "com.walkcode.tap-$profile" "$plist"
  echo "  tap: $plist 已加载"
  set_env_line "$profile" "$port"
  kick_walkcode "$profile"
}

do_remove() {
  local profile="$1"
  echo "── $profile"
  launchctl bootout "gui/$UID_NUM/com.walkcode.tap-$profile" 2>/dev/null || true
  rm -f "$PLIST_DIR/com.walkcode.tap-$profile.plist"
  echo "  tap 已卸载"
  unset_env_line "$profile"
  kick_walkcode "$profile"
}

do_status() {
  local profile="$1" port="$2"
  local pid; pid=$(launchctl list 2>/dev/null | awk -v l="com.walkcode.tap-$profile" '$3==l{print $1}')
  local listen="no"
  nc -z 127.0.0.1 "$port" 2>/dev/null && listen="yes"
  printf '%-10s port=%-6s launchd_pid=%-8s listening=%s\n' "$profile" "$port" "${pid:--}" "$listen"
}

do_init() {
  mkdir -p "$CONF_DIR" "$RUN_DIR" "$LOG_DIR"
  [ -f "$CONF" ] && die "$CONF 已存在，不覆盖"
  cat > "$CONF" <<'EOF'
# claude-tap sidecar 统一配置（唯一需要改的文件）
# 列：profile  端口  上游target(auto=从该profile settings.json自动探测)  额外放行路径(-=无)
#
# 改完跑 claude-tap-setup.sh apply 重新生成并生效；全部关掉用 remove。
# 上游形态参考（详见 docs/claude-tap-deploy.md）：
#   - OAuth 订阅 / 官方 API：target=auto，无需放行路径
#   - Vertex 代理网关（settings.json 配 ANTHROPIC_VERTEX_BASE_URL）：
#     target=auto，放行 /projects（网关路径通常没有 /v1 前缀）
#   - 真 Google Vertex（SA 认证，无显式 base url）：显式
#     target=https://aiplatform.googleapis.com/v1，放行 /projects
#
# work      18901  auto                                   -
# work2     18902  auto                                   /projects
# personal  18903  https://aiplatform.googleapis.com/v1   /projects
EOF
  echo "已生成 $CONF —— 按注释填好后跑：$0 apply"
}

case "${1:-status}" in
  init)   do_init ;;
  apply)  mkdir -p "$RUN_DIR" "$LOG_DIR"; each_conf do_apply; echo; echo "dashboard: http://127.0.0.1:19527" ;;
  remove) each_conf do_remove ;;
  status) each_conf do_status ;;
  *) echo "usage: $0 [init|apply|remove|status]"; exit 1 ;;
esac
