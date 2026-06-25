# WalkCode

[**中文文档**](README.md)

> **Code is cheap. Show me your talk.**

**Your agent codes. You walk.**

WalkCode lets AI coding agents message you when they need help, so you can review, approve, or redirect them from your phone. Stay in the loop without being stuck at your desk.

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> Chat (thread)
                     <──tmux send-keys──  <──WS── (reply)
```

## ✨ What It Does

- 🔔 **Approve from your phone** — when the agent hits a permission prompt, your phone buzzes; tap "Allow" and it keeps going
- 💬 **Drive it from your phone** — reply with text, images, or rich text and it goes straight into the agent's terminal
- 🩺 **Session health card** — every session keeps a live status card at the top of its thread: Running / Waiting for you / Done, plus model, duration, message count, and token usage, updated on key events
- 🧵 **One thread = one session = one agent** — zero cross-talk; reply in a thread and it always reaches the right agent
- 🚀 **Remote start + auto-resume** — send a message to spin up an agent in a fresh tmux; reply in an expired thread to resume it
- 🤖 **Multiple agents in parallel** — run Claude Code and Codex CLI at the same time, each with its own Feishu bot
- 🔌 **Flexible routing** — give each instance its own launch flags and permission mode (e.g. route Claude through Vertex, run Codex fully autonomous with `--yolo`)

## Supported Agents

| Agent | Status | Notes |
|-------|--------|-------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Supported | Default agent |
| [Codex CLI](https://github.com/openai/codex) | Supported | Requires separate Feishu bot |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) / [Cline](https://github.com/cline/cline) / [Aider](https://github.com/Aider-AI/aider) etc. | Planned | Via Agent Adapter |

> **Each agent gets its own Feishu bot.** Message the Claude bot to start Claude Code, message the Codex bot to start Codex CLI. Clean and intuitive.

## Why WalkCode?

Your agent hits a permission prompt while you're away. Without WalkCode, it blocks until you're back. With WalkCode, your phone buzzes, you tap "Allow", and it keeps going.

- **Don't let your agent wait** — approve prompts and reply to requests from chat, anytime
- **Every session in its own thread** — reply in a thread, it always reaches the right agent
- **Know the status at a glance** — the health card at the top of each thread tells you whether it's running, waiting on you, or done
- **Works with screen locked** — built on tmux, no GUI dependency

## Features

**Core:**
- **Permission approvals** — tools not on the allow list send an interactive card to chat; tap Allow / Deny / Always Allow. If you handle it in the terminal instead, the Feishu card is invalidated so you don't act on it twice
- **Question answering** — AskUserQuestion interactive cards, each option shown with its description, with multi-question sequential flow, multiSelect, and custom text (Other) via thread reply
- **Text replies** — reply in a thread to type directly into the agent's terminal; when the agent finishes a turn, the full turn (including multi-part output) is forwarded to you, not just the last chunk
- **Image & rich text** — send images or rich text (text + images); images are auto-downloaded and passed to the agent
- **Session health card** — every session keeps a live status card at the top of its thread, with status, model, duration, message count, and token usage (grouped by model), updated on key events and frozen once the session ends
- **Remote start** — send a message to start a new agent session from your phone
- **Session resume** — reply in an expired thread to automatically resume the conversation
- **Send while busy** — a message you send while the agent is busy is injected right away (queuing is left to the terminal/agent), with an emoji reaction to confirm delivery
- **Auto-cleanup** — idle tmux sessions are killed after 2 hours and you get notified
- **Multi-agent** — run Claude Code and Codex CLI simultaneously, each with its own Feishu bot

**Also:**
- **Multi-session** — multiple agent sessions, one instance, auto-routing
- **Session persistence** — survives server restarts
- **Auto auth recovery** — when a Codex OAuth token expires, device-auth is started automatically; finish it from your phone
- **Emoji receipts** — random emoji reactions confirm delivery at a glance
- **i18n** — auto-detects system locale (Chinese for zh*, English otherwise)

## Quick Start (Claude Code)

> The guide below uses Claude Code as the default. For Codex CLI setup, see [Multi-Agent Setup](#multi-agent-setup-codex-cli).

### Before You Start

- macOS
- [tmux](https://github.com/tmux/tmux) (`brew install tmux`)
- [uv](https://docs.astral.sh/uv/) (Python >= 3.13)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed locally
- A [Feishu / Lark](https://www.feishu.cn/) bot (free enterprise app)

### One-Click Install

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/install.sh | bash
```

