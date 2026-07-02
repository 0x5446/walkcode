# ADR 0032: Native TUI Hook Observation

Date: 2026-06-28

Status: Accepted

For Codex, ADR 0041 supersedes hook observation as the target architecture.
Hooks remain a fallback for unmanaged local TUI launches. Claude Code continues
to use this ADR's hook-based observation path unless a structured multi-client
protocol becomes available.

## Context

Channel-native V3 has two agent start modes:

- IM-started headless sessions, where WalkCode owns the structured transport
  and reads/writes through the agent's programmatic interface.
- TUI-started or TUI-resumed sessions, where the local TUI owns stdin/stdout and
  WalkCode must observe selected hook output without writing into the live TUI.

When an IM user sends input to a TUI-owned session, continuing to write to both
surfaces would break the single-writer invariant. The IM side must first
perform takeover: terminate the external TUI if authorized, resume the durable
agent session through the structured transport, then submit the blocked input.

## Decision

Add `walkcode native hook <hook_type> --agent claude|codex` as the channel-native
hook ingress. It reads one JSON object from stdin.

- The CLI accepts both WalkCode's migrated hook names and raw TUI hook event
  names such as `Stop`, `UserPromptSubmit`, and `PostToolUse`; the runtime
  normalizes them before processing.
- Real TUI hook configs should use `--defer`. The hook command persists a local
  hook event and exits immediately; the running `walkcode native serve` process
  performs Telegram topic/status/tool-progress side effects asynchronously.
- `sync` and `session-start` remain the only hooks that may claim an existing
  IM-started structured session as external TUI-owned.
- `sync`, `session-start`, `UserPromptSubmit`, and `PreToolUse` may create a
  new observed TUI session when no prior binding exists. `UserPromptSubmit` is
  the practical fallback for TUI sessions whose startup hook did not run, and
  `PreToolUse` preserves visibility when the first observable event is a tool
  call.
- `UserPromptSubmit` also sends a read-only transcript message to the channel
  when the prompt text is available. This mirrors what the TUI user typed; it is
  not submitted back to the agent because the TUI already delivered it.
- `MessageDisplay`, `stop`, `notification`, and `tui-output` may send
  user-visible text only to an existing observed session's channel binding.
- Tool lifecycle hooks are observation events when a session already exists:
  `PreToolUse` updates `tool.started`, `PostToolUse` updates
  `tool.completed`, and failures/denials update `tool.failed`.
- `stop`, `notification`, or `tui-output` with no existing observed session is
  accepted as a no-op. A late Stop hook must not create a new Telegram topic or
  claim an IM-started structured session.
- `stop` sends any final visible text but does not mark the observed session
  stopped. For Claude Code, `Stop` means the current turn ended; it does not
  prove the TUI process exited. Treating it as process exit breaks takeover
  safety because the TUI can still accept input.
- Only explicit process-exit style hook names may mark an observed TUI session
  stopped.
- Without `--json`, accepted hooks emit no stdout. Claude Code treats non-empty
  stdout as hook decision JSON for Stop hooks, so WalkCode's side-effect hook
  must stay silent unless the operator explicitly asks for JSON diagnostics.
- Hooks without a durable resume reference are accepted as no-op observations.
  The hook command must still exit 0 so the TUI does not report a hook failure.
- Non-session-observation events such as task, prompt, or subagent lifecycle
  hooks are accepted as no-op observations unless they are later promoted to a
  dedicated product flow.
- Duplicate hook events are deduped as accepted no-ops. They must not emit the
  same channel output twice, and they must not make the TUI command fail.
- Hooks that arrive after an IM-started durable session is already stopped are
  accepted as no-op observations. They must not reclaim or revive that stopped
  structured session, and they must not make the TUI command fail. If a
  previously observed external-TUI session was incorrectly marked stopped by an
  older runtime and a new claim-capable external TUI hook arrives, WalkCode may
  restore it to `EXTERNAL_OBSERVED_READONLY` and preserve the termination
  reference for safe takeover.
- Claude hooks identify the durable session with `agent_session_id`,
  `claude_session_id`, or `session_id`.
- Codex hooks identify the durable session with `thread_id` or
  `codex_thread_id`.
- The hook may carry `terminate_ref`; process termination is accepted only when
  the process ref includes `allow_terminate=true`.
- When no explicit `terminate_ref` is provided, `walkcode native hook` captures
  the hook process group and parent process tree before deferring. On macOS TUI
  runs, the foreground job's process group is usually the real `claude` or
  `codex` TUI PID, which is safer than relying only on transient shell parents.
- If no matching session exists, `sync`, `session-start`, `UserPromptSubmit`,
  or `PreToolUse` may create a new Telegram observed session rooted at a bot
  message in the configured TUI chat.
- Internal Codex/status events and raw hook handler traces are filtered instead
  of being forwarded to Telegram.

Telegram session placement remains channel-native:

- preferred: one native topic per session when the configured Telegram target
  supports forum/private bot topics;
- fallback: one session per root message/reply chain;
- not default: one Telegram group per session.

## Consequences

- TUI output can be observed without reviving the legacy tmux injection model.
- Codex hook observation is a compatibility path, not the long-term primary
  Codex TUI integration.
- IM takeover has a concrete process-control boundary and can be fully
  automated for authorized local processes.
- Unrecognized or unauthorized processes are never killed automatically.
- Telegram users should see agent output, takeover prompts, and progress states,
  not raw app-server status dictionaries or hook execution records. TUI user
  input is shown as `TUI input` transcript, distinct from agent output.
- Hook dedupe completion is persisted after the hook is accepted. A restarted
  runtime should not see accepted hook events as still in progress.
- Stop-hook completion is persisted as read-only observation progress, not as
  a stopped session.
- Late hooks for stopped IM-started sessions are idempotent no-ops. User input
  sent from Telegram into a TUI-origin topic is handled by the takeover flow.
- Late Stop hooks for sessions that were never observed do not create stale
  Telegram topics.
- Stale read-only observations whose recorded process is already gone are
  cleanup candidates for the debug state repair gate, not live takeover
  targets.

## Verification

Focused tests cover:

- hook-created observed Telegram sessions;
- hook claim of existing Claude and Codex structured sessions;
- duplicate hook dedupe through the inbound ledger;
- duplicate hook events returning accepted no-op results;
- stop hooks forwarding output while keeping observed sessions read-only and
  preserving takeover termination refs;
- hook process-group inference creates an authorized termination ref for a real
  external TUI process, while shell hook commands containing `claude`/`codex`
  are not misclassified as TUI processes;
- UserPromptSubmit fallback creation when SessionStart was absent;
- UserPromptSubmit mirroring the TUI user's input without calling the agent
  transport;
- MessageDisplay forwarding assistant text from Claude message content blocks;
- PreToolUse fallback creation followed by compact tool progress;
- Stop hooks without an existing observed session do not create Telegram topics;
- Stop hooks with a matching IM-started session do not claim or stop that
  session unless a prior `sync`/`session-start` hook already made it external
  TUI-owned;
- raw hook event names normalizing before session state changes;
- missing resume references returning accepted no-op results;
- non-session-observation events returning accepted no-op results;
- late stop/UserPromptSubmit hooks against stopped sessions;
- filtering of internal Codex status and hook handler output;
- `walkcode native hook` stdin dispatch;
- deferred hook queue write/drain behavior;
- TUI tool lifecycle hooks updating Telegram's single tool progress message;
- authorized and unauthorized `LocalProcessController` behavior;
- end-to-end takeover from Telegram through process termination, transport
  resume, and blocked-input submit.
