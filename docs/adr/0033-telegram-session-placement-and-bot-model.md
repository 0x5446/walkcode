# ADR 0033: Telegram Session Placement and Bot Model

Date: 2026-06-29

Status: Accepted

## Context

Channel-native V3 currently routes Telegram input with:

```text
channel_kind + account_id + chat_id + thread_id + root_message_id
```

This is correct for isolation, but the visible Telegram UX is not clear enough
when multiple Claude Code or Codex sessions share one private chat. The previous
V3 prototype allowed one Telegram bot to start either Claude Code or Codex via
`/claude` and `/codex`, but that made bot identity, agent identity, and session
placement feel like three unrelated routing layers.

The product target is simpler:

```text
one Coding Agent -> one bot/app identity
one agent session -> one topic/thread
```

Telegram now has two topic-like surfaces:

- forum topics in supergroups;
- topic mode in private bot chats, when enabled for the bot.

The configured local Telegram target may still be a private chat. If the bot
does not have private topic mode enabled, that environment cannot show one
native Telegram topic per session until the bot/chat setup changes to a forum
supergroup or bot private-topic mode.

## Decision

Use an explicit session placement strategy instead of treating a reply chain as
the product model.

### Placement Preference

`topic_per_session` is the best UX whenever the channel can provide native
topics:

1. Lark/Feishu topic-capable chats: one topic per WalkCode session.
2. Telegram forum supergroup: one forum topic per WalkCode session. The bot
   must be an administrator with the `can_manage_topics` administrator right
   required by Telegram's `createForumTopic` method. For ordinary root text
   input in the group, bot privacy mode must be disabled or the message must
   otherwise be delivered to the bot.
3. Telegram private chat with bot topic mode enabled: one private bot topic per
   WalkCode session.

When native topics are not available, Telegram falls back to
`root_reply_chain`:

- one session root message anchors the session;
- replies to that root route to the session;
- rootless messages are accepted only when exactly one active session exists in
  the same chat/thread;
- when multiple active sessions exist, the runtime must render an explicit
  session chooser instead of guessing.

`group_per_session` is not the default strategy. It may be supported later as a
deployment recipe with pre-provisioned groups, but WalkCode should not create or
manage a new Telegram group per coding task by default.

For group deployments with privacy mode disabled, one forum supergroup per
agent bot is the cleanest shape. A shared forum group with multiple agent bots
can be supported only with stricter mention/reply discipline; otherwise multiple
bots may consume the same root message.

### Bot and Agent Model

Separate these concepts:

- `Channel`: the IM surface, selected per runtime instance
  (`WALKCODE_CHANNEL=telegram|lark`).
- `Bot/App identity`: the Telegram bot token or Lark app credentials used by
  that runtime instance.
- `Agent`: the coding backend product, currently `claude` or `codex`.
- `Session`: one durable Claude Code or Codex conversation with one placement.

V3 uses agent-dedicated bots. One runtime instance binds one bot/app identity to
one Coding Agent:

```text
Telegram bot A -> WalkCode instance A -> WALKCODE_AGENT=claude
Telegram bot B -> WalkCode instance B -> WALKCODE_AGENT=codex
```

That profile requires separate env files and state paths. `/claude` and
`/codex` are not routing commands inside a shared bot. In a Claude bot, a text
message starts or continues Claude. In a Codex bot, a text message starts or
continues Codex. If an old agent selector command is sent, the runtime replies
with guidance and does not start a turn.

## Consequences

- Product copy must say that V3 uses one bot/app identity per Coding Agent.
- V3 docs must explain that one runtime instance has exactly one channel, one
  bot/app identity, and one Coding Agent.
- Telegram docs must show two setup levels:
  - private chat: works immediately, but uses reply-chain fallback unless
    private topic mode is enabled;
  - forum supergroup: recommended for the clearest Telegram UX because each
    session can become a visible topic, whether the session starts from IM or
    is first observed from a local TUI hook.
- Runtime implementation should keep a placement negotiation step before a new
  session is created:
  - inspect the target chat and bot capabilities;
  - create or select a native topic when possible;
  - persist the resulting `thread_id` in `ChannelBinding`;
  - fall back to `root_reply_chain` with an explicit chooser for ambiguity.
- Existing `ChannelBinding` remains the right durable identity. Placement
  changes how `thread_id` and `root_message_id` are obtained; it does not add
  platform-specific session fields to core state.
- `ChannelNativeConfig` exposes a single `agent` value for the current bot. The
  runtime only wires that agent's transport.
- Empty Telegram system messages are acknowledged without creating a topic or
  agent session.

## Verification Plan

The implementation should be split into module-level tests:

1. Telegram capability probe parses `getMe` and `getChat` without leaking token
   or chat id.
2. Forum topic placement creates a topic, stores `message_thread_id`, and sends
   the session root inside the topic.
3. TUI hook observation creates the observed-session root inside a forum topic
   when the configured Telegram chat supports topics.
4. Private topic placement does the same when `has_topics_enabled=true`.
5. Reply-chain fallback keeps current routing and shows a chooser on ambiguous
   rootless input.
6. Agent selector commands such as `/claude` and `/codex` are rejected with a
   clear message and do not start another agent through the same bot identity.
7. Empty Telegram messages are consumed without creating sessions.