This installs tmux/uv if missing, installs the WalkCode CLI via `uv tool install`, creates `.env`, adds a shell wrapper, configures tmux scrollback, and installs Claude Code hooks. [Review the script](install.sh) before running if you prefer.

Open a new terminal window, or run `exec $SHELL` to reload your current session.

### Upgrade

```bash
walkcode upgrade
```

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/uninstall.sh | bash
```

### Configure & Run

#### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) and create an enterprise app
2. **Add capability** > Bot
3. **Permissions** > Enable:
   - `im:message` — Read messages
   - `im:message:send_as_bot` — Send messages as bot (also covers message updates, required for the health card)
   - `im:message.reactions:write_only` — Add emoji reactions
4. **Events & Callbacks** > Long connection mode > Add events:
   - `im.message.receive_v1` — Receive messages
   - `card.action.trigger` — Receive card button clicks
5. **Version Management** > Create version > Publish

#### 2. Edit `.env`

```bash
vim ~/.walkcode/.env
```

Fill in `FEISHU_APP_ID` and `FEISHU_APP_SECRET` from your Feishu app dashboard.

#### 3. Get Your open_id

```bash
walkcode serve
```

Send any message to your bot in Feishu. WalkCode will print the sender's `open_id` in the console. Add it to `FEISHU_RECEIVE_ID` in `.env`, then Ctrl+C and start the daemon:

```bash
walkcode start
```

That's it. Type `claude` and go for a walk.

#### 4. (Recommended) Auto-start on Login

Create a launchd plist so WalkCode starts automatically on login and auto-restarts on crash:

```bash
cat > ~/Library/LaunchAgents/com.walkcode.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.walkcode</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/walkcode</string>
        <string>serve</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>WALKCODE_ENV_FILE</key>
        <string>/Users/YOU/.walkcode/.env</string>
        <key>HOME</key>
        <string>/Users/YOU</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>/Users/YOU/.walkcode</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>ExitTimeOut</key>
    <integer>15</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>/Users/YOU/.walkcode/launchd.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOU/.walkcode/launchd.err.log</string>
