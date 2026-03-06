# Agent Hotline

**Let your AI agent call you when it needs help.**

> Vibe coding anytime, anywhere — human-in-the-loop for Claude Code via Feishu.

When running Claude Code agents or automation, sometimes Claude needs help — confirmation, missing context, permission to proceed. Normally this blocks the task.

Agent Hotline solves this by sending you a Feishu notification. You reply with text or tap a button, and Claude continues — even when you're away from your computer.

```
Claude Code ──Hook──> Agent Hotline ──API──> Feishu (card + buttons)
             <──inject──              <──WS── (button click / text reply)
```

## Quick Start

### 1. Create a Feishu App

1. Go to [Feishu Open Platform](https://open.feishu.cn/app) and create an enterprise app
2. **Add capability** > Bot
3. **Permissions** > Enable `im:message` and `im:message:send_as_bot`
4. **Events & Callbacks** > Use long connection > Add event `im.message.receive_v1` > Confirm permissions
5. **Version Management** > Create version > Publish

### 2. Install

```bash
# Install uv (skip if already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/0x5446/agent-hotline.git
cd agent-hotline
uv sync
cp .env.example .env
```

Edit `.env` with your Feishu App ID / Secret / Verification Token. Default port is 3001 (customizable via `PORT` env var).

### 3. Get your open_id

Start the service, then send any message to your bot in Feishu. The log will print your `open_id`:

```bash
uv run agent-hotline serve
# Log output: Non-reply message from ou_xxxx (use this open_id for FEISHU_RECEIVE_ID)
```

Add `ou_xxxx` to `FEISHU_RECEIVE_ID` in `.env`, then restart.

### 4. Install Claude Code Hooks & macOS Permission

```bash
uv run agent-hotline install-hooks
```

Restart your Claude Code session to activate.

> **macOS Accessibility**: System Settings > Privacy & Security > Accessibility > Add Terminal.app. Without this, terminal injection will fail.

---

Done. Just use `claude` as normal. No tmux, no wrapper, no special setup.

## How It Works

1. Claude Code [Hooks](https://docs.anthropic.com/en/docs/claude-code/hooks) fire on task completion / input needed, `agent-hotline hook` POSTs the event to the local server
2. Agent Hotline sends an **interactive card** via Feishu API (permission prompts have buttons), same session messages auto-thread
3. You tap a button or reply with text, delivered in real-time via WebSocket
4. Agent Hotline injects your response into the correct Terminal.app tab via AppleScript clipboard paste

## Usage

| Feishu Card | Action | Effect |
|-------------|--------|--------|
| Permission prompt (red) | Tap button | Injects y/n/a |
| Waiting for input (blue) | Reply with text | Types into terminal |
| Task complete (green) | Reply with new instruction | Continues working |

Delivery confirmation is shown after each action (success or failure).

## Multiple Sessions

Each Claude Code session maps to a separate Feishu thread. Reply to any message in a thread to inject into the correct terminal:

```
Terminal Tab 1 (session abc)  <-->  Feishu Thread A
Terminal Tab 2 (session def)  <-->  Feishu Thread B
```

One `agent-hotline serve` handles all sessions.

## CLI

```bash
agent-hotline start                          # Background (log: ~/.agent-hotline/agent-hotline.log)
agent-hotline start --log /tmp/hotline.log   # Custom log path
agent-hotline stop                           # Stop
agent-hotline restart                        # Restart
agent-hotline status                         # Check status
agent-hotline serve                          # Foreground (debug)
agent-hotline install-hooks                  # Install Claude Code hooks
agent-hotline test-inject /dev/ttys003 "hi"  # Test injection
```

Runtime files in `~/.agent-hotline/`:

| File | Purpose |
|------|---------|
| `agent-hotline.pid` | Daemon PID |
| `agent-hotline.log` | Service log |
| `state.json` | Session persistence |

## Requirements

- macOS (AppleScript + Terminal.app)
- [uv](https://docs.astral.sh/uv/) (Python 3.13 managed by uv)
- Feishu enterprise app (free)

## Disclaimer

This project is not affiliated with Anthropic. Claude is a trademark of Anthropic.

## [License](LICENSE)

MIT
