# ADR 0031: Structured Session External TUI Handoff

Date: 2026-06-28

Status: Accepted

## Context

An IM user can start a structured headless session from Telegram or Lark. The
same underlying Claude/Codex conversation can later be resumed from a local TUI.
Once that happens, the TUI is the best interactive writer and the IM thread must
not keep submitting input to the headless transport. Allowing both writers would
reintroduce the lost-input and double-submit races V3 is designed to remove.

This is the reverse direction of observed takeover:

- observed takeover: external TUI owns writer, IM asks to take over;
- external TUI handoff: IM owns structured writer, external TUI resumes and
  claims writer.

## Decision

Add an explicit external writer claim boundary.

- A TUI resume can claim an existing structured session only through a
  WalkCode-aware surface. The first implemented surface is
  `walkcode native hook ...`, which reads hook JSON on stdin and reports a
  durable agent session id matching an IM session.
- The claim moves the session to read-only observation for IM:
  - increment `generation`;
  - set `writer_owner.kind=external_tui`;
  - clear the active `writer_lease`;
  - set `lifecycle_state=EXTERNAL_OBSERVED_READONLY`;
  - store a structured `resume_ref` for future IM takeover;
  - store a `terminate_ref` when the TUI process can be stopped cleanly.
- The TUI process identity is a temporary writer lease. It is persisted only so
  runtime startup and takeover can validate whether the same local TUI still
  owns the session. A stale pid is never treated as proof that takeover has
  safely stopped the current writer.
- If startup detects that the stored TUI process no longer exists, the observed
  topic is moved out of live read-only ownership:
  - `EXTERNAL_DETACHED_IMPORTABLE` when a durable `resume_ref` remains;
  - `EXTERNAL_DETACHED_UNIMPORTABLE` when no durable resume reference exists.
- IM output observation may continue through hooks, but IM input is no longer
  submitted directly. It is stored as blocked input and rendered with the
  existing takeover UX.
- If the TUI was launched outside WalkCode and no hook reports the claim, the
  runtime cannot reliably infer ownership from state alone.
- Telegram placement is capability-driven. Native topic-per-session is the
  preferred UX where Telegram or Lark can provide topics; root reply-chain is
  the fallback. `thread_id` alone is not the session identity.
  Group-per-session is not the default runtime model. See ADR 0033.

## Consequences

- IM-started sessions and TUI-resumed sessions share the same single-writer
  invariant.
- Existing takeover confirmation and termination rules can be reused when an IM
  user wants to take the session back.
- Stale IM callbacks from before the TUI claim are invalidated by generation
  change.
- A live TUI claim without terminate capability still makes IM read-only, but
  future IM takeover becomes manual-only unless another controller can stop the
  TUI.
- Once the TUI process is detached, IM takeover no longer attempts termination;
  it can continue only if the stored agent session can be resumed.

## Verification

Contract tests cover:

- structured-to-external handoff changing writer ownership and generation;
- IM input after handoff being blocked instead of submitted;
- stale handoff claims being rejected.
- startup reconciliation marking stale TUI process references detached instead
  of leaving them as live read-only writers.
- `walkcode native hook` claiming existing Claude/Codex structured sessions by
  durable resume id.
