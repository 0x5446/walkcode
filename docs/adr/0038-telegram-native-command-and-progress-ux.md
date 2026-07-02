# ADR 0038: Telegram Native Commands, Processing Acks, and Tool Progress UI

Date: 2026-06-29

Status: Accepted

## Context

Telegram users expect slash commands to be bot-native controls. If `/status` or
`/model` is forwarded as ordinary agent input, Claude/Codex may answer with
agent-specific "command unavailable" text even though WalkCode could have
handled the control locally.

Telegram message check marks are client-level delivery/read UI. WalkCode cannot
turn a user's single check mark into a Telegram-native double check mark through
Bot API. The product still needs a visible "received and processing" signal
inside the same chat/topic.

Agent tool calls also need visible progress. Dumping every tool output into the
topic is noisy and can expose too much raw terminal data. The better IM pattern
is a compact, editable progress surface that says what kind of tool work is
running and whether it completed or failed.

## Decision

Telegram runtime installs a native bot command menu with `setMyCommands`.
Because V3 uses one bot per coding agent, the command menu is agent-specific:
the Claude bot installs WalkCode controls plus known Claude-native slash
commands, while the Codex bot installs WalkCode controls plus known Codex-native
slash commands. The menu is generated from a command catalog and kept under the
Telegram 100-command API limit.
Telegram command names cannot contain hyphens, so hyphenated agent commands are
registered with underscore aliases, for example `/add_dir`; inside a session
WalkCode forwards that input to the agent as `/add-dir`.

WalkCode intercepts these commands before agent submission:

- `/status`: render the current session status card when sent inside a session
  topic or reply chain; otherwise render runtime-level status.
- `/sessions`: list active Telegram sessions in the current chat.
- `/model [value]`: show model-switch capability plus a local model inventory,
  or call the transport's model control when a supported session transport
  exposes it. Claude inventory is derived from `WALKCODE_CLAUDE_SETTINGS`.
  Codex inventory is derived from `WALKCODE_CODEX_CONFIG` and
  `WALKCODE_CODEX_MODELS_CACHE` when set, otherwise `~/.codex/config.toml` and
  `~/.codex/models_cache.json`.
- `/skills`: show current skill-introspection support. It is WalkCode-owned and
  must not be forwarded blindly to the agent.
- `/takeover`: request takeover for a TUI-origin topic.
- `/commands`: show the installed WalkCode and agent command catalog.

Unknown slash commands are treated as agent-native only when they are sent
inside an existing session topic or reply chain. Unknown slash commands in the
General/root chat are rejected instead of creating a new session, because root
chat is the task inbox and cannot know which agent session should receive the
command.

The polling runtime must prioritize `getUpdates` and inbound routing before
best-effort maintenance work. `setMyCommands` is useful for discovery, and TUI
observed-session refresh / deferred-hook drain are useful for read-only mirror
freshness, but failures or slow operations in those paths must not delay or
block user input consumption.

For live polling, WalkCode confirms a Telegram update after the user turn has
been submitted to the agent transport. It does not wait for the whole
Claude/Codex turn to finish before confirming the Telegram offset. Agent output,
tool activity, permission prompts, and final replies continue draining in the
background. This keeps one long-running agent turn from starving later Telegram
updates.

For user text that WalkCode receives, the runtime best-effort calls
`setMessageReaction` with `✅` on the user's message. This is the WalkCode-owned
receipt marker. For accepted user input, WalkCode also sends Telegram
`sendChatAction(typing)` to the target chat/topic before agent submission. These
are Bot API-level processing acknowledgements. They do not claim to alter
Telegram read receipts or client check marks.

Agent transports may emit neutral tool lifecycle events:

- `tool.started`
- `tool.completed`
- `tool.failed`

Claude SDK tool-use/tool-result blocks, direct SDK tool blocks, Codex app-server
tool-like events, Codex command-execution item events, and observed TUI tool
hooks are converted into those neutral events. The Telegram UI renders them as
one compact tool progress message per session, stored in
`ChannelBinding.capabilities.tool_progress_message_id`, and edits that message
as tool state changes. Full tool output is intentionally not rendered in the
progress card; final agent text still arrives through normal turn output.
The Telegram text surface labels this message as `Agent activity` to distinguish
tool/thinking progress from ordinary agent replies. There is no `/progress`
toggle; the activity surface is a default part of the Telegram session UX.

When creating a Telegram forum topic, WalkCode randomizes the topic icon. It
first tries `getForumTopicIconStickers` and `icon_custom_emoji_id`; if that is
not available, it falls back to a random allowed `icon_color`.

## Consequences

- Telegram slash commands now behave like bot controls instead of accidental
  agent prompts.
- Claude and Codex bots expose their own command menus instead of sharing one
  generic selector bot command surface.
- Command-menu installation is best-effort after polling, so menu sync failures
  cannot starve inbound Telegram updates.
- TUI observed-session refresh and deferred-hook drain run after polling as
  bounded best-effort maintenance, so TUI hook bursts cannot starve inbound
  Telegram updates.
- Long-running agent turns do not block Telegram offset confirmation after
  transport submission succeeds.
- Users get an immediate message-level ACK reaction and an in-topic processing
  cue after WalkCode receives input.
- Agent activity becomes visible without spamming raw command output.
- Topic icons are visually distinct, which helps scan many session topics.
- Lark remains a peer adapter target. Its equivalent should map the same neutral
  command/tool progress view models to Lark topic/thread cards.

## Verification

Focused tests cover:

- `/status` is handled locally and not sent to the agent;
- `/model` does not leak into unsupported transports;
- `/model` lists Claude configured models and Codex locally cached models;
- unknown slash commands outside a session are rejected, while unknown slash
  commands inside a session are passed to the agent transport;
- `setMyCommands` is called by Telegram polling service startup after the first
  inbound poll;
- live polling calls `getUpdates` before TUI observed-session refresh and
  deferred-hook drain;
- live polling confirms offsets after turn submission even if the agent event
  stream has not finished yet;
- `/commands` renders the installed command catalog;
- accepted input sends `sendChatAction(typing)`;
- received text input best-effort sets a `✅` message reaction;
- forum topic creation sends a randomized icon payload;
- Claude SDK tool-use/tool-result blocks convert to tool lifecycle events;
- direct Claude SDK tool blocks convert to tool lifecycle events;
- Codex tool-like and command-execution item events convert to tool lifecycle
  events;
- TUI `PreToolUse`/`PostToolUse` hooks convert to tool lifecycle events;
- Telegram edits one tool progress card instead of sending raw tool output.
