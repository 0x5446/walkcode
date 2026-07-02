# ADR 0040: Telegram Topic Session and TUI Takeover UX

Date: 2026-06-30

Status: Accepted

## Context

WalkCode V3 runs one local runtime as one channel, one bot, and one coding
agent. For Telegram forum supergroups, one agent session maps to one Telegram
topic.

Telegram identifies a forum topic in Bot API payloads as `message_thread_id`,
not `topic_id`. `chat_id` identifies the containing chat or group. For native
topic placement, WalkCode's primary channel binding identity is therefore:

```text
chat_id + message_thread_id
```

`root_message_id` is only a reply-chain fallback for surfaces without native
topics, such as private chats or non-forum groups. It must not be treated as the
product session identity inside a Telegram forum topic.

TUI-origin sessions are different from Telegram-origin sessions. A TUI-origin
topic is created by observing a local Claude Code or Codex TUI process through
hooks. Telegram is initially a read-only observation surface. If a Telegram user
types in that topic, the input must be held behind an explicit takeover prompt.

## Decision

Telegram topics are never closed or reopened as the default read-only UX.
WalkCode no longer calls `closeForumTopic` or `reopenForumTopic` for ordinary
TUI observation. Those methods create Telegram system tips such as
"Closed Topic" and "Reopened Topic", which confuse users and do not reliably
express the intended writer ownership model.

Each topic binding records origin-like capability metadata:

- `origin=telegram` for Telegram-created read/write topics.
- `origin=external_tui` for TUI-observed topics.
- `native_topic=true` when `message_thread_id` is the primary route.
- `static_status_card=true` for Telegram topic status cards.

The first status card in a newly created Telegram topic is a fixed session
information card. It is not continuously edited as progress changes. Progress
feedback uses Telegram-native typing, message reactions, tool/activity messages,
takeover cards, and final agent replies.

For Telegram-created sessions, the initial General/root-chat user input is
submitted to the agent before status-card creation and pinning. Topic creation
and status UI are user feedback; they must not block the actual first turn long
enough for the writer lease to expire.

If an agent transport returns partial output but never yields `turn.completed`,
WalkCode releases the writer lease and marks the session `ERROR_RECOVERABLE`.
That keeps the topic recoverable by the next user input instead of leaving it
stuck in `ACTIVE` with an expired lease.

When a takeover is started from the status card instead of a user message,
WalkCode resumes the structured transport without submitting an empty turn.
Because the status card is static, successful takeover renders a separate
`Takeover completed` progress message so the user can see that the topic is now
writable.

When a user sends input in a TUI-origin topic:

1. WalkCode stores that exact input as a blocked input.
2. WalkCode renders a single-step takeover prompt bound to that blocked input.
3. If the user sends more messages before choosing, each message gets its own
   takeover prompt and its own blocked input.
4. The prompt the user clicks decides which blocked input is submitted.
5. Clicking `Take over and send` performs the takeover immediately: first
   validate structured resume by creating a provisional headless handle, then
   terminate the external TUI if a live TUI writer still exists, then commit
   writer ownership and submit the blocked input. There is no second
   confirmation card, no `Keep read-only` action, and no user-facing
   `Manual steps` action.
6. After successful takeover, stale prompts from the previous session generation
   are rejected.

Status-card `Take over` is idempotent per `session_id + generation`. A repeated
click on the same observed session reuses the existing takeover transaction and
does not create another `takeover-only` blocked input or replay progress
messages.

If the external TUI process is still active, automatic takeover requires an
explicit termination boundary after structured resume has been validated.
Claude Code `Stop` is a turn-completion hook, not proof that the TUI process
exited. Therefore a `Stop` hook does not mark the observed session stopped and
does not remove the need to terminate a live TUI process before takeover. Only
explicit process-exit style hooks may mark a TUI observation stopped.

Runtime startup reconciles observed TUI topics against their stored process
reference:

