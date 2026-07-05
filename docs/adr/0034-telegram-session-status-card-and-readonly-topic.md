# ADR 0034: Telegram Session Status Card and Read-only Topic UX

Date: 2026-06-29

Status: Superseded by ADR 0040 for Telegram topic close/reopen and live status
card behavior.

Amended: 2026-06-29
Superseded: 2026-06-30

ADR 0040 changes the default Telegram behavior: status cards in Telegram topics
are static informational anchors, and WalkCode no longer calls
`closeForumTopic` / `reopenForumTopic` or deletes user messages for the default
TUI read-only UX. The historical decision below remains as context for why the
status card exists.

## Context

Telegram forum topics give WalkCode a native surface for one agent session per
topic, but two UX gaps remained:

- a topic did not have a stable root/status card showing what task it belonged
  to, whether it was running, waiting, idle, stopped, or read-only;
- a TUI-observed session was logically read-only in WalkCode, but Telegram still
  showed a normal input box, so users could type into a session that could not
  accept input until takeover.

The old WalkCode Feishu/Lark implementation had a useful topic root card. It
was not reused as code, but its information structure and update timing remain
the right product shape:

- one stable root card per session/thread;
- update on session start, progress, permission/user wait, takeover, stop, and
  timeout/stale evidence;
- show title, session id, model/agent, project context, duration, current
  status, and last progress;
- use the card as the user's control surface.

Telegram Bot API has `createForumTopic`, `closeForumTopic`, and
`reopenForumTopic` for forum topics, and inline keyboard callbacks for buttons.
It does not provide a per-topic `setChatPermissions` equivalent; chat
permissions and user restrictions are chat/user level, not topic level.
In practice, `closeForumTopic` is also not a hard input-disable guarantee for
the group owner or administrators. WalkCode therefore treats it as a UX hint,
not as the read-only safety boundary.

## Decision

V3 introduces a per-session status card for native topic placements.

The product session remains the Claude Code or Codex session. Telegram routing
uses the topic's `message_thread_id`; `root_message_id` is only a reply-chain
fallback helper. In native topic mode, a reply to any message inside the topic
must continue the same agent session by `chat_id + message_thread_id`, even
when `reply_to_message.message_id` is not the status card.

For Telegram forum supergroups:

- General/root chat text is treated as an inbox/start signal.
- If the bot can create topics, WalkCode creates a session topic before the
  agent session starts.
- After successful topic creation, WalkCode keeps the original General launch
  message as an audit/start record and sends a short General reply saying which
  session topic was created. For private supergroups, the reply may include a
  best-effort `t.me/c/...` topic link.
- The General launch text is submitted to the agent before status-card or pin
  UI work. Slow Telegram status-card delivery must not consume the writer lease
  and leave a newly created topic idle.
- Topic creation best-effort randomizes the Telegram topic icon, preferring
  default custom emoji topic icons and falling back to Telegram's allowed topic
  colors.
- The first WalkCode-owned message in the new topic is the status card.
- `ChannelBinding.health_message_id` stores the card message id.
- Later status updates edit the card via `editMessageText` instead of sending a
  new card.
- If edit fails, WalkCode degrades by sending a fresh card and replacing
  `health_message_id`.
- The card may be pinned best-effort where the bot has permission.

For TUI-observed sessions:

- the status card shows that the writer is `external_tui` and input is
  read-only until takeover;
- the card includes a `Take over` inline button;
- WalkCode best-effort calls `closeForumTopic` for the observed topic, which is
  the closest Telegram-native way to make the input box unavailable for that
  topic;
- if Telegram still lets an owner/admin send text into that topic, WalkCode's
  writer gate still blocks the input, opens the takeover flow, and
  best-effort deletes the just-sent blocked message to reduce UI ambiguity;
- after a successful automatic takeover, WalkCode best-effort calls
  `reopenForumTopic`;
- `/takeover` remains a command fallback when the topic is open, but it is not
  the primary UX because a closed topic may not allow new text input.

## Consequences

- Telegram topic UI now has one durable place to inspect task state, progress,
  duration, cwd, writer ownership, and takeover affordance.
- The General topic remains a lightweight task index instead of making the
  user's just-sent launch message disappear.
- TUI read-only state is visible in the Telegram client as much as Bot API
  permits, but the authoritative enforcement remains WalkCode's writer/takeover
  state machine.
- Status cards are opt-in per binding through `binding.capabilities.status_card`
  so older reply-chain fallback tests and non-topic channels are not forced into
  a new visual contract.
- TUI hook claims backfill status/read-only topic capabilities on older
  persisted Telegram bindings whose `capabilities` map was empty, so existing
  observed sessions get the same status card and takeover UX as newly created
  topics.
- Telegram polling startup also refreshes active TUI-observed bindings once, so
  old persisted sessions do not need to wait for another hook event before their
  status card can appear.
- Lark remains a peer channel. Its equivalent should use the same
  `health_message_id`/edit boundary, mapped to Lark topic thread card updates.

## Verification

Module tests cover:

- native Telegram topic replies route by `message_thread_id`, not by
  `root_message_id`;
- historically, status cards were created once and edited on progress; ADR 0040
  changes Telegram topic cards to static anchors;
- TUI status card buttons open the takeover prompt without requiring a typed
  input;
- existing TUI-observed Telegram sessions with empty binding capabilities are
  repaired on the next hook update or Telegram polling startup;
- Telegram runtime creates a status card for observed TUI sessions and keeps
  final output delivery intact;
- General launch messages are preserved and acknowledged with a session-topic
  navigation notice.
