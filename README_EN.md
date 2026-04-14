# WalkCode

[**中文文档**](README.md)

> **Code is cheap. Show me your talk.**

**Your agent codes. You walk.**

WalkCode lets AI coding agents message you when they need help, so you can review, approve, or redirect them from your phone. Stay in the loop without being stuck at your desk.

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> Chat (thread)
                     <──tmux send-keys──  <──WS── (reply)
```

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
- **Works with screen locked** — built on tmux, no GUI dependency

## Features

**Core:**
- **Permission approvals** — approve or deny directly from chat
- **Question answering** — AskUserQuestion interactive cards with multi-question sequential flow
- **Text replies** — reply in a thread to type directly into the agent's terminal
- **Image & rich text** — send images or rich text (text + images); images are auto-downloaded and passed to the agent
- **Remote start** — send a message to start a new agent session from your phone
- **Session resume** — reply in an expired thread to automatically resume the conversation
- **Auto-cleanup** — idle tmux sessions are killed after 2 hours and you get notified
- **Multi-agent** — run Claude Code and Codex CLI simultaneously, each with its own Feishu bot

**Also:**
- **Multi-session** — multiple agent sessions, one instance, auto-routing
- **Session persistence** — survives server restarts
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
   - `im:message:send_as_bot` — Send messages as bot (also covers message updates)
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

Create a launchd plist so WalkCode starts automatically when you log in:

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
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
```

> **Important:** Replace `/Users/YOU/.local/bin/walkcode` with your actual walkcode path (run `which walkcode` to find it). The `PATH` must include the directories containing `tmux` and `claude` (typically `/opt/homebrew/bin`).

```bash
launchctl load ~/Library/LaunchAgents/com.walkcode.plist     # Load and start
launchctl unload ~/Library/LaunchAgents/com.walkcode.plist   # Stop and unload
```

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

# Codex API key
OPENAI_API_KEY=sk-xxx
EOF
```

#### 4. Install Codex Hooks

```bash
walkcode install-hooks --agent codex
```

This writes `~/.codex/hooks.json` and enables the Codex hooks feature flag.

#### 5. Start Codex Instance

```bash
WALKCODE_ENV_FILE=~/.walkcode/codex.env walkcode start
```

Now you have two bots: message the Claude bot for Claude Code, message the Codex bot for Codex CLI.

#### 6. (Recommended) Auto-start on Login

Create a second launchd plist for the Codex instance:

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
        <string>start</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>WALKCODE_ENV_FILE</key>
        <string>/Users/YOU/.walkcode/codex.env</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF
```

> Replace `/Users/YOU` with your actual home directory.

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

### Security: Remote Start Permissions

When you start an agent from chat, WalkCode launches it with a controlled permission mode (Claude Code: `--permission-mode default`, Codex CLI: `--ask-for-approval untrusted`). Hooks enable approval from Feishu:

| Tool status | What happens |
|---|---|
| In allow list | Auto-approved, no prompt |
| **Not** in allow list | **Interactive card sent to chat** — tap Allow / Deny / Always Allow |

Requests are auto-denied after 2 minutes with no response.

## Usage

| Scenario | What You See | What You Do |
|----------|-------------|-------------|
| Permission prompt | Interactive card with tool details | Tap **Allow** / **Deny** / **Always Allow** |
| Question from agent | Interactive card with option buttons | Tap an option; multi-question flows auto-advance |
| Send image | Reply with image in thread | Image auto-downloaded, passed as `![Image N](path)` to agent |
| Send rich text | Reply with rich text in thread | Text and images preserved in order |
| Waiting for input | Text in thread | Reply with text |
| Task complete | Text in thread | Reply to continue, or ignore |
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
walkcode upgrade                          # Pull + reinstall + restart
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
| `PORT` / `WALKCODE_PORT` | No | HTTP server port (default: `3001`) |
| `WALKCODE_CWD` | No | Default cwd for remote-started sessions (default: `~/.walkcode/workspace`) |
| `WALKCODE_AGENT` | No | Agent type: `claude` (default) or `codex` |
| `WALKCODE_INSTANCE` | No | Instance name (isolates PID/log/state for multi-agent) |
| `WALKCODE_ENV_FILE` | No | Override `.env` file path (for multi-instance setups) |
| `WALKCODE_STATE_PATH` | No | Custom state file path |

## Roadmap

### Features

| Feature | Status |
|---------|--------|
| Permission approvals, Q&A, text replies | Supported |
| Image and rich text messages | Supported |
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