</dict>
</plist>
EOF
```

> **Important:**
> - Replace `/Users/YOU` with your actual home directory.
> - Use `walkcode serve` (foreground uvicorn), **not** `walkcode start` (daemonizer). `start` forks and the parent exits immediately — launchd loses track of the real PID and `KeepAlive` silently stops working. `serve` blocks in the foreground so launchd watches it and restarts on crash.
> - `KeepAlive { SuccessfulExit=false, Crashed=true }` keeps a clean `launchctl unload` from triggering a respawn while still recovering from crashes (throttled by `ThrottleInterval=10s`).
> - `WALKCODE_ENV_FILE` must be an absolute path. The launchd environment does not inherit direnv/shell rc files, so Feishu credentials have to be pointed at explicitly.

```bash
launchctl load ~/Library/LaunchAgents/com.walkcode.plist     # Load and start
launchctl unload ~/Library/LaunchAgents/com.walkcode.plist   # Stop and unload
launchctl list | grep walkcode                               # Status (PID + last exit code)
```

> Once launchd owns the process, **don't also run `walkcode start` manually** — both would fight for the same port. Use `launchctl load/unload` for lifecycle, or unload first before attaching a manual `walkcode start` for debugging.

#### 5. (Recommended) Prevent macOS from Sleeping

WalkCode depends on a persistent network connection. When plugged in, configure macOS to never sleep the system (display can still turn off):

```bash
sudo pmset -c sleep 0 && sudo pmset -c disksleep 0 && sudo pmset -c standby 0 && sudo pmset -c hibernatemode 0
```

<details>
<summary>Manual Install (step-by-step)</summary>

#### 1. Create a Feishu App

(Same as above)

#### 2. Install

```bash
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/0x5446/walkcode.git
```

> To enable auto-summarized titles for the session health card (optional, Codex sessions only — Claude uses its own AI title), install with the `summary` extra:
> `uv tool install "walkcode[summary] @ git+https://github.com/0x5446/walkcode.git"`

#### 3. Configure

```bash
mkdir -p ~/.walkcode && cp .env.example ~/.walkcode/.env
vim ~/.walkcode/.env  # Fill in Feishu App ID and Secret
```

#### 4. Add Shell Wrapper

Add to `~/.zshrc` (or `~/.bashrc`):

```bash
claude() {
  if [ -z "$TMUX" ]; then
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
```

Then: `source ~/.zshrc`

#### 5. Configure tmux Scrollback

Add to `~/.tmux.conf`:

```bash
set-option -ga terminal-overrides ',*:smcup@:rmcup@'
```

#### 6. Install Hooks

```bash
walkcode install-hooks
```

</details>

## Multi-Agent Setup (Codex CLI)

WalkCode supports running multiple agents simultaneously. **Each agent gets its own Feishu bot.** Here's how to add Codex CLI:

### How It Works

```
Feishu Bot A (Claude)  ──>  WalkCode Instance A (port 3001)  ──>  claude
Feishu Bot B (Codex)   ──>  WalkCode Instance B (port 3002)  ──>  codex
```

Each instance has its own `.env`, port, PID file, log, and state — fully isolated.

### Steps

#### 1. Install Codex CLI

```bash
npm install -g @openai/codex
```

#### 2. Create a Second Feishu App

Follow the [Create a Feishu App](#1-create-a-feishu-app) steps to create a new bot (e.g., name it "Codex").

#### 3. Create Codex Instance Config

```bash
cat > ~/.walkcode/codex.env << 'EOF'
# Codex instance
WALKCODE_AGENT=codex
WALKCODE_INSTANCE=codex
PORT=3002

# Feishu App for Codex bot
FEISHU_APP_ID=cli_codex_xxx
FEISHU_APP_SECRET=xxx
FEISHU_RECEIVE_ID=ou_xxx

# Enable this for Lark international apps
# LARK_OPENAPI_DOMAIN=https://open.larksuite.com

# Run remote-started sessions fully autonomous (skip approvals + sandbox)
WALKCODE_PERMISSION_FLAG=--yolo

# Authentication (pick one):
# Option A: ChatGPT subscription (recommended) — run `codex login` first
#   When token expires, WalkCode auto-starts device-auth and sends the code to Feishu
# Option B: API Key
# OPENAI_API_KEY=sk-xxx
EOF
```

> `FEISHU_RECEIVE_ID` must be the `open_id` discovered from the Codex bot's own app. Do not reuse an `open_id` printed by the Claude bot; Feishu/Lark `open_id` values are app-scoped, and cross-app IDs fail with `open_id cross app`.

#### 4. Add Shell Wrapper

Add to `~/.zshrc` (or `~/.bashrc`) so local `codex` runs auto-wrap in tmux:

```bash
codex() {
  if [ -z "$TMUX" ]; then
    local session="codex-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command codex --no-alt-screen $(printf '%q ' "$@")"
  else
    command codex "$@"
  fi
}
```

Then: `source ~/.zshrc`

> `--no-alt-screen` keeps Codex output in tmux scrollback, which WalkCode needs for output capture.

#### 5. Install Codex Hooks

```bash
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode install-hooks --agent codex
```

This writes `~/.codex/hooks.json` (hook commands auto-point to port 3002) and enables the Codex hooks feature flag.

The installed hook commands explicitly include `WALKCODE_AGENT=codex`, `WALKCODE_PORT=3002`, and preserve `WALKCODE_ENV_FILE`, so Codex permission responses don't accidentally use the Claude hook protocol.

#### 6. Start Codex Instance

```bash
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode start
```

Now you have two bots: message the Claude bot for Claude Code, message the Codex bot for Codex CLI.

**Auto auth recovery:** When Codex OAuth token expires, WalkCode detects the error, runs device-auth, and sends the verification URL + code to Feishu. Complete re-login from your phone — no need to go back to your computer.

#### 7. (Recommended) Auto-start on Login

Create a second launchd plist for the Codex instance (same template as the Claude one, only `Label`, `WALKCODE_ENV_FILE` and log paths differ):

```bash
cat > ~/Library/LaunchAgents/com.walkcode-codex.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.walkcode-codex</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/walkcode</string>
        <string>serve</string>
    </array>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>WALKCODE_ENV_FILE</key>
        <string>/Users/YOU/.walkcode/codex.env</string>
        <key>HOME</key>
        <string>/Users/YOU</string>
    </dict>

    <key>WorkingDirectory</key>
    <string>/Users/YOU/.walkcode</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
        <key>Crashed</key>
        <true/>
    </dict>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>ExitTimeOut</key>
    <integer>15</integer>

    <key>ProcessType</key>
    <string>Background</string>

    <key>StandardOutPath</key>
    <string>/Users/YOU/.walkcode/launchd.codex.out.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/YOU/.walkcode/launchd.codex.err.log</string>
</dict>
</plist>
EOF

launchctl load ~/Library/LaunchAgents/com.walkcode-codex.plist
```

> Replace `/Users/YOU` with your actual home directory. Same `serve` vs `start` rationale as the Claude instance.

### Flexible Routing: Per-Instance Launch Flags

Each instance can set its own agent launch flags and permission mode via `.env`, fully independent of the others:

| Variable | Purpose | Typical use |
|----------|---------|-------------|
| `WALKCODE_PERMISSION_FLAG` | Replace the default permission/approval flag | `--yolo` for fully autonomous Codex; unset uses the default (Claude `--permission-mode default`, Codex `--ask-for-approval untrusted`) |
| `WALKCODE_EXTRA_ARGS` | Insert extra flags after the agent command | Route Claude through Vertex: `--settings /Users/you/.walkcode/vertex.json` (absolute path) |

> These values are strictly shell-escaped before being spliced into the launch command, so they can only ever be parsed as agent arguments, never as shell syntax. Applied on both start and resume, so routing stays consistent when a session is resumed.
>
> **Note:** escaping keeps values literal — a `~` / `$HOME` in a path is NOT expanded, so use an absolute path (e.g. `/Users/you/.walkcode/vertex.json`). Routing Claude through Vertex means your code context is handled by your own cloud project — only enable it for trusted projects; keep the `--settings` file and its credentials (including the service account JSON) outside the repo and out of version control.

### Multi-Instance Management

```bash
# Claude instance (default)
walkcode start / stop / status

# Codex instance
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode start / stop / status
```

### File Layout

```
~/.walkcode/
  .env                # Claude instance config
  codex.env           # Codex instance config
  walkcode.pid/log    # Claude instance runtime
  codex.pid/log       # Codex instance runtime
  state.json          # Claude session state
  codex-state.json    # Codex session state
  images/             # Shared image cache
```

## Architecture: 1:1:1 Mapping

> For a deep dive into the internal design, see [ARCHITECTURE.md](ARCHITECTURE.md).

WalkCode uses a strict 1:1:1 mapping: **one chat thread, one tmux session, one agent process.** Zero cross-talk, naturally isolated context, stateless message routing.

```
Chat Thread A  <──1:1──>  tmux: claude-myapp-12345  <──1:1──>  Claude Code (myapp)
Chat Thread B  <──1:1──>  tmux: claude-api-67890    <──1:1──>  Claude Code (api)
Chat Thread C  <──1:1──>  tmux: walkcode-99999      <──1:1──>  Codex CLI (api)
```

### Session Health Card

Every session keeps an interactive card at the top of its thread. Remote replies, local input, permission approvals, and Stop results refresh it immediately, so you can see the session status at a glance:

| Field | Content |
|-------|---------|
| Status | 🟢 Running / 🟠 Waiting for you / ✅ Done / 🔴 Error / ⏱️ Timeout interrupted (the card color tracks the status) |
| Session | Current session ID |
| Model | Model the session is using |
| Duration | How long the session has been running |
| Inputs | Number of messages you've sent |
| Tokens | Cumulative token usage (grouped by model) |

The thread title is auto-named from a task summary (Claude uses its own AI title; Codex can optionally use Haiku to refine it — see Configuration). Once the session ends the card is frozen and stops refreshing. If a running state has no observable progress for more than 30 minutes, or a permission / AskUserQuestion wait state goes unanswered for more than 30 minutes, WalkCode sends Esc to interrupt it, marks the card as "Timeout interrupted", and posts a thread notice. This uses WalkCode's own recorded state and hook/progress events; it does not parse TUI footer wording, treat pane repaints as progress, or depend on tmux `window_activity`. Subagent/task lifecycle events refresh the timer only while the session is running; if the session is stopped on permission or AskUserQuestion, they do not move it back to running or reset the wait timeout. A single long-running tool call that exceeds the threshold without any WalkCode hook/progress event is intentionally treated as "no observable progress" and interrupted; raise `WALKCODE_STUCK_THRESHOLD` for long unattended tasks. The whole feature is on by default; set `WALKCODE_HEALTH_CARD=0` to disable the health card and automatic timeout interrupts.

### Security: Remote Start Permissions

When you start an agent from chat, WalkCode launches it with a controlled permission mode (Claude Code: `--permission-mode default`; Codex CLI: `--ask-for-approval untrusted`, plus a fixed `--dangerously-bypass-hook-trust` so WalkCode's own hooks aren't gated by per-change trust prompts, and `--no-alt-screen`). Hooks enable approval from Feishu:

| Tool status | What happens |
|---|---|
| In allow list | Auto-approved, no prompt |
| **Not** in allow list | **Interactive card sent to chat** — tap Allow / Deny / Always Allow |

> If you set `WALKCODE_PERMISSION_FLAG` to a fully autonomous mode like `--yolo` (Codex) in an instance `.env`, the agent skips approvals and runs directly — no approval cards. Choose this according to your own trust boundary.

If you don't respond within 30 minutes, WalkCode uses the unified watchdog to send Esc to the TUI and marks the wait as "Timeout interrupted". That visible interrupt applies to WalkCode remote-started sessions bound to a health-card thread; locally started sessions without a remote thread still rely on the hook's own fail-open behavior. If the WalkCode server is unreachable or the hook itself crashes, the hook still fails open — it does not block the agent. The agent falls back to its own native terminal permission prompt, so "hook broken = same as no WalkCode installed" instead of leaving the Coding Agent stuck.

## Usage

| Scenario | What You See | What You Do |
|----------|-------------|-------------|
| Permission prompt | Interactive card with tool details | Tap **Allow** / **Deny** / **Always Allow** |
| Question from agent | Interactive card with option buttons and descriptions | Tap an option; multi-question flows auto-advance |
| Check progress | Health card at the top of the thread | Glance at status / duration / tokens, nothing to do |
| Send image | Reply with image in thread | Image auto-downloaded, passed as `![Image N](path)` to agent |
| Send rich text | Reply with rich text in thread | Text and images preserved in order |
| Waiting for input | Text in thread | Reply with text |
| Task complete | Full turn forwarded to thread | Reply to continue, or ignore |
| Mid-turn follow-up | Send while the agent is busy | Injected right away, emoji confirms delivery |
| Session expired | Reply in old thread | Agent resumes automatically |
| Remote start | Send a message in chat | Agent starts in new tmux session |

## CLI

```bash
walkcode start                            # Start as daemon
walkcode stop                             # Stop daemon
walkcode restart                          # Restart daemon
walkcode status                           # Check if running
walkcode serve                            # Foreground (debug)
walkcode install-hooks                    # Install Claude Code hooks
walkcode install-hooks --agent codex      # Install Codex CLI hooks
walkcode upgrade                          # Pull latest release + reinstall + restart
walkcode uninstall                        # Uninstall WalkCode
walkcode clean-images 1d                  # Clean images older than 1d (1d/1w/1m/180d)
walkcode test-inject <tmux-session> "hi"  # Test injection
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `FEISHU_APP_ID` | Yes | Feishu app ID |
| `FEISHU_APP_SECRET` | Yes | Feishu app secret |
| `FEISHU_RECEIVE_ID` | No | Your open_id or chat_id (run `walkcode serve` to discover) |
| `FEISHU_RECEIVE_ID_TYPE` | No | `open_id` (default) or `chat_id` |
| `LARK_OPENAPI_DOMAIN` | No | OpenAPI domain. Feishu defaults to `https://open.feishu.cn`; use `https://open.larksuite.com` for Lark international (`FEISHU_OPENAPI_DOMAIN` also works) |
| `PORT` / `WALKCODE_PORT` | No | HTTP server port (default: `3001`) |
| `WALKCODE_CWD` | No | Default cwd for remote-started sessions (default: `~/.walkcode/workspace`) |
| `WALKCODE_AGENT` | No | Agent type: `claude` (default) or `codex` |
| `WALKCODE_INSTANCE` | No | Instance name (isolates PID/log/state for multi-agent) |
| `WALKCODE_ENV_FILE` | No | Override `.env` file path (for multi-instance setups) |
| `WALKCODE_STATE_PATH` | No | Custom state file path (default: `~/.walkcode/state.json` for the main Claude instance, `~/.walkcode/<instance>-state.json` for others) |
| `WALKCODE_PERMISSION_FLAG` | No | Replace the agent's default permission/approval flag, e.g. `--yolo` for Codex |
| `WALKCODE_EXTRA_ARGS` | No | Extra launch flags inserted after the agent command, e.g. `--settings` to route Claude through Vertex |
| `WALKCODE_HEALTH_CARD` | No | Session health card and automatic timeout interrupt toggle; set `0` to disable (on by default) |
| `WALKCODE_STUCK_THRESHOLD` | No | Seconds before auto-sending Esc for a running state with no progress, or an unanswered wait state (default: `1800`, 30 minutes). After changing it, rerun `walkcode install-hooks` for each instance and restart the service so agent hook timeouts stay aligned with the watchdog |
| `WALKCODE_SUMMARY_VERTEX_PROJECT` | No | Vertex project for health-card title summarization (Codex sessions only; unset → first line as title) |
| `WALKCODE_SUMMARY_VERTEX_REGION` | No | Vertex region (default: `global`) |
| `WALKCODE_SUMMARY_SA_PATH` | No | Path to the Vertex service account JSON |
| `WALKCODE_SUMMARY_MODEL` | No | Title summarization model (default: `claude-haiku-4-5`) |
| `WALKCODE_SUMMARY_TIMEOUT` | No | Title summarization timeout in seconds (default: `8`) |

> Title summarization (`WALKCODE_SUMMARY_*`) applies to **Codex sessions only** (Claude uses its own AI title and skips this path) and requires the optional `summary` extra (`anthropic[vertex]`). When it isn't installed or configured, Codex degrades to using the task's first line as the title — nothing else is affected. Keep credentials like the service account JSON outside the repo, use an absolute path, and don't commit them.

## Roadmap

### Features

| Feature | Status |
|---------|--------|
| Permission approvals, Q&A, text replies | Supported |
| Image and rich text messages | Supported |
| Session health card | Supported |
| Remote start and session resume | Supported |
| Multi-agent (Claude Code + Codex CLI) | Supported |
| Forwarded messages | Planned |

### Chat Platforms

| Platform | Status |
|----------|--------|
| [Feishu / Lark](https://www.feishu.cn/) | Supported |
| [Slack](https://slack.com/) | Planned |
| [Telegram](https://telegram.org/) | Planned |
| [Discord](https://discord.com/) | Planned |

## Community

- [GitHub Issues](https://github.com/0x5446/walkcode/issues) — Bug reports & feature requests
- [GitHub Discussions](https://github.com/0x5446/walkcode/discussions) — Q&A & ideas

### Feishu Group

<img src="docs/images/feishu-group-qr.jpg" width="300" alt="Feishu Group">

## Contributing

Issues and PRs welcome.

## Disclaimer

Not affiliated with Anthropic or OpenAI. Claude is a trademark of Anthropic. Codex is a trademark of OpenAI.

## License

[MIT](LICENSE)
