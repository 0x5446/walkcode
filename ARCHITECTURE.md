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

Feishu replies are delivered into Claude by injecting keystrokes with `tmux
send-keys`. A successful `send-keys` only proves the bytes reached the pane — it
is **not** proof Claude accepted them as a prompt. When the TUI is not at a
ready, empty input box, the keystrokes are routed elsewhere and the message is
silently lost:

- a modal/overlay is open (`/status`, `/model`, a permission prompt, the
  `/resume` picker, autocomplete) — the text is ignored by the dialog and the
  injected Enter just dismisses it;
- the pane is in tmux copy-mode (scrollback) — keys go to copy-mode;
- the context is 100% full — submission is blocked.

Previously walkcode added a success emoji based purely on the `send-keys` exit
code, so these losses looked delivered. Delivery is now confirmed out-of-band
via the **`UserPromptSubmit` hook** — no screen scraping:

```
_handle_message
  → inject(tty, text)                 # send-keys; raise → immediate "not delivered" reply
  → _register_pending_inject(...)     # await confirmation; NO emoji yet

UserPromptSubmit hook (the prompt was actually accepted)
  → POST /hook/prompt → _confirm_pending_inject()  → success emoji, clear pending

sweeper (1s tick)
  → window elapsed with no confirmation → "not delivered" reply + failure emoji
```

**Busy/idle is derived from hooks only**, so an inject during generation is
treated as *queued* (not lost): `UserPromptSubmit` marks a turn busy, `Stop`
marks it idle.

| Inject context | Confirmation window | No `UserPromptSubmit` ⇒ |
|----------------|---------------------|--------------------------|
| Session idle | `injected_at` + `_INJECT_CONFIRM_GRACE` (4s) | swallowed (modal/copy-mode) → reported failure |
| Session busy (generating) | first `Stop` after inject, then + grace | queued prompt never landed → reported failure |
| Never goes idle | `_INJECT_CONFIRM_MAX` (10m) backstop | reported failure |

Matching is by `session_id` (falling back to tmux name), with whitespace-tolerant
substring comparison of the echoed prompt. The single-key permission-answer path
(`_tmux_fallback`) is **not** confirmed this way — it targets a prompt overlay on
purpose, not the chat input.

The cost: `UserPromptSubmit` fires on **every** prompt in every Claude session,
so `walkcode hook user-prompt-submit` is a fast path — a short-timeout localhost
POST with no stdout (hook stdout would be injected into the prompt), returning
before any debug logging.

---

## Hook Communication Protocol

Claude Code hooks call `walkcode hook {stop|notification|permission-request|sync|user-prompt-submit}` which reads JSON from stdin and communicates with the WalkCode server.

### Hook types

| Hook | Claude Event | Endpoint | Feishu Format | Blocking |
|------|-------------|----------|---------------|----------|
| `sync` | SessionStart | POST /hook/sync | None (mapping update only) | No |
| `user-prompt-submit` | UserPromptSubmit | POST /hook/prompt | None (delivery confirmation + busy state) | No |
| `stop` | Stop | POST /hook | Plain text | No |
| `notification` | Notification (elicitation_dialog) | POST /hook | Plain text or interactive card (AskUserQuestion) | No |
| `permission-request` | PermissionRequest | POST /hook/permission → poll GET /hook/permission/{rid}/decision | Interactive card with buttons | Yes (up to 30m) |

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
  "message": "content text",
  "title": "optional title",
  "matcher": "elicitation_dialog | idle_prompt | …"
}
```

For Stop hooks `message` is sourced from Claude Code's `last_assistant_message` field. When that field arrives empty — which happens when the final assistant turn is a pure `tool_use` block (e.g. ends on `TaskUpdate`) — `walkcode hook stop` tails the `transcript_path` JSONL and recovers the most recent assistant text content. Without this fallback the Feishu thread would show only the "✅ Task complete" label with no reply body.

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

### Hook installation

`walkcode install-hooks` writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{"hooks": [{"type": "command", "command": "walkcode hook sync"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "walkcode hook stop"}]}],
    "Notification": [{"matcher": "elicitation_dialog", "hooks": [
      {"type": "command", "command": "walkcode hook notification"}
    ]}],
    "PermissionRequest": [{"matcher": "", "hooks": [
      {"type": "command", "command": "walkcode hook permission-request", "timeout": 1800000}
    ]}]
  }
}
```

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

