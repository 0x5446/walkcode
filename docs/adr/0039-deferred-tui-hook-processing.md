# ADR 0039: Deferred TUI Hook Processing

## Status

Accepted.

## Context

Claude Code and Codex TUI hooks run on the agent's interactive critical path.
The V3 hook command previously created Telegram topics, sent status cards, and
updated read-only state synchronously inside that hook process. Those Telegram
network calls can exceed short TUI hook timeouts, especially Codex's 5 second
hook timeout, causing the TUI to report hook failures and sometimes preventing
the observed session from appearing in Telegram.

Tool visibility had a related gap: transports could emit neutral tool events
for IM-started sessions, but TUI `PreToolUse`, `PostToolUse`, and permission
hooks were still treated as non-observation hooks and did not update Telegram.

Real Claude TUI smoke also exposed a startup gap: some sessions only produced a
late `Stop` summary because the local hook config did not include
`UserPromptSubmit` and `MessageDisplay`. In that state, Telegram had no early
event with enough context to create the read-only topic, so the TUI answer never
appeared in Telegram.

## Decision

`walkcode native hook` supports `--defer`.

With `--defer`, the hook command:

- reads the hook JSON from stdin;
- adds local WalkCode metadata such as the hook process id;
- writes one JSON file into a per-state hook spool directory;
- names the spool file with a nanosecond timestamp so same-turn hook files keep
  their local creation order instead of being reordered by process id;
- exits 0 without stdout unless `--json` was requested.

The running `walkcode native serve` process drains that local spool from an
independent maintenance task, then calls the same `process_tui_hook` runtime
path that direct hooks use. TUI hook drain, outbox flush, and loaded TUI binding
refresh are intentionally separate from Telegram `getUpdates` polling so a
stuck long-poll request cannot starve read-only TUI transcript sync.

Codex TUI observation requires these hook events in `~/.codex/hooks.json`:
`SessionStart`, `UserPromptSubmit`, `MessageDisplay`, `PreToolUse`,
`PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, and `Stop`. In
particular, `UserPromptSubmit` is what mirrors the TUI user's input into the
Telegram topic. Without it, Telegram can show the later assistant/Stop output
but cannot reconstruct the user's typed prompt.

The polling-cycle drain must not use a tiny timeout. Creating a Telegram forum
topic plus sending/pinning the status card can legitimately take several
seconds on a real network; cancelling that coroutine leaves the first hook
without a durable session and causes following `MessageDisplay` hooks to be
accepted as unobserved no-ops. The maintenance timeout is therefore sized for
Telegram side-effect latency, not for local file I/O latency.

TUI tool lifecycle hooks are now first-class observation events:

- `PreToolUse` / `pre-tool` -> `tool.started`
- `PermissionRequest` / `permission-request` -> `tool.started`
- `PostToolUse` / `post-tool` -> `tool.completed`
- `PostToolUseFailure` / `post-tool-failure` -> `tool.failed`
- `PermissionDenied` / `permission-denied` -> `tool.failed`

Only `sync` and `session-start` may claim an existing IM-started structured
session as external TUI-owned. `UserPromptSubmit` and `PreToolUse` may create a
new observed session when no binding exists, but they do not claim an existing
IM-started session. `MessageDisplay` forwards assistant-visible text to an
existing observed session.

Telegram still renders a single editable tool progress message per session and
does not show full tool output. Tool and permission observations keep the
session in `EXTERNAL_OBSERVED_READONLY`; they prove that the TUI is active, not
that Telegram has acquired the writer.

## Consequences

- TUI startup and tool hooks no longer block on Telegram network latency.
- TUI transcript sync is no longer gated by the Telegram polling loop returning
  successfully.
- A running Telegram service is required to make deferred TUI observations
  visible.
- TUI sync latency is bounded by the service's local spool drain cadence and
  Telegram side-effect latency rather than the hook command runtime.
- Tool calls from TUI sessions become visible in Telegram with the same compact
  activity UI as headless sessions while preserving readonly takeover semantics.

## Verification

Focused tests cover:

- deferred hook commands queue locally and stay stdout-silent;
- the runtime drains queued hooks and creates the observed Telegram session;
- missing `SessionStart` is covered by `UserPromptSubmit` or first
  `PreToolUse` fallback creation;
- `MessageDisplay` forwards Claude assistant text without leaking raw hook
  payload dictionaries;
- TUI tool hooks update one Telegram tool progress message without rendering
  raw tool output;
- non-observation hooks still return accepted no-op results.
