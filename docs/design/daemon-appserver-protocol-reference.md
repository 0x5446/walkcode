# Claude Code Daemon & Codex App-Server Protocol Reference

> Grounding: all protocol details in this document are derived from local
> observation on 2026-07-04. Claude Code v2.1.201 protocol was reverse-
> engineered from the compiled binary (`strings` extraction + live socket
> probing). Codex app-server protocol was exported via the official
> `codex app-server generate-json-schema --experimental` command (CLI v1.x).
> Both protocols are marked **experimental** by their vendors and may change
> across versions.

## Purpose

Enable a single agent session to be read and written from multiple UIs
simultaneously (terminal TUI + Feishu/Lark messaging card). This document
captures the complete control-plane protocol for both Claude Code and Codex,
sufficient to implement a secondary UI client alongside the native TUI.

---

## Part 1: Claude Code Daemon Protocol

### 1.1 Architecture Overview

```
                       +-----------------+
                       |  daemon (PID 1) |  one per CLAUDE_CONFIG_DIR
                       |  supervisor     |
                       +--------+--------+
                                |
                    control.sock (Unix domain)
                       /        |        \
                      /         |         \
            +--------+   +--------+   +--------+
            |pty-host|   |pty-host|   |pty-host|   <- TUI terminal shells
            | attach |   | attach |   | attach |
            +--------+   +--------+   +--------+
                 |             |             |
            +--------+   +--------+   +--------+
            | worker |   | worker |   | worker |   <- session processes
            |session1|   |session2|   |session3|      --session-id <uuid>
            +--------+   +--------+   +--------+
```

- The **supervisor** (daemon) manages worker lifecycles and multiplexes
  client connections over one control socket.
- Each **worker** is a standalone process that holds one conversation
  session, identified by `--session-id <uuid>`.
- **pty-host** processes provide TUI rendering for attached terminal
  windows. They are optional; workers run fine without any pty-host.
- The daemon starts on demand when the first `claude` CLI invocation
  opens a session, and exits when the last client disconnects.

> **Scope correction (live-verified 2026-07-05):** the daemon manages
> **background-agent sessions only** (`claude --bg` / `/bg` from the TUI,
> then attached via `claude agents` → pty-host). A plain interactive
> `claude` TUI run is a standalone process: it does NOT register as a
> daemon job, never appears in `list`, and cannot be driven via `reply`.
> `claude daemon status` labels its jobs "bg sessions" accordingly, and a
> profile with no bg sessions has no daemon running at all. Any multi-UI
> client must treat plain-TUI sessions as out of daemon scope and keep a
> fallback (walkcode: hook observation + takeover).

### 1.2 File Paths

| File | Purpose |
|------|---------|
| `/tmp/cc-daemon-<uid>/<hash>/control.sock` | Control socket (Unix domain) |
| `<CLAUDE_CONFIG_DIR>/daemon/control.key` | 32-char hex authentication key |
| `<CLAUDE_CONFIG_DIR>/daemon/roster.json` | Persistent job roster |
| `<CLAUDE_CONFIG_DIR>/daemon.log` | Supervisor log |
| `<CLAUDE_CONFIG_DIR>/daemon.status.json` | Supervisor PID and metadata |
| `<CLAUDE_CONFIG_DIR>/daemon.lock` | Supervisor lock file |

- `CLAUDE_CONFIG_DIR` defaults to `~/.claude` when no profile wrapper
  is used. Profile wrappers set it to, e.g., `~/.claude-profiles/work`.
- Each distinct `CLAUDE_CONFIG_DIR` runs its own independent daemon.
- The `<hash>` in the socket path is deterministic:
  `sha256(<expanded absolute CLAUDE_CONFIG_DIR, no trailing slash>)[:8]`.
  Live-verified 2026-07-04: `sha256("/Users/alpha/.claude-profiles/work")[:8]`
  = `19e5f12f`, matching `/tmp/cc-daemon-501/19e5f12f/control.sock`. This
  makes socket discovery a pure function of the profile config dir — no
  globbing or daemon.status.json parsing needed.

### 1.3 Transport

- **Socket type**: Unix domain socket, stream mode.
- **Framing**: newline-delimited JSON (ndjson). Each message is one JSON
  object followed by `\n`.
- **Direction**: the socket is bidirectional. The client sends a request;
  the server sends one or more responses on the same connection.
- **Concurrency**: a single TCP-style connection handles one logical
  conversation. For streaming operations (`subscribe`, `attach`), the
  connection stays open and the server pushes events until the job settles
  or the client disconnects.

### 1.4 Authentication

The daemon uses a symmetric key stored in `daemon/control.key` (a 32-character
hex string, readable only by the owning user). Operations that modify session
state require the key in the `auth` field of the request. Read-only operations
(`ping`, `list`, `has`, `subscribe`) do not require authentication.

### 1.5 Request Format

Every request is a single JSON object with at minimum:

