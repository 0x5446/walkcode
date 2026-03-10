"""Lightweight i18n: Chinese if system locale is zh*, English otherwise."""

import os

def _detect_zh() -> bool:
    lang = os.environ.get("LANG", "") or os.environ.get("LANGUAGE", "")
    return lang.startswith("zh")

_ZH = _detect_zh()

# (en, zh) pairs
_T: dict[str, tuple[str, str]] = {
    # --- config.py ---
    "config.missing_vars": (
        "Missing required env vars: {vars}\nSee .env.example",
        "缺少必需的环境变量: {vars}\n请参考 .env.example",
    ),

    # --- tty.py ---
    "tty.no_session": (
        "No tmux session specified",
        "未指定 tmux 会话",
    ),
    "tty.not_found": (
        "tmux session '{name}' not found (Claude exited?)",
        "tmux 会话 '{name}' 不存在（Claude 已退出？）",
    ),
    "tty.not_installed": (
        "tmux is not installed",
        "tmux 未安装",
    ),
    "tty.check_failed": (
        "tmux check failed: {error}",
        "tmux 检查失败: {error}",
    ),

    # --- __main__.py: serve ---
    "serve.listening": (
        "WalkCode serving on http://localhost:{port}",
        "WalkCode 已启动，监听 http://localhost:{port}",
    ),
    "serve.feishu_target": (
        "  Feishu {id_type}: {receive_id}",
        "  飞书 {id_type}: {receive_id}",
    ),
    "serve.hook_url": (
        "  Hook: POST http://localhost:{port}/hook",
        "  Hook: POST http://localhost:{port}/hook",
    ),
    "serve.no_receive_id": (
        "\n  ⚠️  FEISHU_RECEIVE_ID is not set."
        "\n     Send a message to your Feishu bot — the sender's open_id will be printed here."
        "\n     Then add it to ~/.walkcode/.env and run: walkcode restart\n",
        "\n  ⚠️  FEISHU_RECEIVE_ID 未设置。"
        "\n     向你的飞书机器人发送一条消息，发送者的 open_id 会显示在这里。"
        "\n     然后将其添加到 ~/.walkcode/.env 并执行: walkcode restart\n",
    ),
    "serve.received_open_id": (
        "\n  ✉️  Message received! Sender open_id: {open_id}"
        "\n     Add this to ~/.walkcode/.env:"
        "\n       FEISHU_RECEIVE_ID={open_id}"
        "\n     Then restart: walkcode restart\n",
        "\n  ✉️  收到消息！发送者 open_id: {open_id}"
        "\n     将以下内容添加到 ~/.walkcode/.env:"
        "\n       FEISHU_RECEIVE_ID={open_id}"
        "\n     然后重启: walkcode restart\n",
    ),

    # --- __main__.py: start/stop/restart/status ---
    "start.already_running": (
        "WalkCode already running (pid {pid})",
        "WalkCode 已在运行 (pid {pid})",
    ),
    "start.started": (
        "WalkCode started (pid {pid})",
        "WalkCode 已启动 (pid {pid})",
    ),
    "start.started_with_log": (
        "WalkCode started (pid {pid}), log: {log}",
        "WalkCode 已启动 (pid {pid})，日志: {log}",
    ),
    "not_running": (
        "WalkCode is not running",
        "WalkCode 未在运行",
    ),
    "stop.stopped": (
        "WalkCode stopped (pid {pid})",
        "WalkCode 已停止 (pid {pid})",
    ),
    "stop.killed": (
        "WalkCode killed (pid {pid})",
        "WalkCode 已强制终止 (pid {pid})",
    ),
    "status.running": (
        "WalkCode is running (pid {pid})",
        "WalkCode 运行中 (pid {pid})",
    ),

    # --- __main__.py: hook ---
    "hook.not_in_tmux": (
        "[walkcode] not in tmux, skipping hook",
        "[walkcode] 不在 tmux 中，跳过 hook",
    ),
    "hook.failed": (
        "[walkcode] hook failed: {error}",
        "[walkcode] hook 发送失败: {error}",
    ),

    # --- __main__.py: install-hooks ---
    "install_hooks.not_found": (
        "Error: {path} not found",
        "错误: 未找到 {path}",
    ),
    "install_hooks.done": (
        "Hooks installed to {path}",
        "Hooks 已安装到 {path}",
    ),
    "install_hooks.restart_hint": (
        "Restart Claude Code sessions to activate.",
        "重启 Claude Code 会话以生效。",
    ),

    # --- __main__.py: run helper ---
    "run.failed": (
        "  ✗ failed (exit {code})",
        "  ✗ 失败 (退出码 {code})",
    ),

    # --- __main__.py: upgrade ---
    "upgrade.current": (
        "Current version: {version}",
        "当前版本: {version}",
    ),
    "upgrade.latest": (
        "Latest release: {tag}",
        "最新版本: {tag}",
    ),
    "upgrade.no_release": (
        "No releases found, installing from main branch",
        "未找到正式版本，从 main 分支安装",
    ),
    "upgrade.restarting": (
        "Restarting daemon...",
        "正在重启守护进程...",
    ),
    "upgrade.not_running": (
        "Daemon not running, skipping restart.",
        "守护进程未运行，跳过重启。",
    ),
    "upgrade.complete": (
        "Upgrade complete.",
        "升级完成。",
    ),

    # --- __main__.py: uninstall ---
    "uninstall.stopping": (
        "Stopping daemon (pid {pid})...",
        "正在停止守护进程 (pid {pid})...",
    ),
    "uninstall.stopped": (
        "  Daemon stopped.",
        "  守护进程已停止。",
    ),
    "uninstall.removing_cli": (
        "Removing walkcode CLI...",
        "正在移除 walkcode CLI...",
    ),
    "uninstall.done": (
        "  Done.",
        "  完成。",
    ),
    "uninstall.removed_wrapper": (
        "  Removed shell wrapper from {path}",
        "  已从 {path} 移除 shell wrapper",
    ),
    "uninstall.removed_tmux": (
        "  Removed tmux config from {path}",
        "  已从 {path} 移除 tmux 配置",
    ),
    "uninstall.config_dir": (
        "\nConfig directory: {path}",
        "\n配置目录: {path}",
    ),
    "uninstall.config_contents": (
        "  Contains .env, state.json, logs, etc.",
        "  包含 .env、state.json、日志等。",
    ),
    "uninstall.remove_prompt": (
        "  Remove it? [y/N] ",
        "  是否删除？[y/N] ",
    ),
    "uninstall.removed_dir": (
        "  Removed {path}",
        "  已删除 {path}",
    ),
    "uninstall.kept_dir": (
        "  Kept {path}",
        "  已保留 {path}",
    ),
    "uninstall.complete": (
        "\nWalkCode uninstalled.",
        "\nWalkCode 已卸载。",
    ),

    # --- __main__.py: test-inject ---
    "test_inject.error": (
        "Error: {error}",
        "错误: {error}",
    ),
    "test_inject.done": (
        "Injected '{text}'{suffix} -> tmux:{session}",
        "已注入 '{text}'{suffix} -> tmux:{session}",
    ),

    # --- server.py: Feishu labels ---
    "feishu.label.stop": (
        "✅ Task complete",
        "✅ 任务完成",
    ),
    "feishu.label.permission": (
        "🔐 Permission required",
        "🔐 需要权限确认",
    ),
    "feishu.label.idle": (
        "⏳ Waiting for your input",
        "⏳ 等待你的输入",
    ),
    "feishu.label.elicitation": (
        "📋 Please choose",
        "📋 请选择",
    ),

    # --- server.py: Permission card ---
    "feishu.perm.header": (
        "🔐 Permission Required",
        "🔐 需要权限确认",
    ),
    "feishu.perm.allow": (
        "✅ Allow",
        "✅ 允许",
    ),
    "feishu.perm.deny": (
        "❌ Deny",
        "❌ 拒绝",
    ),
    "feishu.perm.always_allow": (
        "🔓 Always Allow",
        "🔓 始终允许",
    ),
    "feishu.perm.allowed": (
        "✅ Allowed",
        "✅ 已允许",
    ),
    "feishu.perm.denied": (
        "❌ Denied",
        "❌ 已拒绝",
    ),
    "feishu.perm.always_allowed": (
        "🔓 Always Allowed (rule added)",
        "🔓 已始终允许（规则已添加）",
    ),
    "feishu.perm.timeout": (
        "⏰ Timed out — denied",
        "⏰ 超时未响应，已拒绝",
    ),
    "feishu.perm.expired": (
        "Request expired",
        "请求已过期",
    ),

    # --- server.py: Feishu user-facing messages ---
    "feishu.stale_session": (
        "⚠️ tmux session expired, please wait for the next notification from Claude",
        "⚠️ tmux 会话已失效，请等待 Claude 的下一条通知刷新会话",
    ),
    "feishu.start_failed": (
        "⚠️ Start failed: {error}",
        "⚠️ 启动失败: {error}",
    ),
    "feishu.started": (
        "🚀 Claude Code started\ntmux attach -t {tmux}",
        "🚀 已启动 Claude Code\ntmux attach -t {tmux}",
    ),
    "feishu.started_with_session": (
        "🚀 Claude Code | {session_id}\ntmux attach -t {tmux}",
        "🚀 Claude Code | {session_id}\ntmux attach -t {tmux}",
    ),
    "feishu.resume_failed": (
        "⚠️ Resume failed: {error}",
        "⚠️ 恢复失败: {error}",
    ),
    "feishu.resumed": (
        "🔄 Claude Code session resumed\ntmux attach -t {tmux}",
        "🔄 已恢复 Claude Code 会话\ntmux attach -t {tmux}",
    ),
    "feishu.text_only": (
        "⚠️ Only text replies are supported",
        "⚠️ 只支持文本回复",
    ),
    "feishu.session_expired": (
        "⚠️ Session expired, send a new message to start",
        "⚠️ 会话已过期，请发送新消息开始新会话",
    ),
    "feishu.session_not_found": (
        "⚠️ Session not found, wait for next notification to reply",
        "⚠️ 找不到对应会话，请等待下一条通知后再回复",
    ),
    "feishu.idle_killed": (
        "⏰ Session closed due to inactivity, reply to resume",
        "⏰ 会话因长时间无活动已关闭，回复任意消息可恢复",
    ),
}


def t(key: str, **kwargs) -> str:
    """Translate a key, optionally formatting with kwargs."""
    pair = _T.get(key)
    if not pair:
        return key
    text = pair[1] if _ZH else pair[0]
    return text.format(**kwargs) if kwargs else text