- `EXTERNAL_OBSERVED_READONLY` means a verified TUI process still owns writer
  control and Telegram remains read-only until takeover.
- `EXTERNAL_DETACHED_IMPORTABLE` means the stored TUI process is gone but a
  durable resume reference remains. Telegram can take over without terminating a
  local TUI process.
- `EXTERNAL_DETACHED_UNIMPORTABLE` means the stored TUI process is gone and no
  durable resume reference exists. Telegram keeps the topic visible but cannot
  automatically continue that agent session.

`terminate_ref` is a live-writer lease hint, not a durable fact. A stale pid
must not be treated as successful termination of the current TUI owner.
`resume_ref` is also a candidate reference; takeover marks the transaction
failed and leaves writer ownership unchanged if the transport cannot resume it.

For local TUI hooks, WalkCode captures both the hook process group and the
parent process tree. The process group is preferred for termination inference
because the terminal foreground job normally uses the TUI process id as the
process group id. Shell hook commands that merely contain the words `claude` or
`codex` are not treated as TUI processes.

After takeover completes, subsequent global hooks from the WalkCode-resumed
headless process must not move the topic back to `external_tui` ownership or
send the same assistant reply again. Those hooks are considered internal
self-observation for claim-capable events unless they include verifiable
external TUI process identity; late non-claiming events remain no-op if no
observed external TUI session owns the topic.

Missing resume information is not a normal user flow. It is treated as a broken
or incomplete observed-session state and rendered as a clear
`Cannot take over automatically` failure. The failure is informational only; it
does not expose manual-step buttons in the normal Telegram UX.

Telegram service messages for topic creation, closing, reopening, editing, and
General topic visibility are acknowledged as Telegram service events. They are
not routed to an agent and must not block later user messages in the polling
offset queue.

Stopped rootless sessions must not capture new General messages. A rootless
stopped binding means old state, not an active conversation target.

## Consequences

- TUI-created topics stay visually stable and do not show confusing Telegram
  close/reopen system tips.
- Users can continue a TUI-origin topic from Telegram through an explicit,
  per-message takeover prompt.
- General remains a task inbox; old stopped rootless state cannot prevent new
  sessions from being created.
- Telegram polling can safely confirm topic service updates and move on to real
  user input.
- Status cards are informational anchors, not live dashboards. Live activity is
  shown through separate activity surfaces.
- Startup reconciliation prevents old observed topics from pretending that a
  dead TUI process is still active.
- Resume failures do not kill a live TUI process or submit the blocked Telegram
  input.

## Verification

Focused tests cover:

- stopped rootless sessions do not capture new General messages;
- Telegram topic service messages are acknowledged without agent routing;
- TUI-origin topic input creates a single-step takeover prompt and the accepted
  action resumes the structured transport;
- Claude Code `Stop` hook output is forwarded without marking the session
  stopped or dropping the TUI termination reference;
- process-group based hook inference captures a real TUI pid for automatic
  termination, and shell hook commands are not treated as TUI processes;
- TUI-origin readonly input is not deleted after the user sends it;
- TUI topic capability backfill sets `static_status_card`, marks
  `origin=external_tui`, and removes stale `topic_closed` state left by older
  readonly implementations;
- TUI topic capability backfill restores `EXTERNAL_OBSERVED_READONLY` for old
  state that had been incorrectly marked `ACTIVE` by tool observation events;
- Telegram no longer calls `closeForumTopic` or `reopenForumTopic` in the
  default readonly flow.
- takeover-resumed sessions ignore late self-observation hooks from the
  WalkCode-owned headless process, so the topic remains writable and replies are
  not duplicated through the external TUI path.
- stale TUI pids are reconciled to detached importable/unimportable states;
- repeated status-card takeover clicks after a failure do not create duplicate
  takeover transactions or duplicate progress messages;
- takeover now validates structured resume before terminating a live external
  TUI process, and resume failure leaves the TUI untouched.