```json
{
  "proto": 1,
  "op": "<operation>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `proto` | integer | yes | Protocol version. Must match the server's version (currently `1`). A mismatch returns `EPROTO`. |
| `op` | string | yes | Operation name (see section 1.6). |
| `auth` | string | for write ops | Contents of `daemon/control.key`. |
| `short` | string | per-op | First 8 hex chars of the session UUID (the job identifier). |

### 1.6 Operations

#### 1.6.1 `ping` (no auth)

Health check. Returns the daemon version and protocol number.

```
-> {"proto":1,"op":"ping"}
<- {"ok":true,"op":"ping","version":"2.1.201","proto":1}
```

#### 1.6.2 `list` (no auth)

List all managed jobs (sessions).

```
-> {"proto":1,"op":"list"}
<- {"ok":true,"op":"list","jobs":[<JobRecord>, ...]}
```

**JobRecord fields:**

| Field | Type | Description |
|-------|------|-------------|
| `short` | string | 8-char hex job identifier |
| `nonce` | string | 8-char hex nonce for dispatch dedup |
| `sessionId` | string | Full UUID of the session |
| `pid` | integer | Worker process PID |
| `attempt` | integer | Spawn attempt counter |
| `startedAt` | integer | Unix timestamp ms when worker started |
| `createdAt` | integer | Unix timestamp ms when job was created |
| `cwd` | string | Working directory |
| `backend` | string | Always `"daemon"` |
| `tempo` | string | `"active"` / `"idle"` / `"blocked"` |
| `state` | string | `"working"` / `"adopted"` / `"done"` / `"failed"` / `"blocked"` |
| `detail` | string | Human-readable detail (last user prompt, current action, etc.) |
| `intent` | string | High-level intent description |
| `needs` | string | What the session is waiting for (e.g., `"approve playwright_work - ..."`) |
| `name` | string | Session display name |
| `cliVersion` | string | Claude Code version that created the session |
| `source` | string | How the session was created (e.g., `"slash"`, `"cli"`) |
| `legacy` | boolean | Whether the job predates the current supervisor |
| `outcome` | string/null | `null` while running, `"ok"` when finished, `"killed"` when terminated |
| `dying` | boolean | Present and `true` when the job is being killed or retired |

#### 1.6.3 `has` (no auth)

Check whether a specific job exists and is ready.

```
-> {"proto":1,"op":"has","short":"5ca3e37c"}
<- {"ok":true,"op":"has","alive":true,"present":true,"ready":true}
```

| Response field | Description |
|----------------|-------------|
| `alive` | Worker process is running |
| `present` | Job is known to the supervisor |
| `ready` | Worker has finished booting |

#### 1.6.4 `subscribe` (no auth) -- KEY FOR READ SYNC

Subscribe to a job's terminal output and state changes. The connection
stays open and the server pushes events in real time.

```
-> {"proto":1,"op":"subscribe","short":"5ca3e37c"}
```

**Optional fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tail` | integer | 200 | Number of historical terminal chunks to include in the initial snapshot |

**Server pushes three event types:**

##### `snapshot` (sent once, immediately)

```json
{
  "type": "snapshot",
  "record": { /* full JobRecord */ },
  "streamTail": ["<ANSI chunk>", ...]
}
```

The `streamTail` array contains the last N terminal output chunks (raw
ANSI escape sequences). This allows reconstructing the current screen
state.

##### `stream` (continuous)

```json
{
  "type": "stream",
  "line": "<ANSI terminal data>"
}
```

Raw terminal output bytes as they are produced by the worker. Includes
ANSI escape codes for cursor movement, colors, and TUI rendering. For a
secondary UI (like Feishu cards), this stream can be ignored in favor of
`state` events, or parsed for plain-text extraction.

##### `state` (on change)

```json
{
  "type": "state",
  "patch": {
    "tempo": "active",
    "state": "working",
    "needs": "",
    "detail": "echo hi",
    "intent": "..."
  }
}
```

Structured state updates. The `patch` object contains only changed fields.
This is the primary mechanism for a secondary UI to track session state
without parsing ANSI terminal output.

**Key `patch` fields for UI sync:**

| Field | Meaning |
|-------|---------|
| `tempo=active` | Agent is thinking / working |
| `tempo=idle` | Waiting for user input |
| `tempo=blocked` | Waiting for approval or external action |
| `needs` | Non-empty when the session needs something. Two distinct meanings (live-verified): a real permission gate is `tempo=blocked` with `needs="approve <Tool>: <detail>"`; an idle worker also reports needs like `"send a prompt to start"` — do NOT treat every non-empty needs as a permission gate |
| `detail` | Latest activity description (user prompt text, tool being run, etc.) |
| `state=done` | Turn completed |

##### `settled` (sent once, then connection closes)

```json
{
  "type": "settled",
  "outcome": "ok"
}
```

Sent when the job finishes (exits, killed, etc.). After this event, the
server closes the connection.

#### 1.6.5 `reply` (auth required) -- KEY FOR WRITE SYNC

Inject user input text into a running session. This is equivalent to
typing in the TUI prompt and pressing Enter.

