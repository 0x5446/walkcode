# WalkCode

[**中文文档**](README_CN.md)

> **Code is cheap. Show me your talk.**

Being chained to your desk isn't vibe coding. **Anytime, anywhere** — that's vibe coding.

WalkCode turns your IM into a remote control for AI coding agents. Your agent codes, you walk. When it needs help, it pings your phone. You reply with a tap or a sentence. It keeps going. You keep walking.

**Yap to code. Code while you walk. That's WalkCode.**

```
Coding Agent (tmux) ──Hook──> WalkCode ──API──> IM (thread + buttons)
                     <──tmux send-keys──  <──WS── (tap / reply)
```

## Why WalkCode?

You're on a walk. Your AI agent hits a permission prompt. It needs a "yes" to continue.

**Without WalkCode:** It blocks. You come back 30 minutes later. Momentum lost.

**With WalkCode:** Your phone buzzes. You tap "Allow". Agent keeps shipping. You keep walking.

This is **Yap Coding** — you talk, it codes. No keyboard, no screen, no desk. Just you and your phone.

## Features

- **Works with screen locked** — `tmux send-keys` injection, no GUI dependency
- **Threaded conversations** — Each agent session maps to an IM thread
- **One-tap permissions** — Interactive cards with Allow / Deny / Always buttons
- **Text replies** — Reply in a thread to type directly into the agent's terminal
- **Remote start** — Send a message to start a new coding agent session from your phone
- **Emoji receipts** — Random emoji reactions confirm delivery at a glance
- **Multi-session** — Multiple agents, one instance, auto-routing
- **Session persistence** — Survives server restarts

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
brew install tmux
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/agent-hotline.git
cd agent-hotline
uv sync
cp .env.example .env
```

Edit `.env` with your Feishu App ID, Secret, and Verification Token.

### 3. Get Your open_id

```bash
uv run walkcode serve
```

Send any message to your bot in Feishu, check logs for `open_id`, add to `FEISHU_RECEIVE_ID` in `.env`, restart.

### 4. Add Shell Wrapper

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

### 5. Install Hooks

```bash
uv run walkcode install-hooks
```

That's it. Type `claude` and go for a walk.

## How It Works

1. Shell wrapper starts the agent inside a tmux session
2. Agent [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on stop / permission / input needed
3. `walkcode hook` detects tmux session name and POSTs to local server
4. WalkCode creates an **IM thread** (project name as title, content as first reply)
5. You tap a button or reply with text — real-time via WebSocket
6. `tmux send-keys` injects your response — no GUI required

### Remote Start

Send a message in your IM chat to start a new agent session remotely:

1. WalkCode creates a tmux session with `claude "<your message>"`
2. Replies in thread confirming session started
3. Subsequent hooks auto-link to the same thread

## Usage

| Scenario | What You See | What You Do |
|----------|-------------|-------------|
| Permission prompt | Card with buttons | Tap **Allow** / **Deny** / **Always** |
| Waiting for input | Text in thread | Reply with text |
| Task complete | Text in thread | Reply to continue, or ignore |
| Remote start | Send a message | Agent starts in new tmux session |

## CLI

```bash
walkcode start                            # Start as daemon
walkcode start --log /tmp/walkcode.log    # Custom log path
walkcode stop                             # Stop daemon
walkcode restart                          # Restart daemon
walkcode status                           # Check if running
walkcode serve                            # Foreground (debug)
walkcode install-hooks                    # Install hooks
walkcode test-inject <tmux-session> "hi"  # Test injection
```

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `FEISHU_APP_ID` | Yes | Feishu app ID |
| `FEISHU_APP_SECRET` | Yes | Feishu app secret |
| `FEISHU_RECEIVE_ID` | Yes | Your open_id or chat_id |
| `FEISHU_VERIFICATION_TOKEN` | Yes | Feishu verification token |
| `FEISHU_RECEIVE_ID_TYPE` | No | `open_id` (default) or `chat_id` |
| `WALKCODE_STATE_PATH` | No | Custom state file path |
| `WALKCODE_CWD` | No | Default cwd for remote-started sessions |

## Roadmap

WalkCode's goal: **connect any coding agent to any IM.**

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

### IM Platforms

| Platform | Status |
|----------|--------|
| [Feishu / Lark](https://www.feishu.cn/) | Supported |
| [Slack](https://slack.com/) | Planned |
| [Telegram](https://telegram.org/) | Planned |
| [Discord](https://discord.com/) | Planned |
| [WhatsApp](https://www.whatsapp.com/) | Planned |

## Community

- [GitHub Issues](https://github.com/0x5446/agent-hotline/issues) — Bug reports & feature requests
- [GitHub Discussions](https://github.com/0x5446/agent-hotline/discussions) — Q&A & ideas

<!-- TODO: Add Discord/Telegram community link -->

## Requirements

- macOS
- [tmux](https://github.com/tmux/tmux) (`brew install tmux`)
- [uv](https://docs.astral.sh/uv/) (Python >= 3.13)
- Feishu enterprise app (free)

## Contributing

Issues and PRs welcome. Run `uv run pytest` before submitting.

## Disclaimer

Not affiliated with Anthropic. Claude is a trademark of Anthropic.

## License

[MIT](LICENSE)
