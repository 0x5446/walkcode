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
  → tmux send-keys -t {tty}
```

### tty is always kept current

Every time a Claude hook fires, it carries the current tmux session name. `receive_hook()` calls `session_store.upsert(session_id, tty=tty, ...)` unconditionally, so `tty` is always updated to wherever Claude is running right now. There is a brief window between a new Claude start and its first hook where `tty` is stale, but this is harmless — the user cannot have replied yet.

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

The pending period (between tmux creation and first hook) uses two in-memory dicts:

```python
_pending_roots: dict[str, str]      # tmux_name → feishu_message_id
_pending_msg_to_tty: dict[str, str] # feishu_message_id → tmux_name
_pending_reply_ids: dict[str, str]  # tmux_name → reply_message_id (for editing)
```

These are consumed atomically when the first hook arrives (`_pending_roots.pop(tty, None)`).

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

## Hook Communication Protocol

Claude Code hooks call `walkcode hook {stop|notification|permission-request}` which reads JSON from stdin and communicates with the WalkCode server.

### Hook types

| Hook | Claude Event | Endpoint | Feishu Format | Blocking |
|------|-------------|----------|---------------|----------|
| `stop` | Stop | POST /hook | Plain text | No |
| `notification` | Notification (elicitation_dialog) | POST /hook | Plain text | No |
| `permission-request` | PermissionRequest | POST /hook/permission → poll GET /hook/permission/{rid}/decision | Interactive card with buttons | Yes (up to 120s) |

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

### PermissionRequest flow

```
Claude Code needs permission for a tool
  ↓ PermissionRequest hook fires
walkcode hook permission-request (reads stdin JSON with tool_name, tool_input)
  ↓ POST /hook/permission
Server sends Feishu interactive card (tool name + input + 3 buttons)
  ↓ hook process long-polls GET /hook/permission/{request_id}/decision
User clicks card button (Allow / Deny / Always Allow)
  ↓ register_p2_card_action_trigger callback
Server stores decision, signals waiting hook process; card updates inline (buttons removed)
  ↓ hook outputs JSON to stdout:
  {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
  ↓
Claude Code receives decision, continues execution
```

If "Always Allow" is clicked:

1. **Current session**: the hook returns `updatedPermissions` in the decision, which tells Claude Code to remember this rule for the rest of the session — no more prompts for the same tool.
2. **Future sessions**: the tool is added to `~/.claude/settings.json` `permissions.allow`, so Claude Code auto-approves it at startup and never fires the PermissionRequest hook.

Note: Claude Code reads `settings.json` only at startup, so writing to it alone does not affect the running session. The `updatedPermissions` field is what makes the "Always Allow" take effect immediately.

### Hook installation

`walkcode install-hooks` writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{"hooks": [{"type": "command", "command": "walkcode hook stop"}]}],
    "Notification": [{"matcher": "elicitation_dialog", "hooks": [
      {"type": "command", "command": "walkcode hook notification"}
    ]}],
    "PermissionRequest": [{"matcher": "", "hooks": [
      {"type": "command", "command": "walkcode hook permission-request", "timeout": 120000}
    ]}]
  }
}
```

---

## Message Routing

`_on_message()` handles all incoming Feishu messages. The routing logic after parsing the message text:

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
  }
}
```

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
# calls: tmux display-message -t {name} -p "#{session_activity}"
# returns: epoch float of last terminal output, or None if session doesn't exist
```

`#{session_activity}` is tmux's native tracking of the last time any output was written to a pane. This is more accurate than WalkCode's own `created_at` — it reflects actual Claude activity (file reads, shell commands, output) not just hook events.

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