```
-> {"proto":1,"op":"reply","auth":"<key>","short":"5ca3e37c","text":"hello from Feishu"}
<- {"ok":true,"op":"reply"}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `short` | string | yes | Target job identifier |
| `text` | string | yes | User input text to inject |
| `auth` | string | yes | Daemon control key |

**Error responses:**

| Code | Meaning |
|------|---------|
| `EAUTH` | Control key mismatch |
| `ENOJOB` | Job not found or already exited |
| `ENOREPLY` | Job is not accepting replies (not in an interactive state) |

**Observed behavior:** When `reply` succeeds, the text appears in the TUI
as if the user typed it. The session processes it as a new user turn.
Subscribers see the prompt in `stream` events and the resulting state
changes in `state` events.

#### 1.6.6 `attach` (auth required)

Full bidirectional terminal attachment. Used by pty-host processes to
render the TUI. After a successful attach, the connection becomes a raw
byte stream (terminal I/O).

```
-> {"proto":1,"op":"attach","auth":"<key>","short":"5ca3e37c","cols":120,"rows":50}
<- {"ok":true,"op":"attach","decModes":{...},"via":"...","tempo":"active","state":"working"}
```

After the initial JSON response, the connection switches to raw PTY I/O:
- **Client -> Server**: terminal input bytes (keystrokes)
- **Server -> Client**: terminal output bytes (ANSI rendering)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `short` | string | yes | Target job identifier |
| `cols` | integer | yes | Terminal columns |
| `rows` | integer | yes | Terminal rows |
| `auth` | string | yes | Daemon control key |
| `attachId` | string | no | Unique attacher identifier (for multi-attach) |

**Multi-attach:** Multiple pty-hosts can attach to the same job
simultaneously. Each gets the full terminal stream. The last attacher's
terminal dimensions win for resize purposes. The `attachers` map on the
job tracks all connected clients.

**Keystroke injection via second attacher（2026-07-06 实测）:** 第二个
attacher（自定义 `attachId`）在握手成功、短暂 settle（~0.8s）后写入的
原始字节会进入 worker 的终端输入处理器，与真人键盘输入等价——**能直接
驱动原生对话框**（AskUserQuestion 单选实测三次：注入 `b"N"` 一击选中即
确认、无需回车，subscribe 观测 blocked→idle/resolved）。注入发生在 raw
PTY 字节层，不区分对话框类型。相关事实：

- 原生对话框（AskUserQuestion / 权限提示）**无自动超时**，无限等待键盘输入；
- 无任何 attacher 时对话框仍在 job 的 PTY 内渲染，`state` patch 的 `needs`
  照常出现（ask 形如 `answer: <question> (<label1> · <label2> ...)`，
  选项按序号 1 起排列；permission 形如 `approve <Tool>: <detail>`）；
- 已有 attacher（如用户终端）不影响第二连接注入，双方输入互不互斥。

WalkCode v3 真双端方案（`claude-daemon-multi-ui-sync.md`「交互闭环 v3」）
基于此机制。

#### 1.6.7 `dispatch` (auth required)

Create and start a new session (job) via the daemon.

```
-> {"proto":1,"op":"dispatch","auth":"<key>","timeoutMs":30000,"d":{
     "short":"<8-hex>",
     "nonce":"<8-hex>",
     ...
   }}
<- (awaits worker acknowledgment, then responds)
```

The `d` object contains the full job dispatch specification. This is
typically used internally by the CLI to start new sessions.

#### 1.6.8 `kill` (auth optional)

Terminate a running job.

```
-> {"proto":1,"op":"kill","short":"5ca3e37c","signal":"SIGTERM"}
<- {"ok":true,"op":"kill"}
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `signal` | string | `"SIGTERM"` | Signal to send to the worker |
| `handoff` | boolean | false | If true, marks outcome as `"handoff"` instead of `"killed"` |

#### 1.6.9 `resize` (no auth)

Resize the terminal for a job or a specific attacher.

```
-> {"proto":1,"op":"resize","short":"5ca3e37c","cols":200,"rows":50}
<- {"ok":true,"op":"resize"}
```

#### 1.6.10 `respawn-stale` (no auth)

Ask the supervisor to restart a stale (idle, unresponsive) worker.

```
-> {"proto":1,"op":"respawn-stale","short":"5ca3e37c"}
<- {"ok":true,"op":"respawn-stale","respawned":true,"reason":"..."}
```

#### 1.6.11 `permission-response` (auth required) — verified: NON-FUNCTIONAL SHELL

