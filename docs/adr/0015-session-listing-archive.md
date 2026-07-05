# ADR 0015: Session Listing and Archive Boundary

Date: 2026-06-27

Status: Accepted

## Context

V3 keeps session list, close, interrupt, and archive as product capabilities. Interrupt and close now call transport control boundaries, but listing and archive still need a clean-slate contract.

Archive must not become a hidden process-control operation. Closing stops the transport; archiving only hides an already stopped session from default lists.

## Decision

Add a `SessionSummary` projection and registry-level listing filters by channel/account/chat/thread. Add archived metadata to `Session` and expose `Orchestrator.archive_session(...)` as an owner/admin-only control.

Archive is allowed only for stopped sessions. Running sessions return `session_running` and remain unchanged. Archived sessions are hidden from default lists and can be included explicitly.

## Consequences

- IM session menus can be built without exposing raw core session objects.
- Archive remains non-destructive and auditable.
- The command menu can show `archive` for stopped sessions without implying transport shutdown.

## Verification

Contract tests cover listing filters, running-session archive rejection, owner/admin authorization, default archive hiding, and JSON persistence of archived metadata.
