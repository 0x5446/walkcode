# ADR 0045: Per-session Workspace Selection (/repo)

Date: 2026-07-02

Status: Accepted

## Context

`WALKCODE_CWD` was an instance-level constant: every IM-initiated session of a
profile ran in the same directory, which makes a work profile that spans many
repositories impractical. The cwd was already a per-call parameter through
`handle_inbound_event` → `start_session`; only the runtime never varied it.

## Decision

- `WALKCODE_WORKSPACE_ROOTS` (colon-separated) defines an optional per-profile
  directory allowlist.
- `/repo <name-or-path> <task>` starts a new session in the resolved
  directory with `<task>` as its first turn. Directory and task travel in one
  message; there is no pending "choose a directory" state to persist.
- Resolution accepts a bare name (matched under each root) or a path; the
  candidate's realpath must sit inside a root's realpath, so neither `..`
  segments nor symlinks can escape the allowlist.
- `/repo` inside an existing session thread is rejected — cwd is bound at
  session start. Without configured roots the command reports the fixed
  `WALKCODE_CWD`. Sessions started without `/repo` keep using `WALKCODE_CWD`.

## Consequences

- The workspace allowlist doubles as a safety boundary: the work bot cannot be
  steered into personal repositories and vice versa, per profile env file.
- The health/status card already displays the session cwd, so the chosen
  directory is visible in the channel.

## Verification

`tests/test_channel_native_lark.py` proves: bare-name and absolute-path
resolution, `..` and symlink escape rejection, missing-directory reasons,
session started with the resolved cwd, usage/rejection replies, and the
in-session rejection.