Schema (verified against the binary's Zod validator):

```
-> {"proto":1,"op":"permission-response","auth":"<key>","short":"...",
    "requestId":"<string>","allow":true|false}
<- {"ok":true,"op":"permission-response"}
```

Live-verified 2026-07-05 (v2.1.201): the handler only validates `auth` and
returns `{ok:true}` — it does **not** forward the decision to the worker.
Sending it against a permission-gated session left the session blocked.
Do not build on this op. WalkCode's approval loop uses a blocking PreToolUse
hook instead (see `claude-daemon-multi-ui-sync.md`, "交互闭环 v2").

#### 1.6.12 Other operations

| Op | Auth | Description |
|----|------|-------------|
| `nudge` | no | Wake a sleeping worker |
| `yield` | no | Yield CPU time hint |
| `lease` | no | Acquire a client lease |
| `leases` | no | List active client leases |
| `shutdown` | no | Shut down the supervisor and optionally reap workers |
| `await-ack` | no | Wait for a job to acknowledge a dispatch |
| `ensure-spare` | no | Ensure spare worker slots are pre-warmed |

### 1.7 Error Response Format

All error responses follow this shape:

```json
{
  "ok": false,
  "error": "human-readable error message",
  "code": "ECODE"
}
```

| Code | Meaning |
|------|---------|
| `EPROTO` | Protocol version mismatch |
| `EAUTH` | Authentication key mismatch |
| `ENOJOB` | Job not found or already exited |
| `ENOREPLY` | Job is not accepting replies |
| `ESTARTING` | Supervisor is still starting up |
| `ERESPAWNING` | Worker is restarting; retry the operation |
| `EUNVERIFIED` | Worker identity could not be verified |
| `EUNKNOWN` | Catch-all for malformed requests or unexpected conditions |

### 1.8 Session Lifecycle for Secondary UI

```
 Secondary UI Client                  Daemon                    Worker
       |                                |                          |
       |--- subscribe(short) ---------->|                          |
       |<-- snapshot(record, tail) -----|                          |
       |                                |                          |
       |<-- state(tempo=idle) ----------|<---- worker idle --------|
       |                                |                          |
  [User sends message on Feishu]        |                          |
       |                                |                          |
       |--- reply(text, auth) --------->|--- inject input -------->|
       |<-- {ok:true} ------------------|                          |
       |                                |                          |
       |<-- state(tempo=active) --------|<---- worker thinking ----|
       |<-- stream(ANSI data) ----------|<---- terminal output ----|
       |<-- state(detail="...") --------|<---- progress update ----|
       |<-- state(tempo=idle) ----------|<---- turn complete ------|
       |                                |                          |
  [User sends another message]          |                          |
       |--- reply(text, auth) --------->|--- inject input -------->|
       |     ...                        |                          |
```

---

## Part 2: Codex App-Server Protocol

### 2.1 Architecture Overview

```
                      +-------------------+
                      |  app-server       |  one per CODEX_HOME
                      |  (daemon mode)    |
                      +--------+----------+
                               |
                    Unix socket / WebSocket / stdio
                      /        |         \
               +------+  +------+  +--------+
               |client|  |client|  | client  |
               | TUI  |  | IDE  |  |walkcode |
               +------+  +------+  +--------+
                               |
                         +----------+
                         |  thread  |  (= session / conversation)
                         +----------+
```

- The **app-server** manages threads (conversations), model interaction,
  tool execution, and file system operations.
- Multiple **clients** can connect simultaneously to the same app-server
  and interact with the same or different threads.
- Unlike Claude's daemon, the app-server uses standard **JSON-RPC 2.0**
  over configurable transports.

### 2.2 Starting the App-Server

```bash
# Start as a daemon
codex app-server daemon start

# Start with explicit transport
codex app-server --listen unix:///tmp/codex.sock
codex app-server --listen ws://127.0.0.1:8080
codex app-server --listen stdio://   # default

# With remote control (exposes to codex.com web UI)
codex remote-control start

# Check daemon status
codex app-server daemon version
```

### 2.3 File Paths

| File | Purpose |
|------|---------|
| `<CODEX_HOME>/app-server-daemon/daemon.lock` | Daemon lock file |
| `<CODEX_HOME>/app-server-control/app-server-control.sock` | Control socket |

`CODEX_HOME` defaults to `~/.codex`. Profile wrappers set it to, e.g.,
`~/.codex-profiles/work`.

### 2.4 Transport Options

| URL scheme | Description |
|------------|-------------|
| `stdio://` | Standard input/output (default). Used by IDE extensions. |
| `unix://` | Auto-generated Unix socket path |
| `unix://PATH` | Explicit Unix socket path |
| `ws://IP:PORT` | WebSocket listener. Requires `--ws-auth` for non-loopback. |

For multi-UI scenarios, `unix://` or `ws://` mode is required (stdio is
single-client by nature).

### 2.5 Authentication (WebSocket mode)

| Mode | Flag | Description |
|------|------|-------------|
| `capability-token` | `--ws-token-file PATH` or `--ws-token-sha256 HEX` | Shared secret file or hash |
| `signed-bearer-token` | `--ws-shared-secret-file`, `--ws-issuer`, `--ws-audience` | JWT-based authentication |

Unix socket connections rely on filesystem permissions (owner-only access).

### 2.6 Protocol Format (JSON-RPC 2.0)

**Client request:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "thread/start",
  "params": { ... }
}
```

**Server response:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": { ... }
}
```

**Server notification (no `id`, no response expected):**

```json
{
  "jsonrpc": "2.0",
  "method": "item/agentMessage/delta",
  "params": { ... }
}
```

**Server request (has `id`, client must respond):**

```json
{
  "jsonrpc": "2.0",
  "id": "srv-42",
  "method": "item/commandExecution/requestApproval",
  "params": { ... }
}
```

### 2.7 Connection Lifecycle

```
Client                              App-Server
  |                                     |
  |--- initialize(clientInfo) --------->|
  |<-- result(serverInfo) -------------|
  |                                     |
  |--- initialized() ----------------->|  (notification, no response)
  |                                     |
  |--- thread/start(params) ---------->|
  |<-- result(threadId) ---------------|
  |<-- thread/started(thread) ---------|  (notification)
  |                                     |
  |--- turn/start(threadId, input) --->|
  |<-- result(turnId) -----------------|
  |<-- turn/started(turn) -------------|
  |<-- item/started(item) -------------|
  |<-- item/agentMessage/delta(text) --|  (repeated)
  |<-- item/completed(item) -----------|
  |<-- turn/completed(turn) -----------|
  |                                     |
```

