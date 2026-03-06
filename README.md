# Agent Hotline

[**中文文档**](README_CN.md)

**Let your AI agent call you when it needs help.**

> Human-in-the-loop for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) via [Feishu / Lark](https://www.feishu.cn/) — vibe coding anytime, anywhere.

When Claude Code needs confirmation, context, or permission, it normally blocks and waits. Agent Hotline bridges that gap: it sends you a Feishu message, you reply from your phone, and Claude keeps going — even when your screen is locked.

```
Claude Code (tmux) ──Hook──> Agent Hotline ──API──> Feishu (thread + buttons)
                    <──tmux send-keys──      <──WS── (tap / reply)
```

## Features

- **Works with screen locked** — Uses `tmux send-keys` instead of GUI automation, so injection works even when macOS is locked or you're away
- **Threaded conversations** — Each Claude Code session maps to a Feishu thread, keeping context organized
- **One-tap permissions** — Interactive cards with Allow / Deny / Always buttons for permission prompts
- **Text replies** — Reply in any thread to type directly into the correct terminal
- **Emoji receipts** — Random emoji reactions confirm delivery at a glance
- **Multi-session** — Run multiple Claude Code instances; replies route to the right terminal automatically
- **Session persistence** — Survives server restarts; sessions resume with their Feishu threads intact
- **Transparent** — A shell wrapper auto-creates tmux sessions; you just type `claude` as normal

## Quick Start

### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) and create an enterprise app
2. **Add capability** > Bot
3. **Permissions** > Enable:
   - `im:message` — Read messages
   - `im:message:send_as_bot` — Send messages
   - `im:message.reactions:write_only` — Add emoji reactions
4. **Events & Callbacks** > Long connection mode > Add event `im.message.receive_v1`
5. **Version Management** > Create version > Publish

### 2. Install

```bash
# Prerequisites
brew install tmux

# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/agent-hotline.git
cd agent-hotline
uv sync
cp .env.example .env
```

Edit `.env` with your Feishu App ID, Secret, and Verification Token.

### 3. Get Your open_id

Start the service, then send any message to your bot in Feishu:

```bash
uv run agent-hotline serve
# Log: Non-reply message from ou_xxxx (use this open_id for FEISHU_RECEIVE_ID)
```

Add `ou_xxxx` to `FEISHU_RECEIVE_ID` in `.env`, then restart.

### 4. Add Shell Wrapper

Add this to your `~/.zshrc` (or `~/.bashrc`):

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

Then reload: `source ~/.zshrc`

This transparently wraps Claude Code in a tmux session. You still just type `claude` — the wrapper handles the rest.

### 5. Install Claude Code Hooks

```bash
uv run agent-hotline install-hooks
```

Restart your Claude Code session to activate.

That's it. Type `claude`, and you'll get Feishu notifications that work even when your Mac is locked.

## How It Works

1. The shell wrapper starts Claude Code inside a tmux session
2. Claude Code [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on task stop / permission prompt / input needed
3. `agent-hotline hook` detects the tmux session name and POSTs it to the local server
4. Agent Hotline creates a **Feishu thread** (project name as title, content as first reply)
5. You tap a button or reply with text — delivered in real-time via Feishu WebSocket
6. `tmux send-keys` injects your response into the correct session — no GUI required

## Usage

| Scenario | What You See | What You Do |
|----------|-------------|-------------|
| Permission prompt | Card with buttons | Tap **Allow** / **Deny** / **Always** |
| Waiting for input | Text message in thread | Reply with text |
| Task complete | Text message in thread | Reply to continue, or ignore |

Delivery status is shown as an emoji reaction on your message.

## Multiple Sessions

Each Claude Code session auto-threads in Feishu. Reply to any message in a thread to inject into the correct terminal:

```
tmux: claude-project-a-12345  <-->  Feishu Thread "project-a | refact..."
tmux: claude-project-b-67890  <-->  Feishu Thread "project-b | add ne..."
```

One `agent-hotline` instance handles all sessions.

## CLI

```bash
agent-hotline start                            # Start as daemon
agent-hotline start --log /tmp/hotline.log     # Custom log path
agent-hotline stop                             # Stop daemon
agent-hotline restart                          # Restart daemon
agent-hotline status                           # Check if running
agent-hotline serve                            # Foreground (debug)
agent-hotline install-hooks                    # Install Claude Code hooks
agent-hotline test-inject <tmux-session> "hi"  # Test tmux injection
```

Runtime files in `~/.agent-hotline/`:

| File | Purpose |
|------|---------|
| `agent-hotline.pid` | Daemon PID |
| `agent-hotline.log` | Service log |
| `state.json` | Session persistence |

## Requirements

- macOS
- [tmux](https://github.com/tmux/tmux) (`brew install tmux`)
- [uv](https://docs.astral.sh/uv/) (Python >= 3.13)
- Feishu enterprise app (free)

## Contributing

Issues and PRs are welcome. Please run `uv run pytest` before submitting.

## Disclaimer

This project is not affiliated with Anthropic. Claude is a trademark of Anthropic.

## License

[MIT](LICENSE)
