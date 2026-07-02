# ADR 0037: Headless Hook Self-observation Guard

Date: 2026-06-29

Status: Accepted

## Context

Claude Code headless sessions launched by WalkCode can inherit the user's
Claude hook configuration. Those hooks may call `walkcode native hook` from the
same Claude subprocess that WalkCode started through the headless SDK.

Without a guard, the native hook path sees the durable Claude session id,
matches the existing IM-owned structured session, and incorrectly treats the
hook as a real external TUI claim. The visible result is that a Telegram-owned
session can flip to `EXTERNAL_OBSERVED_READONLY` even though no human TUI took
over.

## Decision

Native TUI hooks now distinguish real external TUIs from WalkCode-owned
headless transport children before claiming a session.

For hook payloads with a `process_ref`, an inferred native hook parent
process, or a process-tree snapshot captured by `walkcode native hook` before a
deferred hook is queued:

- Claude hooks are ignored when the process tree contains the bundled
  `claude_agent_sdk/_bundled/claude` command or a Claude process running with
  both `--input-format stream-json` and `--output-format stream-json`.
- Codex hooks are ignored when the process tree contains `codex app-server
  --stdio`.
- Real TUI processes, such as `claude --settings ...` or interactive Codex
  commands, still use the existing observed-read-only and takeover flow.

`walkcode native hook` records `_walkcode_hook_parent_pid` and
`_walkcode_hook_process_tree` before `--defer` writes the hook to disk. This is
required because the hook process, and often the agent process itself, may have
exited by the time the long-running runtime drains the deferred file.

When a claim-capable hook, such as `SessionStart`/`sync`, matches an existing
WalkCode-owned headless session but has no verifiable external TUI process
identity, the hook is treated as internal self-observation and ignored. A bare
durable session id is not sufficient to move writer ownership away from the
orchestrator. Non-claiming hooks, such as a late `Stop`, keep their existing
unobserved no-op behavior unless the process tree explicitly identifies a
WalkCode-owned headless process.

Ignored self-observation hooks return accepted success with
`internal_headless_hook_ignored` and do not update writer ownership, generation,
session binding, or status cards.

## Consequences

- IM-owned Claude/Codex sessions remain writable by WalkCode even if user-level
  agent hooks are installed globally.
- Takeover-resumed Claude/Codex sessions do not send a duplicate final message
  through the external TUI hook path when the resumed headless process emits a
  late global Stop hook.
- External TUI observation remains available, but it depends on process
  identity instead of blindly trusting any matching durable session id.
- This keeps the V3 rule intact: the product session is the agent session;
  Telegram topic ids are only channel bindings, and hook events do not become a
  second writer unless they come from a real external TUI.

## Verification

Contract tests cover:

- a matching Claude hook from a WalkCode-owned headless SDK subprocess is
  accepted and ignored;
- a matching deferred hook with a captured headless process-tree snapshot is
  accepted and ignored;
- a matching hook without process identity cannot reclaim an orchestrator-owned
  headless session;
- a matching hook from a real Claude TUI still claims the structured session as
  `EXTERNAL_OBSERVED_READONLY`.