### 2.8 Client Request Methods (client -> server)

#### 2.8.1 Session Management

| Method | Description |
|--------|-------------|
| `initialize` | Handshake. Send `clientInfo` with client name and version. |
| `thread/start` | Create a new conversation thread. |
| `thread/resume` | Resume an existing thread by `threadId` or transcript path. |
| `thread/list` | List all known threads. |
| `thread/read` | Read full thread state including turns and items. |
| `thread/fork` | Fork a thread from a specific point. |
| `thread/archive` | Archive a thread. |
| `thread/delete` | Delete a thread. |
| `thread/search` | Search threads by query. |

#### 2.8.2 User Input -- KEY FOR WRITE SYNC

**`turn/start`** -- Send a user message to start a new turn.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "turn/start",
  "params": {
    "threadId": "<thread-uuid>",
    "input": [
      { "type": "text", "text": "Hello from Feishu" }
    ]
  }
}
```

**Required fields:**

| Field | Type | Description |
|-------|------|-------------|
| `threadId` | string | Target thread identifier |
| `input` | UserInput[] | Array of user input items |

**Optional fields:**

| Field | Type | Description |
|-------|------|-------------|
| `model` | string | Override model for this turn |
| `effort` | string | Reasoning effort override |
| `approvalPolicy` | string/object | `"untrusted"` / `"on-failure"` / `"on-request"` / `"never"` |
| `approvalsReviewer` | string | `"user"` (default) or `"auto_review"` |
| `cwd` | string | Working directory override |
| `permissions` | string | Named permissions profile |

**UserInput variants:**

| Type | Shape |
|------|-------|
| `text` | `{ "type": "text", "text": "..." }` |
| `image` | `{ "type": "image", "url": "..." }` |
| `file` | `{ "type": "file", "path": "..." }` |

**`turn/steer`** -- Redirect an active turn with new input.

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "turn/steer",
  "params": {
    "threadId": "<thread-uuid>",
    "expectedTurnId": "<turn-uuid>",
    "input": [{ "type": "text", "text": "actually, do X instead" }]
  }
}
```

**`turn/interrupt`** -- Stop a running turn.

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "turn/interrupt",
  "params": {
    "threadId": "<thread-uuid>",
    "turnId": "<turn-uuid>"
  }
}
```

#### 2.8.3 Terminal / Process Control

| Method | Description |
|--------|-------------|
| `process/spawn` | Spawn a subprocess |
| `process/writeStdin` | Write to a process's stdin |
| `process/resizePty` | Resize a process's PTY |
| `process/kill` | Terminate a process |
| `command/exec` | Execute a shell command |
| `command/exec/write` | Write to a running command's stdin |
| `command/exec/resize` | Resize a command's terminal |
| `command/exec/terminate` | Terminate a running command |

#### 2.8.4 Configuration

| Method | Description |
|--------|-------------|
| `config/read` | Read current configuration |
| `config/value/write` | Write a single config value |
| `config/batchWrite` | Write multiple config values |
| `model/list` | List available models |
| `permissionProfile/list` | List available permission profiles |
| `collaborationMode/list` | List collaboration modes |

#### 2.8.5 Other

| Method | Description |
|--------|-------------|
| `thread/compact/start` | Trigger context compaction |
| `thread/rollback` | Undo the last turn |
| `thread/name/set` | Set thread display name |
| `thread/settings/update` | Update thread-level settings |
| `thread/backgroundTerminals/list` | List background terminal processes |
| `thread/goal/set` | Set a high-level goal for the thread |
| `review/start` | Start a code review |
| `remoteControl/enable` | Enable remote control |
| `remoteControl/pairing/start` | Start remote control pairing |
| `skills/list` | List installed skills |
| `plugin/list` | List installed plugins |
| `feedback/upload` | Submit feedback |

### 2.9 Server Notifications (server -> client) -- KEY FOR READ SYNC

These are pushed automatically when events occur. A secondary UI client
receives all of these on its connection.

#### 2.9.1 Thread State

| Notification | Key fields | Description |
|-------------|------------|-------------|
| `thread/started` | `thread` | Thread created or loaded |
| `thread/status/changed` | `threadId`, `status` | Thread status changed |
| `thread/closed` | `threadId` | Thread unloaded |

**ThreadStatus variants:**

| Type | Meaning |
|------|---------|
| `notLoaded` | Thread exists on disk but is not active |
| `idle` | Thread is loaded and waiting for user input |
| `active` | Thread is processing (has `activeFlags` array) |
| `systemError` | Thread encountered an unrecoverable error |

#### 2.9.2 Turn Lifecycle

| Notification | Key fields | Description |
|-------------|------------|-------------|
| `turn/started` | `threadId`, `turn` | New turn began |
| `turn/completed` | `threadId`, `turn` | Turn finished |
| `turn/diff/updated` | `threadId`, `turnId` | File diff updated |
| `turn/plan/updated` | `threadId`, `turnId` | Execution plan updated |

#### 2.9.3 Item Lifecycle (tools, messages, code execution)

| Notification | Key fields | Description |
|-------------|------------|-------------|
| `item/started` | `threadId`, `turnId`, `item` | Item began |
| `item/completed` | `threadId`, `turnId`, `item` | Item finished |
| `item/agentMessage/delta` | `threadId`, `turnId`, `itemId`, `delta` | Incremental agent text output |
| `item/commandExecution/outputDelta` | `threadId`, `turnId`, `itemId`, `delta` | Command stdout/stderr delta |
| `item/reasoning/textDelta` | `threadId`, `turnId`, `itemId` | Reasoning text delta |
| `item/reasoning/summaryTextDelta` | `threadId`, `turnId`, `itemId` | Reasoning summary delta |
| `item/reasoning/summaryPartAdded` | `threadId`, `turnId`, `itemId` | Reasoning summary part |
| `item/fileChange/outputDelta` | `threadId`, `turnId`, `itemId` | File change preview delta |
| `item/fileChange/patchUpdated` | `threadId`, `turnId`, `itemId` | File patch updated |
| `item/plan/delta` | `threadId`, `turnId`, `itemId` | Plan item delta |
| `item/mcpToolCall/progress` | `threadId`, `turnId`, `itemId` | MCP tool call progress |
| `item/autoApprovalReview/started` | | Auto-approval review began |
| `item/autoApprovalReview/completed` | | Auto-approval review finished |

**ThreadItem types:**

| Type | Description |
|------|-------------|
| `userMessage` | User input message |
| `agentMessage` | Agent response text |
| `commandExecution` | Shell command execution |
| `fileChange` | File modification |
| `mcpToolCall` | MCP tool invocation |

#### 2.9.4 Token Usage

| Notification | Key fields | Description |
|-------------|------------|-------------|
| `thread/tokenUsage/updated` | `threadId` | Token usage counters updated |
| `model/rerouted` | | Model was rerouted (fallback, etc.) |

#### 2.9.5 Other Notifications

| Notification | Description |
|-------------|-------------|
| `error` | Error with `willRetry` flag |
| `hook/started` | Hook execution started |
| `hook/completed` | Hook execution completed |
| `process/exited` | Background process exited |
| `process/outputDelta` | Background process output |
| `skills/changed` | Installed skills changed |
| `warning` | Non-fatal warning |
| `configWarning` | Configuration warning |
| `deprecationNotice` | Feature deprecation notice |

### 2.10 Server Requests (server -> client, client must respond)

These are requests the server sends to the client, requiring a JSON-RPC
response. This is the mechanism for approval workflows.

#### 2.10.1 Command Execution Approval

```json
// Server sends:
{
  "jsonrpc": "2.0",
  "id": "srv-1",
  "method": "item/commandExecution/requestApproval",
  "params": {
    "threadId": "...",
    "turnId": "...",
    "itemId": "...",
    "command": "rm -rf /tmp/test",
    ...
  }
}

