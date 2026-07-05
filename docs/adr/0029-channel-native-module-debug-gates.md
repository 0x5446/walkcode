# ADR 0029: Channel-native Module-level Debug Gates

Date: 2026-06-28

Status: Accepted

## Context

Running a full Telegram-to-agent E2E loop as the first diagnostic step is too
coarse. Telegram `getUpdates` offsets are destructive once confirmed, and a
misconfigured allowlist can consume a real user message without starting an
agent turn. That makes failures hard to reproduce.

## Decision

Add module-level diagnostics before full E2E:

- `scripts/channel_native_debug.py config` validates the V3 config surface and
  prints only sanitized keys and counts.
- `scripts/channel_native_debug.py agent` reports the configured agent
  capability status without starting a model turn.
- `scripts/channel_native_debug.py state` validates JSON state load and performs
  an atomic write/read probe against a temporary file next to the configured
  state path; it does not create or rewrite the configured state file unless an
  explicit repair flag is passed.
- `scripts/channel_native_debug.py outbox` reports durable outbox counts and
  runs synthetic sent/permanent/transient dispatch contracts without sending to
  a live channel.
- `scripts/channel_native_debug.py runtime` checks local `walkcode serve` and
  `walkcode native serve` consumer processes that could race the module-level
  Telegram probe. It uses launchd labels to distinguish the valid per-agent
  native services (`com.walkcode.telegram-claude` and
  `com.walkcode.telegram-codex`) from unmanaged duplicate consumers.
- `scripts/channel_native_debug.py agent-smoke` validates one agent adapter.
  It is a dry run by default; a real launch/turn requires explicit `--live`.
  In live mode, `session.error` events fail the gate instead of being counted
  as successful agent activity.
- `scripts/channel_native_debug.py telegram` inspects Telegram bot identity,
  webhook status, pending update shape, allowlist match, and known session
  match without confirming update offsets. When a pending message targets an
  existing session, it also checks whether submit would currently be accepted.
- `walkcode native debug telegram` exposes the same Telegram ingress diagnostic
  through the installed CLI.

The Telegram diagnostic calls `getUpdates` without an `offset`, so it can peek
at pending updates without consuming them. It marks `safe_to_run_serve_once=false`
when pending updates would be rejected by the allowlist or when a competing
local consumer process can consume Telegram updates first.

## Consequences

- Debug starts with reproducible module gates instead of a one-shot full E2E.
- A real user message is consumed only after the Telegram ingress gate says it
  is safe to run `serve --once`.
- State and outbox can be verified before any IM ingress or model turn.
- The polling runtime also drains persisted outbox entries on each iteration,
  including immediately after restart, so durable outbound retries are not
  coupled to the next inbound user update.
- Outbound transcript delivery has a single runtime-owned dispatcher. Session
  event drains and polling maintenance both call that dispatcher instead of
  constructing independent flushers. Each ready delivery is claimed with a
  lease before it is sent, and the claim/result is saved through the same state
  callback. This prevents concurrent in-process flushes from sending the same
  Telegram message twice.
- Deferred TUI hooks are drained with a recent-window priority before older
  backlog. This keeps live TUI observation responsive even if a previous
  service run left many historical hook files behind. The batch remains bounded
  per polling iteration so Telegram ingress still runs first.
- Runtime process conflicts are detected before Telegram smoke tests, including
  LaunchAgent-managed legacy `walkcode serve` processes.
- Claude and Codex Telegram services can run side by side without being
  reported as competing consumers, because each V3 runtime owns a separate
  bot/agent pair.
- Running active or waiting sessions with expired writer leases are reported as
  unsafe state, because a fresh one-shot runtime cannot submit into that stale
  handle. Completed `IDLE` sessions are handled by ADR 0030: they release the
  active lease and are resumed from durable transport state on the next input.
- `state --repair-stale-external-tui` may stop read-only external TUI sessions
  whose recorded process no longer exists. It creates a state backup first and
  never terminates a live process.
- Repairable submit failures such as `lease_expired` and capability-disabled
  transports do not complete the inbound ledger or confirm the Telegram offset.
- Agent adapter smoke is opt-in for live turns, so routine module checks do not
  spend model calls or mutate external sessions.
- Agent auth/provider failures are caught before IM updates are consumed.
- Secrets are not printed; diagnostics expose credential key names, counts, and
  booleans only.

## Verification

Contract tests cover:

- Telegram diagnostics do not send a confirming offset.
- disallowed pending updates block the next destructive `serve --once` step.
- pending updates targeting expired active-session leases block the next
  destructive `serve --once` step, while resumable `IDLE` sessions remain safe.
- state diagnostics do not create the configured state file when it is absent.
- state diagnostics fail when active or waiting sessions have expired writer
  leases.
- state repair stops dead external TUI observations with a backup and leaves
  live observed processes untouched.
- outbox diagnostics use synthetic dispatch and no live channel send.
- runtime diagnostics report competing consumers without printing full command
  lines or secrets.
- runtime diagnostics allow launchd-managed per-agent native services while
  still failing unmanaged native consumers.
- concurrent outbox flushers cannot send the same claimed delivery twice.
- Telegram diagnostics block `serve --once` when competing local consumers are
  present.
- agent smoke dry run does not launch a transport.
- agent smoke live mode fails on `session.error`, including authentication
  failures.
- CLI `native debug telegram` does not call the polling serve path.
- the repo script starts and exposes module-level commands.
