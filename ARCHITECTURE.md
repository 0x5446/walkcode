# WalkCode Architecture

This document describes the internal design of WalkCode for developers who want to understand, extend, or contribute to the codebase.

## Table of Contents

- [Core Abstraction: Three-Layer Mapping](#core-abstraction-three-layer-mapping)
- [Session Lifecycle](#session-lifecycle)
- [Session Resume](#session-resume)
- [Hook Communication Protocol](#hook-communication-protocol)
- [Message Routing](#message-routing)
- [State Persistence](#state-persistence)
- [Idle Reaper](#idle-reaper)
- [i18n](#i18n)


---

## Core Abstraction: Three-Layer Mapping

WalkCode connects three systems that each use their own identifiers:

```
Feishu thread           Claude Code             tmux
─────────────           ───────────             ────
root_msg_id      ←→     session_id       ←→     session name (tty)
(permanent)             (stable)                (ephemeral)
```

### Stability

| Identifier | Lifetime | Notes |
|------------|----------|-------|
| Feishu `root_msg_id` | Permanent | The root message of a thread; never changes |
| Claude `session_id` | Stable across restarts | Represents a conversation; survives `--resume` |
| tmux session name | Ephemeral | Format: `walkcode-{unix_ts}` or `claude-{project}-{pid}`; changes every time a new session is created |

### Why session_id is the stable anchor

The tmux session name cannot be the stable key because it changes every time Claude restarts. The Feishu `root_msg_id` cannot be the primary key either, because the state store must be keyed by something that arrives with hooks (Claude fires hooks, not Feishu). Claude's `session_id` is the natural stable identifier — it represents the conversation and persists through process restarts via `--resume`.

### Lookup chain

```
Feishu reply arrives
  → root_msg_id
  → _root_to_session[root_msg_id]  →  session_id   (reverse index in SessionStore)
  → _sessions[session_id].tty      →  tmux name    (always current — see below)
  → PermissionRequest hook returns updatedInput.answers to Claude
```

### tty is always kept current

A `SessionStart` hook fires the moment Claude starts (including `--continue` and `--resume`), calling `POST /hook/sync` with the current `session_id` and tmux name. This ensures the `session_id → tty` mapping is updated **before** Claude processes any user input or tools. Subsequent hooks (`Stop`, `Notification`, `PermissionRequest`) also carry the tmux name and call `session_store.upsert()`, keeping the mapping current throughout the session.

### Eviction when a tmux is reused

A single tmux session is often reused by multiple `session_id`s — typically when the user runs `/clear` or restarts Claude inside an existing tmux. Without protection, every prior `session_id` would keep pointing at the shared tmux, and a Feishu reply on an older thread would inject into whichever Claude is currently active there — silently routing messages to the wrong session. `session_store.upsert()` therefore clears the `tty` field of any other `session_id` that previously held the same tmux name. Older threads then fall through to the dead-tty branch in `_load_reply_session` and trigger `_resume_agent`, which spins up a fresh tmux via `--resume` and keeps the conversation in its original Feishu thread.

---

## Session Lifecycle

### Creation paths

**Path A — Local (terminal-initiated):**

```
User runs `claude` in terminal
  → Shell wrapper detects no $TMUX → creates tmux session: claude-{project}-{pid}
  → Claude runs inside tmux
  → Hook fires → receive_hook() creates Feishu thread
  → session_store.upsert(session_id, tty, cwd, root_msg_id)
```

**Path B — Remote (Feishu-initiated):**

```
User sends message to Feishu bot
  → _on_message() detects no parent/root_id → _start_claude(text, message_id)
  → tmux new-session -d -s walkcode-{ts} "cd '{cwd}' && claude --permission-mode default '{prompt}'"
  → _pending_roots[tmux_name] = message_id
  → First hook arrives → pending_root matched → session_store.upsert(..., root_msg_id=message_id)
  → Launch reply edited to include session_id[:8]
```

The pending period (between tmux creation and first hook) is tracked by `SessionStore` and **persisted to `state.json`**, so pending sessions survive server restarts:

```python
# Inside SessionStore (persisted to disk)
_pending: dict[str, dict]            # tmux_name → {"root_msg_id": str, "reply_id": str|None}
_pending_msg_to_tty: dict[str, str]  # root_msg_id → tmux_name (rebuilt from _pending on load)
```

These are consumed atomically when the first hook arrives (`session_store.pop_pending(tty)`).

### Activity tracking

`Session.created_at` is not a creation timestamp — it is a **last-active timestamp**. It is updated by:

- `session_store.upsert()` — on every hook (Claude is working)
- `session_store.touch()` — on every successful user reply (user is interacting)

This dual-direction update means `created_at` accurately reflects the last time either side was active.

### Termination

A tmux session ends when:

1. Claude process exits naturally (after completing a one-shot task)
2. User sends `/exit` via Feishu reply → injected into terminal → Claude exits
3. Idle reaper kills it after 2 hours of inactivity (see [Idle Reaper](#idle-reaper))
4. Server reboot (tmux does not survive reboots)

---

## Session Resume

When a user replies in a Feishu thread whose tmux session no longer exists:

```
User replies in dead thread
  → _on_message() → _load_reply_session(session_id)
  → validate_target(session.tty) fails
  → _load_reply_session returns (session_data, error)  ← session data preserved
  → _resume_claude(session_id, old_session, reply_text, message_id)
    → tmux new-session -d -s walkcode-{ts} "cd '{cwd}' && claude --resume '{session_id}' ..."
    → session_store.upsert(session_id, tty=new_tmux_name, root_msg_id=old_root)
    → _reply(message_id, "🔄 Session resumed...")
    → threading.Thread: sleep(3) → inject(new_tmux_name, reply_text)
```

Key design decisions:

- **`claude --resume {session_id}`** is used instead of `--continue` for precision — `--continue` picks the most recent conversation in the cwd, which could be wrong if multiple sessions share a directory; `--resume` targets the exact conversation.
- **The Feishu thread is reused** — `root_msg_id` is preserved in `upsert()`, so the thread mapping stays intact. The user sees a new "Session resumed" message in the existing thread.
- **Delayed inject** — Claude needs ~3 seconds to initialize before it can accept input. A daemon thread handles the delay without blocking the WebSocket event handler.
- **Sessions never expire from state** — there is no TTL on `SessionStore` entries. This ensures resume is always possible, regardless of how long the session has been idle. Storage cost is negligible (a few dozen bytes per session).
- **Card actions do not trigger resume** — if a user clicks a permission button on an old card, it returns an error toast. Resume is only triggered by text replies, which carry meaningful context.

---

## Inject Delivery Confirmation

Feishu replies are delivered into the TUI by injecting keystrokes with `tmux
send-keys` (bracketed paste + Enter). A successful `send-keys` only proves the
bytes reached the pane's pty — it is **not** proof the TUI consumed the Enter as
a submit. A loaded or user-attached pane can drop that Enter, leaving the text
sitting in the input box (observed in the wild: a message stuck for minutes on an
idle pane until the user pressed Enter by hand, while walkcode had already shown a
success emoji).

Delivery is therefore **closed-loop and synchronous** in `_inject_live`
(`tty.verify_submitted`), by reading the pane — no reliance on hooks:

```
_inject_live(session_id, tty, text, message_id)
  → inject(tty, text)                          # paste + one Enter; raise → "not delivered"
  → verify_submitted(tty, text)                # capture pane, inspect bottom input box:
       INPUT_HAS_OURS  → re-send a BARE Enter (never re-paste) and recheck, up to 3×
       INPUT_EMPTY     → submitted/queued
       INPUT_MENU/OTHER/UNKNOWN → can't confirm, must not press Enter
  → decision:
       EMPTY                 → success emoji + register (non-authoritative) observation
       STUCK and turn busy   → queued/racing → success emoji
       STUCK and idle        → honest "not submitted" reply + failure emoji
       MENU/OTHER/UNKNOWN     → fall back to the send-keys boundary (success emoji)
```

Key properties:

- **Only the bottom-most input box is inspected** (`_extract_input_box`), so a
  submitted prompt echoed back into the transcript above — which also carries a
  `>`/`❯` and the original text — is never mistaken for an unsent draft. Two
  framings are parsed: the corner box `╭ │ ╰` (codex / older builds) and the two
  horizontal rules `── / ❯ / ──` bracketing the prompt (current Claude Code).
- **A redundant Enter is harmless** (an empty box submit is a no-op) but a second
  paste would duplicate text, so the retry path only ever sends a bare Enter.
- **A real menu/permission dialog must never get a stray Enter** (it would pick
  the default). Menus are detected by their confirm phrase, and short replies
  (e.g. `2`) require an exact box match so `2` can't substring-match a `2. No`
  option.
- **Busy ≠ lost.** A turn running (footer `… esc to interrupt`, or hook-derived
  busy state) means a still-occupied box is a *queued* prompt, not a failure.

The `UserPromptSubmit` hook is **kept only as best-effort observation** (logging),
never as the authoritative delivery verdict. The single-key permission-answer path
is not verified this way — it targets a prompt overlay on purpose.

### Double-instance detection

When `SessionStart` (`/hook/sync`) drifts a `session_id`'s tty to a *new* tmux
while the *old* one is still a live agent (e.g. a manual `claude --resume` of an
id already running under a Feishu-launched pane), two processes may be writing the
same rollout. `_alert_double_instance` warns once per distinct `(session_id,
old_tty, new_tty)` drift on the old thread — it does **not** auto-kill (killing
the wrong pane loses work). It runs off the asyncio event loop (the liveness probe
and `_reply` block) and reserves the dedupe key under a lock before delivering, so
concurrent syncs can't double-alert and a failed/absent delivery doesn't
permanently suppress the warning.

---

## Hook Communication Protocol

Claude Code hooks call `walkcode hook {stop|notification|permission-request|sync|user-prompt-submit|post-tool|subagent-start|subagent-stop|task-created|task-completed}` which reads JSON from stdin and communicates with the WalkCode server.

### Hook types

| Hook | Claude Event | Endpoint | Feishu Format | Blocking |
|------|-------------|----------|---------------|----------|
| `sync` | SessionStart | POST /hook/sync | None (mapping update only) | No |
| `user-prompt-submit` | UserPromptSubmit | POST /hook/prompt | None (best-effort observation + busy state) | No |
| `stop` | Stop | POST /hook | Plain text | No |
| `post-tool` | PostToolUse | POST /hook/post-tool | None (progress / stale HITL cleanup) | No |
| `subagent-start` / `subagent-stop` | SubagentStart / SubagentStop | POST /hook/progress | None (running progress only) | No |
| `task-created` / `task-completed` | TaskCreated / TaskCompleted | POST /hook/progress | None (running progress only) | No |
| `notification` | Notification (elicitation_dialog) | POST /hook | Plain text or interactive card (AskUserQuestion) | No |
| `permission-request` | PermissionRequest | POST /hook/permission → poll GET /hook/permission/{rid}/decision | Interactive card with buttons | Yes (up to 30m) |

### Session state model

Persistent state has only two top-level values:

- `status="running"`: the current turn is active. `running_since` is the
  watchdog timer start and is refreshed only by explicit WalkCode hook/progress
  events such as user prompt submit, post-tool, subagent, task, or a user action
  that resumes a waiting turn.
- `status="stopped"`: the current turn is not actively running. The reason lives
  in `stop_reason`: `completed`, `permission_request`, `ask_user_question`,
  `interrupted`, `agent_error`, `agent_exited`, or `unknown`.

`timeout` is not a status. A timeout interruption is represented as
`status="stopped"`, `stop_reason="interrupted"`, and
`interrupt_reason="timeout"`. PermissionRequest and AskUserQuestion are stopped
states from the user's point of view, but they keep `running_since` so the same
watchdog can time them out if nobody responds.

The watchdog deliberately does not parse the Claude/Codex footer, pane repaint
noise, or tmux `window_activity`. A running session that exceeds
`WALKCODE_STUCK_THRESHOLD` without any WalkCode hook/progress event is treated as
having no observable progress and gets Esc. This is a product tradeoff: long
single-tool jobs with no intermediate hook event need a larger threshold. Setting
`WALKCODE_HEALTH_CARD=0` disables both the health card and this automatic
timeout interrupt path.
Codex currently exposes fewer progress signals than Claude: it has no
UserPromptSubmit/PostToolUse hook, and trusted or `--yolo` tool calls may skip
the permission hook path entirely. Long unattended Codex turns should therefore
set a larger `WALKCODE_STUCK_THRESHOLD` or disable the health card.

**Note on Notification subtypes:**
- **elicitation_dialog** — When the notification carries an `AskUserQuestion` payload (with `question` and `options` fields), WalkCode sends an interactive card with option buttons instead of plain text. Supports multi-question flows: each question generates a card, the card auto-updates to the next question when answered, and all answers are returned together after the last question.
- **Other matchers** — Sent as plain text messages in the Feishu thread.

**AskUserQuestion features:**
- **Single-select** — Click an option button to select it immediately.
- **multiSelect** — Options render as toggle buttons (✓ prefix when selected). Click to toggle, then click the green "✅ 提交所选" button to finalize. Labels are joined with comma in the answer (e.g. "蓝,绿").
- **Other (custom text)** — Each question card has an "✏️ 其他（自定义文本）" button. Click it, then reply with plain text in the Feishu thread. The next text reply becomes the answer for that question.
- **Answer delivery** — All answers are returned to Claude via `PermissionRequest.decision.updatedInput.answers`, bypassing the native terminal TUI entirely. No tmux key injection is involved.

### Thread subscription

Feishu does not send push notifications for thread replies unless the user has subscribed to the thread. WalkCode auto-subscribes the user by @mentioning them (`<at user_id="..."></at>`) in the **first** thread reply of each session. The `Session.subscribed` flag tracks this — once set to `True`, subsequent replies are sent without @mention.

### Stop / Notification payload

```json
{
  "type": "stop | notification",
  "tty": "tmux-session-name",
  "cwd": "/working/directory",
  "session_id": "claude-uuid",
  "turn_id": "codex-turn-uuid (codex only; empty for Claude)",
  "message": "content text",
  "title": "optional title",
  "matcher": "elicitation_dialog | idle_prompt | …"
}
```

For Stop hooks `message` is the **whole turn's** assistant text, not just the final segment. A turn with tool calls emits a *separate* assistant message for each text block that precedes a tool call (the narration interleaved with tools), so a single turn produces several text segments. Claude's `last_assistant_message` — and codex's equivalent — carry only the *last* one, so the Feishu thread used to show only the tail of what the TUI displayed (the TUI showed N segments, Feishu got the Nth). `walkcode hook stop` therefore reads the transcript and concatenates every segment of the latest turn (in order, joined by blank lines; tool I/O is omitted):

- **Claude** — tails the `transcript_path` JSONL (`_read_turn_assistant_texts`). A turn runs from the most recent *real* user prompt (a string/text-or-image `user` record, **not** a `tool_result` echo) to the end. `isSidechain` (subagent/Task) and `isMeta` (hook-injected) records are skipped so they can't pollute or truncate the turn. If **no** real user prompt is found (a compacted/truncated transcript), it returns "" rather than forwarding the whole file's history — the caller then falls back to `last_assistant_message`.
- **Codex** — prefers the rollout path the hook provides (`transcript_path` when it names a `rollout-*` file), else locates it by `session_id` (`rollout-<ts>-<session_id>.jsonl` under `~/.codex/sessions/`; the id is validated to a plain token and matched on the exact suffix, so a value with glob metacharacters can't widen the match). Every `agent_message` since the last real `user_message` is concatenated (`_read_codex_turn_messages`).

Two robustness rules apply to both paths:

- **Truncation keeps the tail.** When a turn exceeds the char cap, the *leading* narration is dropped (marked `…(truncated)` at the front), never the final answer — the conclusion is what the old `last_assistant_message` path always delivered (`_join_turn_segments`).
- **The final segment is guaranteed.** After the transcript read, if `last_assistant_message` (the authoritative final segment) isn't already present, it is appended. This recovers the case where codex's double-fired Stop reads a still-flushing rollout and misses the last segment — without it, the server's `turn_id` dedupe would freeze the incomplete first read in Feishu.

If the transcript is unreadable or yields no text, it falls back to the single `last_assistant_message` the hook provided, then to empty — in which case the Feishu thread shows only the "✅ Task complete" label (e.g. a turn that ended on a pure `tool_use` with no text at all). The Stop branch logs a one-line `source=...` breadcrumb to stderr (the hook log) so a silent degradation back to the single segment is distinguishable from a genuinely one-segment turn.

**Delivery dedupe.** Hook delivery is "at-least-once": codex CLI (≥ 0.135) fires each hook event *twice* — two identical hook processes launched microseconds apart with the same payload (same `turn_id`) — which would otherwise produce a duplicate Feishu reply on every turn. `receive_hook` therefore dedupes `stop`/`notification` deliveries on the consumer side: a turn ends once → one notification. The key is `(session_id, type, turn_id)` when codex supplies a `turn_id`, and falls back to `(session_id, type, message-hash)` within a 30 s TTL for Claude (which carries no `turn_id` but also never duplicates, so the hash path is pure defense). The `Stop`-drives-idle marking for inject confirmation happens *before* the dedupe gate, so dedupe only suppresses the duplicate user-facing message, never the busy/idle state machine.

### PermissionRequest flow

```
Claude Code needs permission for a tool
  ↓ PermissionRequest hook fires
walkcode hook permission-request (reads stdin JSON with tool_name, tool_input)
  ↓ POST /hook/permission
Server captures terminal options via tmux, determines perm_type, sends Feishu card
  ↓ hook process long-polls GET /hook/permission/{request_id}/decision
User clicks card button (options match the terminal exactly)
  ↓ register_p2_card_action_trigger callback
Server stores decision, signals waiting hook process; card updates inline (buttons removed)
  ↓ hook outputs JSON to stdout:
  {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
  ↓
Claude Code receives decision, continues execution
```

**Button text**: Button labels come from walkcode's i18n table (`_PERM_BUTTON_LABELS`), not from the terminal. Earlier versions scraped numbered lines from `tmux capture-pane`, but any preceding Claude output (plan steps, todo lists, ...) could be misidentified as the permission prompt's options, producing buttons with completely unrelated text. Since button **semantics** (allow / always_allow / deny) are owned by walkcode anyway, the screen has no authoritative information to contribute.

**Suggestion rendering**: When the hook supplies `permission_suggestions` (roughly 1/3 of requests in practice), `_format_permission_suggestions` renders the rule scope into the card body — e.g. `Edit /.claude/skills/deep-debug/**` _(this session)_ — so the user can see exactly what "always allow" will cover.

**Permission types**: The server classifies each request into a `perm_type` based on `permission_mode` and `permission_suggestions`:

| perm_type | Condition | Button labels | Button behaviors |
|-----------|-----------|---------------|-----------------|
| `plan` | `permission_mode == "plan"` + no suggestions | `feishu.plan.auto_accept` / `feishu.plan.manual_approve` / `feishu.plan.tell_claude` | `plan_auto_accept` / `plan_manual_approve` / `deny` |
| `setMode` | first suggestion `type == "setMode"` | `feishu.setmode.yes` / `feishu.setmode.accept_edits` / `feishu.setmode.no` | `allow` / `accept_edits` / `deny` |
| `addRules` | default | `feishu.perm.allow` / `feishu.perm.always_allow` / `feishu.perm.deny` | `allow` / `always_allow` / `deny` |

If "Always Allow" is clicked (addRules type):

1. **Current session**: the hook returns `updatedPermissions` in the decision, which tells Claude Code to remember this rule for the rest of the session — no more prompts for the same tool.
2. **Future sessions**: the tool is added to `~/.claude/settings.json` `permissions.allow`, so Claude Code auto-approves it at startup and never fires the PermissionRequest hook.

Note: Claude Code reads `settings.json` only at startup, so writing to it alone does not affect the running session. The `updatedPermissions` field is what makes the "Always Allow" take effect immediately.

**Permission dedupe.** codex (≥ 0.135) double-fires PreToolUse just like Stop, so one tool call would otherwise produce two permission cards and two long-polls. `receive_permission_hook` dedupes by `(session_id, tool_use_id)` — `tool_use_id` identifies a single tool call, whereas `turn_id` would wrongly merge a whole turn's requests. The duplicate reuses the first `request_id` (no second card), and both hook processes long-poll the **same** decision, so codex's two PreToolUse returns can't diverge. Unlike the one-shot pop model, the decision is read-not-popped and reaped lazily: kept `_PERM_GC_GRACE` (5 s) after the first poller reads it, with a `_PERM_DEDUPE_TTL` (30 s) backstop for a codex request no poller ever drains. AskUserQuestion is Claude-only and carries no `tool_use_id`, so its key is `None` → never deduped and never TTL-reaped (it may wait minutes for a multi-step / Other answer). The `_schedule_tmux_fallback` backstop now gates on `consumed_at` (was: decision presence), so a normal double-fire — consumed by at least one poller within 5 s — never triggers a spurious tmux key injection.

### Hook installation

`walkcode install-hooks` writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "walkcode hook sync"}]}],
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "walkcode hook user-prompt-submit"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "walkcode hook stop"}]}],
    "SubagentStart": [{"hooks": [{"type": "command", "command": "walkcode hook subagent-start"}]}],
    "SubagentStop": [{"hooks": [{"type": "command", "command": "walkcode hook subagent-stop"}]}],
    "TaskCreated": [{"hooks": [{"type": "command", "command": "walkcode hook task-created"}]}],
    "TaskCompleted": [{"hooks": [{"type": "command", "command": "walkcode hook task-completed"}]}],
    "Notification": [{"matcher": "elicitation_dialog", "hooks": [
      {"type": "command", "command": "walkcode hook notification"}
    ]}],
    "PermissionRequest": [{"matcher": "", "hooks": [
      {"type": "command", "command": "walkcode hook permission-request", "timeout": 2100000}
    ]}],
    "PostToolUse": [{"hooks": [
      {"type": "command", "command": "walkcode hook post-tool"}
    ]}]
  }
}
```

The generated permission hook timeout follows `WALKCODE_STUCK_THRESHOLD` plus a
short grace window, so the watchdog owns the visible timeout and the hook remains
alive long enough to consume the watchdog's deny decision.
Because hook timeout values are written into the agent's hook config at install
time, changing `WALKCODE_STUCK_THRESHOLD` requires rerunning
`walkcode install-hooks` for each instance and restarting the service.

---

## Message Routing

### Async dispatch (the SDK must ack fast)

The Lark SDK calls our handler synchronously on its WebSocket asyncio loop and then sends the WebSocket ack frame **only after the handler returns**. Doing tmux/HTTP work inline therefore delays the ack, starves the heartbeat (PING/PONG keepalive timeouts in the SDK logs), and causes Feishu to redeliver the same message on the next reconnect.

`_on_message()` is therefore a thin shim: it submits the work to a single-worker `ThreadPoolExecutor` (`_msg_executor`) and returns. The actual work runs in `_handle_message()`, wrapped by `_handle_message_safe()` so a raised exception is logged and the executor thread survives.

Single worker (not a pool) preserves the FIFO ordering the synchronous path used to give us — important because multiple replies to the same thread must inject in the order they arrived.

The first log line per message now also carries `message_id`, `parent`, and `root` so duplicate deliveries can be diagnosed by grepping the `message_id`.

### Routing logic

`_handle_message()` handles all incoming Feishu messages. The routing logic after parsing the message text:

```
message received
  │
  ├─ config.feishu_receive_id empty?
  │    └─→ print sender's open_id to console (setup helper)
  │
  ├─ no parent_id AND no root_id?
  │    └─→ _start_claude(text, message_id)          # new session
  │
  └─ is a reply
       │
       ├─ _resolve_session_id(msg) found session_id?
       │    └─ _load_reply_session(session_id)
       │         ├─ tmux alive?  → inject(tty, text)
       │         ├─ tmux dead, session data exists?  → _resume_claude(...)
       │         └─ session not in store?  → "session expired"
       │
       └─ no session_id (pending feishu-initiated session)
            ├─ _pending_msg_to_tty[root_id] found?  → inject(pending_tty, text)
            └─ not found?  → "session not found"
```

`_resolve_session_id()` looks up `msg.root_id` first, then `msg.parent_id`, in `_root_to_session`. This handles both top-level thread replies (where `root_id` is set) and direct replies to individual messages (where only `parent_id` is set).

---

## State Persistence

State is stored in `~/.walkcode/state.json` (configurable via `WALKCODE_STATE_PATH`).

### Schema

```json
{
  "sessions": {
    "<claude_session_id>": {
      "tty": "tmux-session-name",
      "cwd": "/working/directory",
      "root_msg_id": "feishu-root-message-id",
      "created_at": 1709876543.0
    }
  },
  "pending": {
    "<tmux_session_name>": {
      "root_msg_id": "feishu-message-id",
      "reply_id": "feishu-reply-message-id"
    }
  }
}
```

The `pending` section tracks Feishu-initiated sessions that have been launched but whose first hook has not yet arrived. Once the first hook fires, the entry is consumed and moved into `sessions`.

### Indexes

`SessionStore` maintains two in-memory indexes rebuilt on load and after every write:

```python
_sessions: dict[str, Session]        # session_id → Session
_root_to_session: dict[str, str]     # root_msg_id → session_id
```

The second index is what makes Feishu → Claude session_id reverse lookup O(1).

### Write strategy

All writes use an atomic rename (`tempfile → replace`) to prevent corruption if the process is killed mid-write. The file is written on every `upsert()` and `touch()` — frequency is bounded by hook rate (typically a few times per task).

### No TTL

Sessions are never deleted by age. This is a deliberate choice: since `--resume` can recover any conversation regardless of how long ago it was active, expiring records would silently break resume for long-idle sessions. The storage overhead is negligible.

---

## Idle Reaper

A background daemon thread runs every 10 minutes and checks all tracked sessions.

### Idle detection

```python
activity = get_session_activity(session.tty)
# calls: tmux display-message -t {name} -p "#{window_activity}"
# returns: epoch float of last terminal output, or None if session doesn't exist
```

`#{window_activity}` is tmux's native tracking of the last time any output was written to a window. Unlike `#{session_activity}` (which only updates on real client attach/keypress), `window_activity` correctly tracks `send-keys` input and program output in detached sessions.

### Reap decision

```
activity is None  →  session already dead, skip (don't notify)
now - activity > 7200s (2h)  →  kill + notify
otherwise  →  skip
```

### On kill

```python
kill_session(session.tty)          # tmux kill-session -t {name}
_reply(root_msg_id,                # notify user in Feishu thread
    t("feishu.idle_killed"),       # locale-aware message (see i18n)
    reply_in_thread=True)
```

The session record is **not deleted** from state — it stays for resume.

---

## i18n

All user-facing strings — CLI output, Feishu messages, error messages — pass through a lightweight i18n module (`src/walkcode/i18n.py`).

### Locale detection

```python
def _detect_zh() -> bool:
    lang = os.environ.get("LANG", "") or os.environ.get("LANGUAGE", "")
    return lang.startswith("zh")
```

If the system locale starts with `zh` (e.g., `zh_CN.UTF-8`), all output is Chinese. Otherwise, English.

### Translation function

```python
_T: dict[str, tuple[str, str]] = {
    "feishu.idle_killed": (
        "⏰ Session closed due to inactivity, reply to resume",    # en
        "⏰ 会话因长时间无活动已关闭，回复任意消息可恢复",              # zh
    ),
    # ... ~60 keys
}

def t(key: str, **kwargs) -> str:
    pair = _T[key]
    text = pair[1] if _ZH else pair[0]
    return text.format(**kwargs) if kwargs else text
```

### Design decisions

- **Logger messages stay English** — logs are for developers; mixing locales in log output hurts searchability.
- **Shell scripts have their own i18n** — `install.sh` and `uninstall.sh` use `is_zh()` / `msg()` functions, not the Python module.
- **No framework dependency** — the entire i18n system is a single file with zero external imports.