// Client responds:
{
  "jsonrpc": "2.0",
  "id": "srv-1",
  "result": {
    "decision": "accept"
  }
}
```

**Decision values:**

| Decision | Description |
|----------|-------------|
| `"accept"` | Approve this command |
| `"acceptForSession"` | Approve this and similar future commands in the same session |
| `{"acceptWithAmendment": {...}}` | Approve with policy amendment |
| `"deny"` | Reject this command |

#### 2.10.2 File Change Approval

```json
// Server sends:
{
  "jsonrpc": "2.0",
  "id": "srv-2",
  "method": "item/fileChange/requestApproval",
  "params": { "threadId": "...", "turnId": "...", "itemId": "...", ... }
}

// Client responds:
{
  "jsonrpc": "2.0",
  "id": "srv-2",
  "result": { "decision": "approved" }
}
```

#### 2.10.3 Permissions Request

```json
// Server sends:
{
  "jsonrpc": "2.0",
  "id": "srv-3",
  "method": "item/permissions/requestApproval",
  "params": { ... }
}

// Client responds:
{
  "jsonrpc": "2.0",
  "id": "srv-3",
  "result": {
    "permissions": "full-auto",
    "scope": "turn"
  }
}
```

#### 2.10.4 Tool User Input Request

```json
// Server sends:
{
  "jsonrpc": "2.0",
  "id": "srv-4",
  "method": "item/tool/requestUserInput",
  "params": {
    "threadId": "...",
    "turnId": "...",
    "questions": [...]
  }
}

// Client responds:
{
  "jsonrpc": "2.0",
  "id": "srv-4",
  "result": {
    "answers": { "q1": { "value": "..." } }
  }
}
```

#### 2.10.5 Other Server Requests

| Method | Description |
|--------|-------------|
| `execCommandApproval` | Legacy command approval |
| `applyPatchApproval` | Patch application approval |
| `mcpServer/elicitation/request` | MCP server user input request |
| `item/tool/call` | Tool call notification |
| `account/chatgptAuthTokens/refresh` | Auth token refresh |
| `attestation/generate` | Generate attestation |
| `currentTime/read` | Clock read (for sandboxed environments) |

### 2.11 Schema Generation

The complete protocol schema can be exported at any time:

```bash
# Standard schema
codex app-server generate-json-schema --out /tmp/schema

# Including experimental methods
codex app-server generate-json-schema --out /tmp/schema --experimental

