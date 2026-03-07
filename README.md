# WalkCode

[**中文文档**](README_CN.md)

> **Code is cheap. Show me your talk.**

**Your agent codes. You walk.**

WalkCode lets AI coding agents message you when they need help, so you can review, approve, or redirect them from your phone. Stay in the loop without being stuck at your desk.

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> Chat (thread + buttons)
                     <──tmux send-keys──  <──WS── (tap / reply)
```

## Why WalkCode?

Your agent hits a permission prompt while you're away. Without WalkCode, it blocks until you're back. With WalkCode, your phone buzzes, you tap "Allow", and it keeps going.

- **Don't let your agent wait** — approve prompts and reply to requests from chat, anytime
- **Every session in its own thread** — reply in a thread, it always reaches the right agent
- **Works with screen locked** — built on tmux, no GUI dependency

## Features

**Core:**
- **One-tap permissions** — interactive cards with Allow / Deny / Always buttons
- **Text replies** — reply in a thread to type directly into the agent's terminal
- **Remote start** — send a message to start a new agent session from your phone
- **Session resume** — reply in an expired thread to automatically resume the conversation
- **Auto-cleanup** — idle tmux sessions are killed after 2 hours and you get notified

**Also:**
- **Multi-session** — multiple agents, one instance, auto-routing
- **Session persistence** — survives server restarts
- **Emoji receipts** — random emoji reactions confirm delivery at a glance

## Architecture: 1:1:1 Mapping

WalkCode uses a strict 1:1:1 mapping: **one chat thread, one tmux session, one agent process.** This avoids cross-talk, keeps context localized, and makes message routing stateless.

```
Chat Thread A  <──1:1──>  tmux: claude-myapp-12345  <──1:1──>  Claude Code (myapp)
Chat Thread B  <──1:1──>  tmux: claude-api-67890    <──1:1──>  Claude Code (api)
```

### How Remote Start Works

You can start an agent directly from chat — no terminal needed:

1. You send a message (e.g., "fix the login bug in myapp")
2. WalkCode creates a tmux session with `claude "<your message>"`
3. WalkCode replies in a thread confirming the session started
4. It stores the link: `tmux session name → chat message ID` (in `_pending_roots`)
5. When the agent's hooks fire for the first time, WalkCode matches the tmux name and links the session to that thread
6. From now on, all events from this agent reply to the same thread — the 1:1:1 link is established

### Security: Remote Start Permissions

When you start an agent from chat, WalkCode launches Claude Code with `--permission-mode dontAsk`. This is a deliberate security design:

| | `dontAsk` mode (WalkCode) | `dangerouslySkipPermissions` |
|---|---|---|
| Trust dialog | Skipped | Skipped |
| Tools in `permissions.allow` | Auto-approved | Auto-approved |
| Tools NOT in `permissions.allow` | **Auto-denied (safe)** | **Auto-approved (unsafe)** |

This means remote-started sessions respect your `~/.claude/settings.json` permission rules — tools you've allowed (like `Bash(*)`, `Read(*)`, `Edit(*)`) work automatically, while anything else is denied rather than left hanging for approval that will never come.

To customize which tools are allowed, edit your `~/.claude/settings.json`:

```json
{
  "permissions": {
    "allow": ["Bash(*)", "Read(*)", "Write(*)", "Edit(*)", "Glob(*)", "Grep(*)"],
    "deny": ["Bash(rm -rf /*)"]
  }
}
```

## Quick Start

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

This installs tmux/uv if missing, clones the repo, runs `uv sync`, creates `.env`, adds a shell wrapper, configures tmux scrollback, and installs Claude Code hooks. [Review the script](install.sh) before running if you prefer.

After installation, reload your shell config:

```bash
source ~/.zshrc  # or source ~/.bashrc for bash users
```

### Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/0x5446/walkcode/main/uninstall.sh | bash
```

Removes the daemon, shell wrapper, tmux config, Claude Code hooks, and the `~/.walkcode` directory. If you customized the install path, prefix with `WALKCODE_DIR=/your/path`. [Review the script](uninstall.sh) before running if you prefer.

### Configure & Run

#### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) and create an enterprise app
2. **Add capability** > Bot
3. **Permissions** > Enable:
   - `im:message` — Read messages
   - `im:message:send_as_bot` — Send messages
   - `im:message.reactions:write_only` — Add emoji reactions
4. **Events & Callbacks** > Long connection mode > Add event `im.message.receive_v1`
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

Send any message to your bot in Feishu, check logs for `open_id`, add it to `FEISHU_RECEIVE_ID` in `.env`, then Ctrl+C to stop.

#### 4. Start the Daemon

```bash
walkcode start
```

That's it. Type `claude` and go for a walk.

### Manual Install

<details>
<summary>Step-by-step instructions</summary>

#### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) and create an enterprise app
2. **Add capability** > Bot
3. **Permissions** > Enable:
   - `im:message` — Read messages
   - `im:message:send_as_bot` — Send messages
   - `im:message.reactions:write_only` — Add emoji reactions
4. **Events & Callbacks** > Long connection mode > Add event `im.message.receive_v1`
5. **Version Management** > Create version > Publish

#### 2. Install

```bash
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/walkcode.git ~/.walkcode
cd ~/.walkcode
uv sync
cp .env.example .env
```

Edit `.env` with your Feishu App ID and Secret.

#### 3. Get Your open_id

```bash
uv run walkcode serve
```

Send any message to your bot in Feishu, check logs for `open_id`, add to `FEISHU_RECEIVE_ID` in `.env`, restart.

#### 4. Add Shell Wrapper

Add to `~/.zshrc` (or `~/.bashrc`):

```bash
claude() {
  if [ -z "$TMUX" ]; then
    local session="claude-$(basename "$PWD")-$$"
    tmux new-session -s "$session" "command claude $@"
  else
    command claude "$@"
  fi
}
```

Then: `source ~/.zshrc`

#### 5. Configure tmux Scrollback

Add to `~/.tmux.conf`:

```bash
# Disable alternate screen so TUI output (e.g. Claude Code) stays in scrollback
set-option -ga terminal-overrides ',*:smcup@:rmcup@'
```

Then: `tmux source-file ~/.tmux.conf` (if tmux is running)

#### 6. Install Hooks

```bash
uv run walkcode install-hooks
```

</details>

## How It Works

1. Shell wrapper starts the agent inside a tmux session
2. Agent [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on stop / permission / input needed
3. `walkcode hook` detects tmux session name and POSTs to local server
4. WalkCode creates a **chat thread** (`project | session_id | prompt` as title, content as first reply)
5. You tap a button or reply with text — real-time via WebSocket
6. `tmux send-keys` injects your response — no GUI required

## Usage

| Scenario | What You See | What You Do |
|----------|-------------|-------------|
| Permission prompt | Card with buttons | Tap **Allow** / **Deny** / **Always** |
| Waiting for input | Text in thread | Reply with text |
| Task complete | Text in thread | Reply to continue, or ignore |
| Session expired | Reply in old thread | Agent resumes automatically via `--resume` |
| Remote start | Send a message in chat | Agent starts in new tmux session |

## CLI

```bash
walkcode start                            # Start as daemon
walkcode start --log /tmp/walkcode.log    # Custom log path
walkcode stop                             # Stop daemon
walkcode restart                          # Restart daemon
walkcode status                           # Check if running
walkcode serve                            # Foreground (debug)
walkcode install-hooks                    # Install hooks
walkcode upgrade                          # Pull + reinstall + restart
walkcode test-inject <tmux-session> "hi"  # Test injection
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `FEISHU_APP_ID` | Yes | Feishu app ID |
| `FEISHU_APP_SECRET` | Yes | Feishu app secret |
| `FEISHU_RECEIVE_ID` | Yes | Your open_id or chat_id |
| `FEISHU_RECEIVE_ID_TYPE` | No | `open_id` (default) or `chat_id` |
| `WALKCODE_STATE_PATH` | No | Custom state file path |
| `WALKCODE_CWD` | No | Default cwd for remote-started sessions (default: `~/.walkcode/workspace`) |

## Roadmap

WalkCode's goal: **connect any coding agent to any chat platform.**

### Coding Agents

| Agent | Status |
|-------|--------|
| [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | Supported |
| [Codex CLI](https://github.com/openai/codex) | Planned |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Planned |
| [Cline](https://github.com/cline/cline) | Planned |
| [Aider](https://github.com/Aider-AI/aider) | Planned |
| [Copilot CLI](https://githubnext.com/projects/copilot-cli) | Planned |
| [Goose](https://github.com/block/goose) | Planned |
| [Amp](https://ampcode.com) | Planned |

### Features

| Feature | Status |
|---------|--------|
| Multi-modal messages (image, rich text, forwarded messages) | Planned |

### Chat Platforms

| Platform | Status |
|----------|--------|
| [Feishu / Lark](https://www.feishu.cn/) | Supported |
| [Slack](https://slack.com/) | Planned |
| [Telegram](https://telegram.org/) | Planned |
| [Discord](https://discord.com/) | Planned |
| [WhatsApp](https://www.whatsapp.com/) | Planned |

## Community

- [GitHub Issues](https://github.com/0x5446/walkcode/issues) — Bug reports & feature requests
- [GitHub Discussions](https://github.com/0x5446/walkcode/discussions) — Q&A & ideas

## Contributing

Issues and PRs welcome. Run `uv run pytest` before submitting.

## Disclaimer

Not affiliated with Anthropic. Claude is a trademark of Anthropic.

## License

[MIT](LICENSE)