# TypeScript bindings
codex app-server generate-ts --out /tmp/ts-bindings
```

This produces JSON Schema files for every request, response, and
notification type. The v2 schema bundle contains 571 type definitions.

### 2.12 Session Lifecycle for Secondary UI

```
 Secondary UI Client                  App-Server                 Model
       |                                  |                        |
       |--- initialize(clientInfo) ------>|                        |
       |<-- result(capabilities) ---------|                        |
       |--- initialized() -------------->|                        |
       |                                  |                        |
       |--- thread/list() -------------->|                        |
       |<-- result([threads]) -----------|                        |
       |                                  |                        |
       |--- thread/resume(threadId) ---->|                        |
       |<-- thread/started(thread) ------|                        |
       |<-- thread/status/changed(idle) -|                        |
       |                                  |                        |
  [User sends message on Feishu]          |                        |
       |                                  |                        |
       |--- turn/start(threadId, input)->|                        |
       |<-- result(turnId) --------------|                        |
       |<-- turn/started(turn) ----------|                        |
       |<-- item/started(agentMsg) ------|--- API call ---------->|
       |<-- item/agentMessage/delta -----|<-- streaming tokens ---|
       |<-- item/agentMessage/delta -----|<-- streaming tokens ---|
       |                                  |                        |
       |  [Agent wants to run a command]  |                        |
       |<== requestApproval(command) =====|                        |
       |=== response(accept) ============>|                        |
       |                                  |--- execute command --->|
       |<-- item/commandExecution/delta --|<-- command output -----|
       |<-- item/completed(cmdExec) ------|                        |
       |                                  |                        |
       |<-- item/completed(agentMsg) -----|                        |
       |<-- turn/completed(turn) ---------|                        |
       |<-- thread/status/changed(idle) -|                        |
```

---

## Part 3: Comparison and Implementation Guidance

### 3.1 Feature Matrix

| Capability | Claude daemon | Codex app-server |
|------------|---------------|------------------|
| Protocol format | Custom ndjson `{op, proto}` | JSON-RPC 2.0 |
| Official documentation | None (reverse-engineered) | Schema generator built in |
| Output format | Raw ANSI terminal stream + structured state patches | Structured text deltas (no ANSI) |
| User input injection | `reply` (plain text) | `turn/start` (structured, typed input) |
| Approval workflow | `permission-response` (limited info) | Full request/response with command details |
| Multi-client attach | `attach` (full PTY) + `subscribe` (read-only) | All connections are equal JSON-RPC peers |
| Transport | Unix socket only | Unix socket, WebSocket, stdio |
| Thread management | `list`, `has`, `kill` | Full CRUD (`start`, `resume`, `list`, `read`, `fork`, `delete`, `archive`) |
| Turn steering | Not available (can only `reply` when idle) | `turn/steer` redirects active turns |
| Token usage | Not exposed via protocol | `thread/tokenUsage/updated` notification |
| Model selection | Not exposed via protocol | `model/list`, per-turn `model` override |
| Schema stability | `proto` field version check, may break across releases | Versioned schema (v1/v2), `--experimental` flag |

### 3.2 Walkcode Integration Approaches

#### For Claude Code sessions:

1. **Read path**: Open a persistent `subscribe` connection per session.
   Parse `state` events for `tempo`, `needs`, `detail`, and `intent`.
   Ignore `stream` events (ANSI) unless you need to render a terminal
   preview.

2. **Write path**: When the user sends a message on Feishu, call `reply`
   with the daemon control key and the message text. The message appears
   in the TUI as if the user typed it.

3. **Approval path**: The current `subscribe` `state` events expose
   `needs` when a permission is pending, but the daemon does not push
   the full permission request payload through `subscribe`, and the
   `permission-response` op is a non-functional shell (§1.6.11). Neither
   `reply` (text injection) can answer a native permission prompt
   (live-verified: injecting "1" left the session blocked). The working
   approval channel is a **blocking PreToolUse hook** returning
   `hookSpecificOutput.permissionDecision` (and `updatedInput` for
   AskUserQuestion answer injection) — see
   `claude-daemon-multi-ui-sync.md` "交互闭环 v2".

4. **Authentication**: Read the daemon control key from
   `<CLAUDE_CONFIG_DIR>/daemon/control.key` at startup. The key is
   stable for the lifetime of the daemon process.

5. **Version guard**: Send `ping` on connect. Compare `proto` and
   `version` fields. If `proto != 1` or the major version changes,
   fall back to the hook-based observation mode.

#### For Codex sessions:

1. **Read path**: Connect to the app-server (Unix socket or WebSocket).
   Send `initialize` + `initialized`. Receive all notifications on the
   connection: `thread/status/changed`, `item/agentMessage/delta`,
   `item/commandExecution/outputDelta`, `turn/completed`, etc.

2. **Write path**: Send `turn/start` with `threadId` and structured
   `input` array. Receive `turn/started` notification, then streaming
   `item/agentMessage/delta` notifications, then `turn/completed`.

3. **Approval path**: The server sends JSON-RPC requests
   (`item/commandExecution/requestApproval`,
   `item/permissions/requestApproval`, etc.) and the client responds
   with a JSON-RPC result containing the decision. This is a full
   closed-loop: the Feishu card can render the command details and
   send back `accept` / `deny`.

4. **Thread discovery**: Use `thread/list` to discover existing threads.
   Use `thread/resume` to attach to a thread that was started by the
   TUI. Both the TUI and Feishu client see the same thread state.

5. **Schema guard**: Regenerate the schema when the Codex CLI updates.
   Compare definition counts or specific type shapes to detect breaking
   changes.

### 3.3 Recommended Architecture

```
                    +----------------------------+
                    |      walkcode runtime      |
                    |  channel_native_runtime.py |
                    +------+----------+----------+
                           |          |
               +-----------+          +------------+
               |                                   |
    +----------v----------+          +-------------v-----------+
    | ClaudeDaemonClient  |          | CodexAppServerClient    |
    |                     |          |                         |
    | subscribe()         |          | initialize()            |
    |  -> state events    |          | thread/resume()         |
    | reply(text)         |          | turn/start(input)       |
    |  -> inject input    |          |  -> all notifications   |
    | ping()              |          | respond to approvals    |
    |  -> version check   |          |  -> closed-loop         |
    +----------+----------+          +-------------+-----------+
               |                                   |
    +----------v----------+          +-------------v-----------+
    | Unix socket         |          | Unix socket / WebSocket |
    | control.sock        |          | app-server-control.sock |
    +---------------------+          +-------------------------+
```

Both clients feed events into the existing walkcode Orchestrator, which
renders Feishu/Lark cards. The `AgentTransport` interface already
abstracts the transport layer; each client type implements it.

### 3.4 Migration Path from Current Architecture

| Current mechanism | Replacement | Benefit |
|-------------------|-------------|---------|
| Claude hooks (observe) | `subscribe` state events | Real-time, no hook latency, structured data |
| Claude takeover (kill + resume) | `reply` on existing session | No process restart, no lost context |
| Codex headless spawn | `thread/start` via app-server | TUI and Feishu share the same thread |
| Permission card + hook callback | `permission-response` (Claude) / JSON-RPC approval (Codex) | Direct closed-loop, no side-channel |

---

## Appendix A: Verified Test Transcript (Claude daemon)

The following transcript was captured on 2026-07-04 against Claude Code
v2.1.201, daemon at `/tmp/cc-daemon-501/19e5f12f/control.sock`.

```
# 1. Ping
-> {"proto":1,"op":"ping"}
<- {"ok":true,"op":"ping","version":"2.1.201","proto":1}

# 2. List sessions
-> {"proto":1,"op":"list"}
<- {"ok":true,"op":"list","jobs":[
     {"short":"f6cb7f48","sessionId":"f6cb7f48-...","tempo":"active","state":"adopted",...},
     {"short":"5ca3e37c","sessionId":"5ca3e37c-...","tempo":"active","state":"adopted",...},
     ...
   ]}

# 3. Check session existence
-> {"proto":1,"op":"has","short":"5ca3e37c"}
<- {"ok":true,"op":"has","alive":true,"present":true,"ready":true}

# 4. Subscribe to session (read-only stream)
-> {"proto":1,"op":"subscribe","short":"5ca3e37c"}
<- {"type":"snapshot","record":{...},"streamTail":["<ANSI>",...]}\n
<- {"type":"state","patch":{"tempo":"idle","needs":""}}\n
   ... (continuous until session ends or client disconnects)

# 5. Inject user input (auth required)
-> {"proto":1,"op":"reply","auth":"<key>","short":"f6cb7f48","text":"echo hi"}
<- {"ok":true,"op":"reply"}

# 6. Observe response via subscribe (on a parallel connection)
<- {"type":"state","patch":{"tempo":"active","detail":"echo hi"}}
<- {"type":"stream","line":"<ANSI: Synthesizing...>"}
<- {"type":"stream","line":"<ANSI: agent response text>"}
<- {"type":"state","patch":{"state":"done","tempo":"idle"}}
```

## Appendix B: Codex App-Server Schema Export

The complete protocol schema was exported with:

```bash
codex app-server generate-json-schema --out /tmp/codex-schema --experimental
```

This produced 80+ JSON Schema files. Key files:

| File | Description |
|------|-------------|
| `ClientRequest.json` | All client-to-server request types |
| `ServerNotification.json` | All server-to-client notification types |
| `ServerRequest.json` | All server-to-client request types (approvals) |
| `ClientNotification.json` | Client-to-server notifications |
| `codex_app_server_protocol.schemas.json` | Bundled v1 schema (all definitions) |
| `codex_app_server_protocol.v2.schemas.json` | Bundled v2 schema (571 definitions) |

The v2 schema includes 193 distinct methods covering session management,
model interaction, file operations, plugin management, terminal control,
remote control, and approval workflows.

## Appendix C: Version Compatibility Notes

| Product | Tested version | Protocol version | Notes |
|---------|---------------|-----------------|-------|
| Claude Code | 2.1.201 | `proto: 1` | Daemon architecture introduced in 2.1.x. Socket path includes a hash that changes on daemon restart. |
| Codex CLI | Current (2026-07) | JSON-RPC 2.0, schema v1+v2 | `app-server` subcommand is marked `[experimental]`. |

Both protocols should be treated as unstable. Implement version checks
and graceful fallback to the existing hook-based mechanism when protocol
changes are detected.
