# Channel-native V3 Implementation Plan

Date: 2026-06-27

Source design: `/Users/alpha/.walkcode/workspace/walkcode-redesign-channel-native-v3.md`

## Implementation Discipline

Each slice follows SDD + TDD:

1. Update design or ADR for the slice.
2. Write failing contract tests.
3. Implement the smallest code needed.
4. Run the focused tests.
5. Run the full local test suite before moving to the next slice.

The current verified baseline is:

```text
uv run --with pytest python -m pytest tests/test_channel_native_*.py -q
297 passed
```

## Slice 1: New Core Contracts

Status: implemented and locally verified.

Goal: prove the clean-slate core can run without Telegram, Lark, tmux, hooks, or real agent CLIs.

Scope:

- `ChannelBinding`, `ChannelCapabilities`, `InboundEvent`, `ViewModel`.
- `TransportCapabilities`, `LaunchSpec`, `TransportHandle`, `TurnInput`, `AgentEvent`.
- `SessionRegistry` with `writer_owner`, `writer_lease`, `generation`, `blocked_inputs`, and pending binding.
- `InteractionStore` with short tokens, write-once decisions, and stale generation checks.
- `DurableOutbox` with retry semantics.
- fake channel and fake transport for contract tests.
- minimal Orchestrator turn flow.

Non-goals:

- Telegram API integration.
- Lark OpenAPI integration.
- Claude SDK integration.
- Codex app-server integration.
- old state import.
- TUI takeover process control.

Acceptance:

- fake session can start, submit input, stream delta, and render completion.
- duplicate callback cannot change a decision.
- transient delivery remains queued; permanent failure is recorded and removed from active queue.
- pending binding can be resolved and committed.
- unknown agent event renders as fallback text.
- capability-disabled operations return explicit blocked results.
- stale generation input/callback does not execute.
- ~~expired writer lease blocks submit until recovered.~~ Withdrawn by
  ADR 0059: lease expiry never blocks submits; the lease is bookkeeping only.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_core.py
9 passed

uv run --with pytest python -m pytest
484 passed, 4 warnings
```

Implemented files:

- `src/walkcode/channel_native/__init__.py`
- `tests/test_channel_native_core.py`

## Slice 2: Telegram + Claude Headless

Status: adapter/transport boundary implemented with mocked external dependencies.

Goal: run the first real Channel + Transport vertical path while keeping every unverified external capability behind capability gates.

Scope:

- `TelegramChannelAdapter` with long polling, send/edit, inline callback parsing, and file metadata download boundaries.
- `ClaudeHeadlessTransport` wrapper with dynamic SDK import, explicit missing-SDK error, launch/submit/event conversion, and capability reporting.
- Orchestrator support for inbound text and callback routing through `SessionRegistry` and `InteractionStore`.
- focused tests using fake HTTP and fake Claude client.

Non-goals:

- production webhook deployment.
- Lark adapter.
- Codex app-server.
- real Telegram bot E2E without `TELEGRAM_BOT_TOKEN` and allowed chat config.

Acceptance:

- Telegram inbound private text can create a Claude headless session through mocked transport.
- Telegram callback uses short token and write-once interaction decision.
- Telegram long output respects message size boundaries in adapter tests.
- Missing Claude SDK fails explicitly and does not claim transport availability.
- Full local tests remain green.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_telegram_claude.py
6 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py
15 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
490 passed, 4 warnings
```

Implementation notes:

- `TelegramChannelAdapter` currently supports update parsing and send-message rendering behind fakeable `TelegramBotApi`.
- `ClaudeHeadlessTransport` does not silently enable itself when `claude_agent_sdk` is missing. It reports disabled capabilities and raises `TransportUnavailable`.
- Real Telegram bot E2E remains gated on `TELEGRAM_BOT_TOKEN` and allowed chat config.
- Real Claude SDK E2E remains gated on installing the SDK and validating local auth/provider routing.

## Slice 3: Lark Peer ChannelAdapter

Status: peer adapter boundary implemented with mocked OpenAPI caller.

Goal: bring Lark/Feishu back as a peer IM adapter without reusing old `server.py` control flow.

Scope:

- `LarkChannelAdapter` with OpenAPI caller injection.
- parse message events and card callbacks into `InboundEvent`.
- render post/card-like view models through fakeable API calls.
- preserve thread/root semantics inside `ChannelBinding`, not core fields.
- tests for Lark thread message, callback token parsing, card update fallback, and orchestrator inbound flow.

Non-goals:

- real Lark WebSocket startup.
- real app credentials.
- old `FEISHU_*` runtime config compatibility.

Acceptance:

- Lark thread text can create or continue the same kind of structured session as Telegram.
- Lark card callback exposes the same short-token shape used by `InteractionStore`.
- Lark rendering can send interaction/status views without leaking Feishu field names into core state.
- Full local tests remain green.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_lark.py
4 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py
19 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
494 passed, 4 warnings
```

Implementation notes:

- `LarkChannelAdapter` accepts an injected `LarkBotApi` caller.
- Lark `root_id` stays inside `ChannelBinding.thread_id/root_message_id`; no Feishu-named field is added to core state.
- Real Lark WebSocket/OpenAPI E2E remains gated on app credentials and scopes.

## Slice 4: Codex App-server Transport

Status: structured transport boundary implemented with injectable app-server client.

Goal: add the second structured agent transport without claiming unverified active-turn fan-out or permission equivalence.

Scope:

- `CodexAppServerTransport` with injectable JSON message client.
- `thread/start`, `turn/start`, `thread/resume`, and event conversion.
- capability gates for active fan-out and permission approval.
- tests for request shapes, non-ephemeral resume-after-complete, and unsupported capability behavior.

Non-goals:

- default daemon discovery.
- TUI co-connection.
- permission approval parity.
- active turn multi-client subscription.

Acceptance:

- transport sends expected app-server method payloads.
- delta/completed events convert to `AgentEvent`.
- resume is available only when the caller provides a thread id.
- active fan-out and permission approval remain disabled by default.
- Full local tests remain green.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_codex.py
4 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py
23 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
498 passed, 4 warnings
```

Implementation notes:

- `CodexAppServerTransport` sends `thread/start`, `thread/resume`, and `turn/start` through an injected message client.
- `turn/start` includes the app-server-required text item shape with `text_elements: []`.
- Permission approval, active-turn resume, and multi-client fan-out remain disabled by capabilities.

## Slice 5: Observed Takeover Transaction

Status: implemented and locally verified.

Goal: make external TUI observed sessions safe by turning IM input into a persisted blocked input and takeover transaction, not direct injection or unconditional process kill.

Scope:

- `TakeoverTransaction` model.
- transaction creation from `blocked_input`.
- authorization, recoverability check, manual-only state, and completion state.
- tests for no native resume, stale generation, and successful transition to structured ownership.

Non-goals:

- real process termination.
- real Claude/Codex resume.
- TUI screen parsing.

Acceptance:

- observed session input is blocked and retained.
- takeover without resume ref becomes manual-only and does not change writer owner.
- successful takeover increments generation, moves writer owner to orchestrator, and marks blocked input submitted.
- stale takeover actions cannot execute.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py
26 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
502 passed, 4 warnings
```

Implementation notes:

- `SessionRegistry` stores `TakeoverTransaction` records separately from blocked inputs.
- `authorize_takeover(..., resume_ref=None)` produces `manual_only` and leaves the external TUI writer unchanged.
- `complete_takeover(...)` is the only transition that moves writer ownership to the orchestrator, creates a new writer lease, increments generation, and marks the blocked input submitted.
- The slice intentionally does not kill a real process or inject text into a TUI. Those actions remain outside the contract until a process-control adapter is explicitly designed and tested.

## Slice 6: Legacy State Cutover Boundary

Status: superseded by the V3 hard cut.

Goal: make legacy state a cleanup/blocker concern, not a runtime input.

Scope:

- detect old launchd, hook, shell-wrapper, and Feishu env remnants in install,
  upgrade, and debug gates.
- require a clean V3 env before local deploy validation.
- document that old state may be inspected manually, but is never imported into
  V3 sessions.

Non-goals:

- loading old `state.json` into the V3 runtime.
- converting old Feishu/Lark root ids into active `ChannelBinding` records.
- treating shell wrapper or terminal state as a structured Claude/Codex resume
  reference.
- runtime compatibility mode.

Acceptance:

- V3 starts only from `ChannelNativeConfig` and its own state file.
- legacy remnants fail release/debug gates with actionable cleanup guidance.
- no `LegacyStateImporter` or equivalent importer is exported by the runtime.
- old terminal-only records are not classified as resumable sessions.

Verification:

```text
uv run --with pytest python -m pytest
pending after Slice 36 hard-cut edits
```

Implementation notes:

- Legacy cleanup belongs to `install.sh`, `upgrade.sh`, and
  `scripts/channel_native_debug.py`.
- Runtime code has no importer that maps old Feishu/tmux state into live V3
  sessions.
- TUI observation starts from native hook payloads and explicit process-control
  metadata, not old state records.

## Slice 7: ViewModel and Interaction Rendering Contracts

Status: implemented and locally verified.

Goal: keep core rendering platform-neutral while still proving Telegram and Lark can render permission, AskUserQuestion, health, error, command, and takeover views.

Scope:

- typed view model builders for permission, AskUserQuestion, health, error, command menu, and takeover state.
- callback token wiring through `InteractionStore`.
- Telegram inline keyboard payload shape.
- Lark card-like payload shape.
- tests that verify no Telegram HTML or Lark card JSON is required in core state.

Non-goals:

- pixel-perfect Lark cards.
- real Telegram callback acknowledgement.
- full AskUserQuestion multi-step transport integration.

Acceptance:

- permission prompts create short callback tokens and render actions on both Telegram and Lark.
- AskUserQuestion supports option tokens and an `Other` awaiting state.
- health/error/command/takeover views render as platform-neutral view models.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_views_auth_outbox.py
8 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py
35 passed

uv run --with pytest python -m pytest
510 passed, 4 warnings
```

Implementation notes:

- `ViewModelFactory` generates platform-neutral dict view models.
- Telegram rendering converts view `actions` into inline keyboard callback data using short tokens.
- Lark rendering passes the same view through the card-like adapter boundary without adding Feishu fields to core state.
- AskUserQuestion `Other` is stored as an awaiting binding state in `InteractionStore`.

## Slice 8: Channel Authorization Model

Status: implemented and locally verified.

Goal: enforce the owner/collaborator/reviewer/admin model before IM input or high-risk actions reach a transport.

Scope:

- `AuthorizationStore` with per-session roles.
- role checks for submit, permission decisions, high-risk decisions, and takeover.
- Orchestrator submit gate.

Non-goals:

- real Telegram/Lark member admin lookup.
- organization-wide policy engine.

Acceptance:

- session creator becomes owner.
- collaborator can submit input but reviewer cannot.
- only owner/admin can approve high-risk actions or takeover.
- denied input is explicit and does not call transport.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_views_auth_outbox.py
8 passed

uv run --with pytest python -m pytest
510 passed, 4 warnings
```

Implementation notes:

- `AuthorizationStore` owns per-session role grants and audit events.
- `Orchestrator.start_session(...)` grants the creator owner when an authorization store is configured.
- `Orchestrator.submit_user_input(...)` rejects unauthorized actors before transport validation or submission.

## Slice 9: Durable Outbox Dispatch and Inbound Ledger

Status: implemented and locally verified.

Goal: turn the existing outbox primitive into a dispatch contract and prevent duplicate inbound events from starting duplicate turns.

Scope:

- `InboundLedger` for event id dedupe.
- `OutboxDispatcher` that sends queued view models through `ChannelAdapter`.
- transient/permanent adapter failures mapped back into `DurableOutbox`.
- Orchestrator event output enqueued before delivery.

Non-goals:

- persistent database backend.
- background worker scheduling.

Acceptance:

- duplicate inbound event is ignored.
- turn output is enqueued into the outbox before delivery.
- transient send failure remains pending.
- permanent send failure moves to dead queue.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_views_auth_outbox.py
8 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
510 passed, 4 warnings
```

Implementation notes:

- `InboundLedger` rejects duplicate event ids before session routing.
- `OutboxDispatcher` maps adapter exceptions to sent/transient/permanent delivery state.
- `Orchestrator` now enqueues transport output into `DurableOutbox` before dispatching to a channel.

## Slice 10: Persistence, Retry Backoff, and Async IO Hardening

Status: implemented and locally verified.

Goal: address the independent review's reliability findings before adding more channel surface.

Scope:

- atomic JSON snapshot persistence for session registry, interactions, outbox, authz, and inbound ledger.
- outbox retry delay and maximum attempts before moving persistent transient failures to dead queue.
- two-phase inbound ledger handling so exceptions do not permanently consume an event.
- non-blocking Telegram HTTP call boundary.

Non-goals:

- production database selection.
- background scheduler.
- real Telegram E2E.

Acceptance:

- blocked inputs, writer leases, roles, callback tokens, and pending outbox items survive save/load.
- transient outbox failures are retried only after backoff and eventually move to dead queue.
- inbound events that fail with an exception can be retried.
- Telegram's real HTTP branch does not block the event loop directly.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_persistence_reliability.py
5 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py
40 passed

uv run python -m compileall -q src/walkcode/channel_native
passed

uv run --with pytest python -m pytest
515 passed, 4 warnings
```

Implementation notes:

- `JsonFileStateStore` writes atomic JSON snapshots for sessions, interactions, outbox, authz, and inbound ledger.
- `DurableOutbox` now tracks `next_attempt_at`, `last_error`, retry delay, and `max_attempts`.
- `InboundLedger` now has `start/complete/fail` so exceptions do not permanently consume an inbound event.
- `TelegramBotApi` runs the real `urllib` call through `asyncio.to_thread`.

## Slice 11: Streaming Event Boundary and Registry Guardrails

Status: implemented and locally verified.

Goal: close medium-risk contract gaps found during independent review.

Scope:

- support transports whose `events(...)` returns an async iterator, not only a buffered list.
- enforce external-TUI-only blocked input inside `SessionRegistry`, not only in Orchestrator.
- move neutral view text rendering out of Telegram adapter so Lark does not depend on Telegram helpers.

Non-goals:

- full production streaming coalescer.
- real Claude SDK stream E2E.

Acceptance:

- Orchestrator can drain an async event stream and render each event.
- `SessionRegistry.block_input(...)` rejects structured sessions.
- Lark and Telegram both render via a shared neutral view text helper.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_streaming_guardrails.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_streaming_guardrails.py
43 passed

uv run --with pytest python -m pytest
518 passed, 4 warnings

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `Orchestrator` now drains either a buffered event list or an async event iterator.
- `SessionRegistry.block_input(...)` enforces the external TUI writer invariant directly.
- `render_view_text(...)` is the shared neutral text helper used by channel adapters.

## Slice 12: Retention and Compaction Policies

Status: implemented and locally verified.

Goal: close the remaining durability risk where interaction tokens, completed interactions, sent deliveries, and dead-lettered deliveries could grow without bound.

Scope:

- explicit creation and expiry timestamps for interaction contexts.
- `InteractionStore.compact()` for expired tokens, expired open interactions, decided interactions after retention, and stale `Other` awaiting bindings.
- delivery completion timestamps for sent/dead outbox items.
- `DurableOutbox.compact()` for sent and dead-letter retention windows.
- persistence round-trip for retention configuration and completion timestamps.

Non-goals:

- database partitioning or archival storage.
- background scheduling.
- product analytics export.

Acceptance:

- expired callback tokens and expired unresolved interactions are removed together.
- decided interactions remain auditable during their retention window and are removed afterward.
- `Other` awaiting bindings are removed when their parent interaction is compacted.
- sent and dead-letter outbox records retain enough audit data for their configured window and are pruned afterward.
- full local tests remain green.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_persistence_reliability.py
9 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_streaming_guardrails.py
47 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `InteractionContext` now stores `created_at` and `expires_at`.
- `InteractionStore.compact()` removes expired tokens/interactions and stale `Other` awaiting bindings.
- `DeliveryItem` now stores `finished_at` for sent and dead-lettered records.
- `DurableOutbox.compact()` prunes sent and dead records by separate retention windows.
- `JsonFileStateStore` persists retention settings and delivery completion timestamps.

## Slice 13: AskUserQuestion State Machine

Status: implemented and locally verified.

Goal: make AskUserQuestion a channel-neutral interaction flow instead of a single-question rendering helper.

Scope:

- multi-question progression inside `InteractionStore`.
- single-select answer tokens.
- multi-select toggle tokens and explicit submit tokens.
- `Other` callback tokens that enter an awaiting-text state bound to the channel binding.
- inbound callback routing in `Orchestrator`.
- inbound text routing to complete a pending `Other` answer before it is treated as a normal agent turn.

Non-goals:

- transport-specific delivery of final answers back to Claude/Codex.
- rich per-platform UI polish.
- cross-device draft editing.

Acceptance:

- answering the first of multiple questions advances `current_index` without finalizing the interaction.
- the final question creates one write-once decision containing all answers.
- multi-select questions can toggle options and require submit before finalization.
- `Other` enters awaiting state through the same callback-token path used by Telegram/Lark.
- text sent while awaiting `Other` completes the answer and is not submitted to the agent as a normal turn.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_views_auth_outbox.py
12 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_streaming_guardrails.py
51 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `InteractionStore` now treats AskUserQuestion as a multi-step state machine rather than a one-shot decision.
- Single-select answers advance until the final question creates the write-once `answers` decision.
- Multi-select questions use `toggle` tokens and explicit `submit` tokens.
- `Other` tokens bind an awaiting-text state to `ChannelBinding.key()`.
- `Orchestrator` routes callbacks to `InteractionStore` and gives pending `Other` text priority over normal agent turn submission.

## Slice 14: Channel-native Runtime Configuration

Status: implemented and locally verified.

Goal: make configuration match the clean-slate architecture: one runtime instance selects one IM channel, Telegram/Lark remain peer adapter types, and legacy Feishu env only acts as a conversion source.

Scope:

- `ChannelNativeConfig.from_env(...)` for selected channel, bound agent, state path, and cwd selection.
- Telegram endpoint config from `TELEGRAM_*`.
- Lark endpoint config from `LARK_*`.
- single-channel validation: `WALKCODE_CHANNEL=telegram|lark` is required and
  credentials never infer the selected channel.
- single agent binding with product names: `WALKCODE_AGENT=claude|codex`.
- one-shot `FEISHU_*` to `LARK_*` conversion report that does not feed runtime config.

Non-goals:

- replacing the legacy `Config` class used by the current server.
- reading secrets from a secret manager.
- launching real channel adapters from config.

Acceptance:

- Telegram config selects Telegram as the single runtime channel.
- Lark config selects Lark as the single runtime channel.
- Telegram + Lark in one runtime config is rejected; run two instances if both ingress channels are needed.
- `WALKCODE_AGENT=codex` selects Codex internally without exposing `codex_app_server` as user-facing config.
- `FEISHU_*` alone does not configure a runtime channel.
- conversion report maps known `FEISHU_*` keys to `LARK_*` suggestions with an explicit warning.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_config.py
8 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
154 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ChannelNativeConfig` is separate from the legacy server `Config`.
- Telegram requires both `WALKCODE_CHANNEL=telegram` and `TELEGRAM_BOT_TOKEN`.
- Lark requires `WALKCODE_CHANNEL=lark` plus its `LARK_*` credentials.
- Lark and Telegram remain peer adapter types, but one local runtime instance binds one selected channel.
- `AgentTransport` remains the internal implementation boundary; `.env` uses `WALKCODE_AGENT`.
- `LegacyFeishuEnvConverter` maps known `FEISHU_*` variables into suggested `LARK_*` variables without activating runtime config.

## Slice 15: Real E2E Gate Harness

Status: implemented and locally verified.

Goal: make real external verification explicit and safe instead of silently skipped or accidentally executed.

Scope:

- named E2E gates for Telegram, Lark, Claude headless, and Codex app-server.
- each gate requires an explicit `WALKCODE_E2E_*` opt-in flag.
- each gate reports missing environment variables.
- helper tests that skip with concrete reasons when credentials or target ids are absent.

Non-goals:

- performing real Telegram/Lark/Claude/Codex calls without credentials.
- embedding secrets in repo files.
- claiming external E2E success when gates are skipped.

Acceptance:

- with no E2E env, gates are disabled with actionable reasons.
- with opt-in flag but missing credentials, gates stay disabled and list missing variables.
- with all required env, gates are enabled.
- repo tests can include real E2E placeholders that skip cleanly when gates are closed.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_e2e_gates.py
5 passed

uv run --with pytest python -m pytest tests/test_channel_native_core.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py tests/test_channel_native_codex.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_streaming_guardrails.py tests/test_channel_native_config.py tests/test_channel_native_e2e_gates.py
61 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ChannelNativeE2EGates` defines explicit gates for Telegram, Lark, Claude headless, and Codex app-server.
- Every real E2E gate requires an opt-in `WALKCODE_E2E_*` flag before credentials are considered.
- Closed gates return concrete missing-variable lists and skip-ready reasons.
- The harness does not perform external calls by itself.

## Slice 16: Session Controls and Transport Control Boundary

Status: implemented and locally verified.

Goal: turn command-menu controls into real session state transitions and transport calls, instead of only rendering platform-neutral menu views.

Scope:

- `ControlResult` contract for user/agent control operations.
- `AuthorizationStore.can_control_session(...)` for owner/admin-only high-risk controls.
- `AgentTransport.interrupt(...)` and `AgentTransport.shutdown(...)` boundary methods.
- `Orchestrator.interrupt_session(...)` and `Orchestrator.close_session(...)`.
- stopped-session guard in `SessionRegistry.validate_submit(...)`.
- capability-gated command menu actions generated by the orchestrator.

Non-goals:

- real Claude/Codex SDK interrupt E2E.
- process killing for external TUI sessions.
- checkpoint rewind or model switching.

Acceptance:

- owner/admin can interrupt an active structured session when the transport advertises interrupt support.
- collaborator/reviewer cannot interrupt or close a session.
- interrupt capability disabled returns `capability_disabled` and does not call the transport.
- closing a session marks it stopped, records the stop reason, calls transport shutdown when available, and blocks later submits.
- command menu actions reflect both authorization and transport capabilities.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_controls.py
5 passed
```

## Slice 17: Attachment Intake Boundary

Status: implemented and locally verified.

Goal: enforce the channel-native attachment boundary before files reach an agent transport.

Scope:

- `ChannelAdapter.download_attachment(...)` contract.
- Orchestrator attachment preparation before `submit_turn(...)`.
- capability gate for channels that cannot download attachments.
- Telegram photo/document parsing into `AttachmentRef`.
- fake adapter support for deterministic attachment download tests.

Non-goals:

- real Telegram file-download E2E.
- Lark binary download implementation.
- file size scanning, malware scanning, or storage quotas.

Acceptance:

- inbound attachments from a capable channel are downloaded before transport submit.
- downloaded `AttachmentRef.local_path` is passed to `TurnInput`.
- attachment input from an incapable channel returns `capability_disabled` and does not call the transport.
- Telegram photo/document updates produce attachment refs with source ids and mime hints.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_attachments.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_controls.py tests/test_channel_native_attachments.py
8 passed
```

Implementation notes:

- `Orchestrator.prepare_turn_from_inbound(...)` normalizes channel file ids before transport submission.
- `TelegramChannelAdapter` parses photo/document updates and downloads files through `getFile` plus the Telegram file endpoint.
- Channels that report `attachment_download=false` reject attachment-bearing input before a transport turn is created.

## Slice 18: Channel Routing and Active Binding Resolution

Status: implemented and locally verified.

Goal: make Telegram-first routing usable without leaking Telegram/Lark-specific fields into core state.

Scope:

- exact binding lookup remains highest priority.
- rootless inbound messages can continue a single active session in the same channel/account/chat/thread.
- ambiguous rootless inbound messages are rejected explicitly instead of being guessed.
- stopped sessions are ignored by active fallback routing.
- Telegram private chat and forum topic flows are covered by contract tests.

Non-goals:

- slash-command session selection UI.
- cross-chat recent-session heuristics.
- real Telegram group administrator lookup.
- Lark WebSocket runtime routing.

Acceptance:

- a second Telegram private message continues the existing private session when it is the only active session.
- a second Telegram forum topic message continues the topic session when it is the only active session in that topic.
- a Telegram reply-to-root message still resolves by exact root binding.
- a rootless message in a chat/thread with multiple active sessions returns `ambiguous_session` and does not submit a turn.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_routing.py
4 passed

uv run --with pytest python -m pytest tests/test_channel_native_telegram_claude.py tests/test_channel_native_routing.py tests/test_channel_native_attachments.py
13 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `SessionRegistry.resolve_active_binding(...)` keeps exact root matching first.
- Rootless inbound messages fall back only when one running session matches the same channel/account/chat/thread.
- Multiple active candidates return `ambiguous_session`, which stops Orchestrator before transport submission.

## Slice 19: Callback Acknowledgement Boundary

Status: implemented and locally verified.

Goal: keep callback acknowledgement platform-specific while ensuring Orchestrator does not leave IM clients waiting during permission, AskUserQuestion, and takeover actions.

Scope:

- `ChannelAdapter.ack_callback(...)` contract.
- Orchestrator best-effort callback acknowledgement before decision handling.
- Telegram `answerCallbackQuery` adapter mapping.
- Lark callback acknowledgement adapter boundary.
- fake adapter callback acknowledgement tracking.

Non-goals:

- rich callback toast copy.
- asynchronous long-running action progress.
- real Telegram/Lark callback E2E.

Acceptance:

- Telegram callback events call `answerCallbackQuery` before token decision work.
- invalid callback tokens are still acknowledged when the channel supports callback ack.
- channel adapters without callback ack support do not block callback decision handling.
- ack API details stay inside `TelegramChannelAdapter` and `LarkChannelAdapter`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_callback_ack.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_callback_ack.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_lark.py
27 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ChannelAdapter.ack_callback(...)` is the channel-neutral boundary.
- `Orchestrator` acknowledges callback events before token decision handling when `private_callback_ack=true`.
- Telegram maps the boundary to `answerCallbackQuery`; Lark keeps the acknowledgement API behind `LarkChannelAdapter`.

## Slice 20: Lark Attachment Intake Parity

Status: implemented and locally verified.

Goal: keep Lark as a peer IM adapter by giving it the same attachment intake boundary as Telegram.

Scope:

- extend `AttachmentRef` with source message context needed by Lark resource download.
- parse Lark image/file message content into `AttachmentRef`.
- implement `LarkChannelAdapter.download_attachment(...)` through the injected Lark API boundary.
- preserve generic Orchestrator attachment normalization for Lark inbound messages.

Non-goals:

- real Lark OpenAPI binary E2E.
- post-message rich text reconstruction beyond attachment discovery.
- malware scanning, quota management, or durable blob storage.

Acceptance:

- Lark image messages produce image attachment refs with source ids and source message ids.
- Lark file messages produce file attachment refs with mime hints.
- Lark attachment download writes a local file and returns an `AttachmentRef.local_path`.
- a Lark inbound attachment is downloaded before transport submission through the existing Orchestrator path.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_attachments.py
6 passed

uv run --with pytest python -m pytest tests/test_channel_native_attachments.py tests/test_channel_native_lark.py tests/test_channel_native_persistence_reliability.py
19 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `AttachmentRef` now carries `source_message_id` for channel APIs that need source-message context.
- `LarkChannelAdapter` parses image/file message content into attachment refs and downloads resources through the injected `LarkBotApi`.
- The generic Orchestrator attachment intake path handles Lark without adding channel-specific branches.

## Slice 21: Session Listing and Archive Boundary

Status: implemented and locally verified.

Goal: complete the retained session management surface with a non-destructive list/archive contract.

Scope:

- `SessionSummary` projection for channel-native session lists.
- filtered `SessionRegistry.list_sessions(...)` by channel/account/chat/thread.
- explicit archive state on stopped sessions.
- owner/admin-only `Orchestrator.archive_session(...)`.
- command menu archive action for stopped sessions.
- persistence round-trip for archived metadata.

Non-goals:

- slash-command UI for choosing sessions.
- destructive archive that kills transports.
- cross-channel global search or pagination.

Acceptance:

- listing returns unarchived sessions in a chat/thread without exposing platform-specific fields.
- archiving a running session returns `session_running` and does not stop or mutate it.
- owner/admin can archive a stopped session; reviewer/collaborator cannot.
- archived sessions are hidden by default and visible with `include_archived=true`.
- archived metadata survives JSON state save/load.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_session_listing.py
5 passed

uv run --with pytest python -m pytest tests/test_channel_native_controls.py tests/test_channel_native_session_listing.py tests/test_channel_native_persistence_reliability.py
19 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `SessionSummary` gives channel-native list views a stable projection without exposing mutable session objects.
- Archive is owner/admin-only, non-destructive, and limited to stopped sessions.
- Archived sessions stay persisted and are hidden from default lists unless explicitly requested.

## Slice 22: High-risk Transport Controls

Status: implemented and verified.

Goal: move model switching, permission mode changes, and checkpoint rewind from capability flags into audited transport control boundaries.

Scope:

- `AgentTransport.set_model(...)` boundary.
- `AgentTransport.set_permission_mode(...)` boundary.
- `AgentTransport.rewind_checkpoint(...)` boundary.
- `Orchestrator` owner/admin authorization and stopped-session guard for these controls.
- fake transport call tracking.
- Claude headless client delegation when the injected client supports the method.
- Codex app-server remains capability-disabled for these controls.

Non-goals:

- real Claude checkpoint file-boundary E2E.
- model catalog discovery.
- UI picker design for model or permission mode values.

Acceptance:

- owner/admin can call supported high-risk controls on a running structured session.
- reviewer/collaborator cannot call high-risk controls.
- disabled transport capabilities return `capability_disabled` and do not call the transport method.
- stopped sessions return `session_stopped`.
- Claude headless delegates controls to injected client methods; Codex keeps them disabled.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_high_risk_controls.py
5 passed

uv run --with pytest python -m pytest tests/test_channel_native_controls.py tests/test_channel_native_high_risk_controls.py
10 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- High-risk controls share the same owner/admin, stopped-session, and capability gate as the lower-risk session controls.
- `FakeAgentTransport` records model, permission-mode, and checkpoint rewind calls for contract tests.
- `ClaudeHeadlessTransport` delegates to injected client methods when present and returns `capability_disabled` if a launched client lacks a method.
- `CodexAppServerTransport` keeps these capabilities disabled and does not advertise unsupported control behavior.

## Slice 23: Permission Callback Round-trip

Status: implemented and verified.

Goal: close the permission loop between transport events, channel interaction callbacks, authorization, and transport approval.

Scope:

- `AgentEventType.PERMISSION_REQUESTED` becomes a first-class event in `Orchestrator._drain_events`.
- Permission events register `InteractionStore` contexts with transport request id and high-risk metadata.
- permission callbacks are authorized through `AuthorizationStore.can_decide_permission(...)`.
- owner/admin can decide high-risk permissions; collaborator can decide low-risk permissions; reviewer cannot decide either.
- accepted permission decisions call `AgentTransport.approve_permission(...)` with the original transport request id.
- disabled transport permission capability returns `capability_disabled` before consuming a callback token.
- fake, Claude headless, and Codex app-server transports expose approval
  boundaries; Codex server-request support is covered further in Slice 44/45.

Non-goals:

- real Claude SDK permission E2E.
- Lark/Telegram visual redesign of permission cards.
- tool-risk classifier beyond the explicit `high_risk` event payload flag.

Acceptance:

- a transport permission event renders a prompt with short callback tokens.
- high-risk callback by collaborator is rejected and does not call the transport.
- low-risk callback by collaborator is accepted and calls the transport approval boundary once.
- disabled permission capability does not consume the token and does not call transport approval.
- Claude headless delegates permission approval to an injected client method.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_permission_roundtrip.py
5 passed

uv run --with pytest python -m pytest tests/test_channel_native_permission_roundtrip.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_callback_ack.py
29 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `permission.requested` transport events now register permission interactions and render neutral permission prompts.
- Permission callbacks are role-gated before token consumption.
- Permission callback capability is checked before token consumption, so unsupported transports do not strand a decision.
- Accepted permission decisions call `AgentTransport.approve_permission(...)` with the original transport request id.

## Slice 24: Transport-aware Health Watchdog

Status: implemented and verified.

Goal: preserve the old product ability of health/watchdog updates without keeping tmux pane probing or hook-driven liveness as a core mechanism.

Scope:

- session progress fields for last transport event time and type.
- lifecycle updates from structured transport events:
  - `turn.delta` keeps the session active.
  - `permission.requested` marks the session waiting for permission.
  - `turn.completed` marks the session idle and releases the active writer
    lease.
  - `session.error` marks the session recoverable-error.
- a channel-neutral health snapshot/check result generated by the Orchestrator.
- progress-timeout detection based on structured event progress only.
- persistence round-trip of progress fields.

Non-goals:

- tmux pane watchdog.
- automatic process killing or restart.
- real external service health checks.

Acceptance:

- transport events update session progress metadata and lifecycle state.
- completed turns become idle rather than remaining indefinitely active.
- idle sessions do not hold active writer leases.
- progress timeout returns a stale health result without interrupting or closing the transport.
- health check view remains platform-neutral.
- progress metadata survives `SessionRegistry` JSON round-trip.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_health_watchdog.py
4 passed

uv run --with pytest python -m pytest tests/test_channel_native_health_watchdog.py tests/test_channel_native_streaming_guardrails.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_views_auth_outbox.py
28 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- Sessions now persist `last_progress_at` and `last_progress_event`.
- Structured transport events update lifecycle state while the Orchestrator drains events.
- `check_session_health(...)` returns a neutral `SessionHealth` snapshot and does not kill, interrupt, or close a transport.

## Slice 25: AskUserQuestion Transport Round-trip

Status: implemented and verified.

Goal: close the AskUserQuestion loop so transport questions are rendered in IM and final answers are delivered back to the originating transport request.

Scope:

- `ask_user.requested` transport event type.
- `InteractionStore.register_ask_user_question(...)` stores a transport request id.
- callback answers and Other-text answers are role-gated like user input.
- `TransportCapabilities.ask_user_question` gates callback consumption and text-answer delivery.
- final answers call `AgentTransport.answer_user_question(...)`.
- intermediate multi-step or multi-select callbacks update state but do not call the transport until final answers are available.
- fake, Claude headless, and Codex app-server transports expose the answer
  boundary; Codex server-request support is covered further in Slice 44/45.

Non-goals:

- real Claude SDK AskUserQuestion E2E.
- redesigning question UI.
- free-form validation beyond the existing state machine.

Acceptance:

- a transport question event renders an AskUserQuestion prompt.
- single-select final answer calls the transport answer boundary once.
- multi-question flow only calls transport after the final question.
- Other text answer is consumed before ordinary agent input and delivered to transport.
- reviewer cannot answer; disabled capability does not consume the token.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_ask_user_roundtrip.py
6 passed

uv run --with pytest python -m pytest tests/test_channel_native_ask_user_roundtrip.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_callback_ack.py tests/test_channel_native_permission_roundtrip.py tests/test_channel_native_health_watchdog.py
30 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ask_user.requested` transport events now register AskUserQuestion interactions and render neutral prompts.
- AskUserQuestion callbacks and Other text are role-gated and capability-gated before state is consumed.
- Final answers call `AgentTransport.answer_user_question(...)`; intermediate state changes update the prompt without calling transport.

## Slice 26: Takeover Orchestrator Boundary

Status: implemented and verified.

Goal: complete the observed-session takeover contract at the Orchestrator boundary, not only inside `SessionRegistry`.

Scope:

- create a takeover prompt when IM input is blocked on an external-TUI observed session.
- keep Telegram/Lark UX channel-neutral by rendering a `takeover_prompt` view through the existing outbox path.
- authorize takeover through `AuthorizationStore.can_takeover(...)`.
- gate takeover execution on `TransportCapabilities.external_tui_takeover`.
- keep callback tokens unconsumed when authorization or capability checks fail.
- convert missing native resume refs to `manual_only` without changing writer ownership or submitting blocked input.
- complete a supported takeover, switch writer ownership to the structured transport, and submit the retained blocked input once.

Non-goals:

- killing a real external TUI process.
- implementing a process-control adapter.
- proving real Claude/Codex native resume E2E.
- pixel-perfect Telegram/Lark takeover cards.

Acceptance:

- observed-session input is blocked and produces a takeover prompt view.
- reviewer/collaborator cannot authorize takeover, and their callback does not consume the token.
- owner/admin with no structured resume ref gets `manual_only`; the external TUI remains the writer and the blocked input remains unsent.
- disabled `external_tui_takeover` capability returns `capability_disabled` without consuming the token.
- owner/admin with a recoverable resume ref completes takeover and submits the blocked input to the target structured transport exactly once.
- stale generation takeover callbacks are rejected without changing transaction state.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py
6 passed

uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_takeover.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_callback_ack.py tests/test_channel_native_controls.py
39 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- IM input on an external-TUI observed session now creates a blocked input, opens a takeover transaction, registers a takeover interaction, and renders a neutral `takeover_prompt`.
- Takeover callbacks are owner/admin gated and capability gated before the callback token is consumed.
- Missing structured resume data becomes `manual_only` and leaves the external TUI writer untouched.
- Supported takeover switches writer ownership to the structured transport and submits the retained blocked input exactly once.

## Slice 27: Structured Resume Boundary

Status: implemented and verified.

Goal: make takeover and future one-shot import use a real `AgentTransport.resume(...)` contract instead of treating a stored resume ref as an already-live handle.

Scope:

- add `ResumeSpec` and `AgentTransport.resume(...)`.
- update fake transport to record resume calls and return a resumed handle.
- update `CodexAppServerTransport` so the generic resume boundary calls `thread/resume`.
- update `ClaudeHeadlessTransport` to delegate resume to an injected client when available and fail explicitly otherwise.
- update takeover execution to call `resume(...)` before completing writer transfer and submitting the blocked input.

Non-goals:

- real Claude SDK resume E2E.
- Codex active-turn fan-out.
- importing old state into live sessions.

Acceptance:

- successful observed takeover calls `resume(...)` before `submit_turn(...)`.
- resume failure leaves the blocked input unsent and does not mark takeover completed.
- Codex generic resume calls `thread/resume` with the provided thread id.
- Claude injected client resume is delegated through the transport boundary.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_resume_boundary.py
4 passed

uv run --with pytest python -m pytest tests/test_channel_native_resume_boundary.py tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_takeover.py tests/test_channel_native_codex.py tests/test_channel_native_telegram_claude.py tests/test_channel_native_persistence_reliability.py
33 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ResumeSpec` and `AgentTransport.resume(...)` are now part of the channel-native transport contract.
- Observed takeover calls `resume(...)` before completing writer transfer and before submitting the blocked input.
- Resume failure marks the takeover failed, keeps the external TUI writer unchanged, and leaves the blocked input unsent.
- `CodexAppServerTransport.resume(...)` maps to `thread/resume`; `ClaudeHeadlessTransport.resume(...)` delegates to an injected client when available.

## Slice 28: Takeover Prompt and Progress Views

Status: implemented and verified.

Goal: align observed takeover execution with the channel UX design: one explicit
takeover button performs the writer switch, and every terminal state is visible.

Scope:

- add neutral `takeover_progress` and `manual_only` view builders.
- make `takeover_and_send` execute takeover directly.
- render progress before resume/submit and render failed/manual-only terminal views.
- keep Telegram/Lark adapters rendering those views through the same generic view path.

Non-goals:

- exact Telegram copywriting or Lark card layout polish.
- external TUI process-control semantics; covered separately by Slice 29.
- async background takeover worker.

Acceptance:

- first takeover click performs resume and submit.
- manual-only and resume-failed states produce neutral terminal views.
- callback ack still happens before long-running handling.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py
11 passed

uv run --with pytest python -m pytest tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py tests/test_channel_native_callback_ack.py tests/test_channel_native_views_auth_outbox.py tests/test_channel_native_persistence_reliability.py tests/test_channel_native_takeover.py
39 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `takeover_and_send` is now the confirmed action. It resumes the transport,
  transfers writer ownership, and submits the blocked input.
- `confirm_takeover` is retained only for compatibility with already-persisted
  legacy tokens. New Telegram/Lark UI does not generate it.
- The prompt no longer exposes `Keep read-only` or `Manual steps` buttons.
- Takeover progress is rendered for structured resume, blocked-input submission, and resume/submit failures.

## Slice 29: External TUI Termination Boundary

Status: implemented and verified.

Goal: close the safety gap between logical writer transfer and the user's TUI
process. This slice introduced the explicit process-control boundary. Slice 43
supersedes the execution order: takeover now validates structured resume before
terminating a verified live TUI writer.

Scope:

- add an `ExternalTuiController` contract and fake controller for tests.
- support `terminate_ref` in observed session external refs.
- render `takeover_progress(phase="terminating_external_tui")` before process termination.
- make takeover `manual_only` when `resume_ref` exists but no termination boundary exists.
- make termination failure terminal and non-destructive.

Non-goals:

- reusing old tmux/hook runtime code.
- silently killing arbitrary processes without explicit `terminate_ref`.
- real Telegram/Lark/OS process E2E without gate credentials and a controlled test process.

Acceptance:

- the accepted takeover action uses the external TUI controller only when a
  verified live TUI writer still owns the session.
- no `terminate_ref` or missing controller leaves the external TUI writer unchanged and renders `manual_only`.
- termination failure leaves the blocked input unsent, keeps writer ownership on `external_tui`, marks the transaction failed, and renders failed progress.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_takeover_process_control.py
3 passed

uv run --with pytest python -m pytest tests/test_channel_native_takeover_process_control.py tests/test_channel_native_takeover_orchestrator.py tests/test_channel_native_resume_boundary.py tests/test_channel_native_takeover.py
18 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
118 passed

uv run python -m compileall -q src/walkcode/channel_native
passed
```

Implementation notes:

- `ExternalTuiController` is an injectable process-control boundary; tests use `FakeExternalTuiController`.
- Automatic observed takeover of a live TUI writer requires both `resume_ref`
  and `terminate_ref`.
- Slice 43 changes the order to resume, terminate, then submit.
- Missing termination capability becomes `manual_only`; termination failure marks the transaction failed and leaves writer ownership and blocked input unchanged.
- Legacy state is not imported. Native takeover depends only on explicit
  `resume_ref` and `terminate_ref` metadata.

## Slice 30: Channel-native V3 Runtime and CLI

Status: implemented and locally verified.

Goal: make the V3 core locally deployable without relying on the legacy
`walkcode serve/start/hook` runtime.

Scope:

- add a V3 runtime that assembles `ChannelNativeConfig`, stores, channel
  adapters, transports, and `Orchestrator`.
- add Telegram long-polling ingress for the first local experience.
- add `walkcode native doctor` and `walkcode native serve`.
- support `walkcode native serve --once` for smoke tests and E2E-gate harnesses.
- persist channel-native state through `JsonFileStateStore`.

Non-goals:

- keeping legacy `walkcode serve/start/hook` as the default local deploy path.
- reusing the old Lark/tmux/hook runtime to pretend V3 is locally deployed.
- claiming real Telegram/Lark/Claude/Codex E2E success without credentials and explicit gates.
- wiring V3 live Lark ingress in this slice.

Acceptance:

- `native doctor` reports the selected channel, bound agent, state path, cwd, and that agent's availability.
- a Telegram update can be polled, parsed, submitted to the configured agent transport, answered back through the channel, and persisted.
- repeated polling uses Telegram update offsets.
- `native serve --once` performs one poll cycle and exits.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_runtime.py tests/test_channel_native_config.py tests/test_channel_native_cli.py
27 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
150 passed

uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
passed

uv build
Successfully built dist/walkcode-0.10.54.tar.gz
Successfully built dist/walkcode-0.10.54-py3-none-any.whl

env WALKCODE_ENV_FILE=/tmp/walkcode-native-v3.env \
  WALKCODE_CHANNEL=telegram \
  TELEGRAM_BOT_TOKEN=123456:fake-token \
  WALKCODE_AGENT=claude \
  WALKCODE_CWD=/tmp \
  WALKCODE_STATE_PATH=/tmp/walkcode-native-state.json \
  uv run --no-project --no-cache --with ./dist/walkcode-0.10.54-py3-none-any.whl \
  walkcode native doctor --json
passed
```

Implementation notes:

- ADR: `docs/adr/0024-channel-native-v3-runtime.md`.
- The command namespace is `native`; top-level install/upgrade docs now point to
  this V3 path.
- `docs/channel-native-local-deploy.md` documents the deployment path.
- `walkcode native doctor --json` was smoke-tested with a fake Telegram token
  and correctly reports a single selected channel, product-level agent status,
  unavailable Claude headless capability, and closed E2E gates.

## Slice 31: E2E Gate Status in Doctor

Status: implemented and locally verified.

Goal: make release and local deployment readiness visible from the V3 CLI without running real external systems by accident.

Scope:

- add E2E gate status to `ChannelNativeRuntime.describe()`.
- include the same status in `walkcode native doctor` and `walkcode native doctor --json`.
- report only gate names, enabled booleans, missing env var names, and reasons.

Non-goals:

- running real Telegram/Lark/Claude/Codex E2E tests automatically.
- printing credential values.
- changing the gate requirements.

Acceptance:

- JSON doctor output shows each E2E gate and never includes secret values.
- text doctor output includes compact gate status.
- external E2E remains opt-in through existing `WALKCODE_E2E_*` variables.

Design:

- ADR: `docs/adr/0025-channel-native-e2e-gate-status.md`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_runtime.py tests/test_channel_native_cli.py
10 passed
```

## Slice 32: Real Claude Agent SDK Adapter

Status: implemented and locally verified; real Telegram 1:1 smoke is blocked on a valid user inbound chat id.

Goal: make the Telegram 1:1 V3 path run against the installed `claude_agent_sdk` package instead of only fake clients.

Scope:

- construct default Claude SDK clients through `ClaudeAgentOptions(cwd=...)`.
- call `connect()` during launch when present.
- submit text through `query()` when a fake-client `submit()` method is not present.
- drain `receive_response()` when a fake-client `events()` method is not present.
- convert SDK assistant/result/error messages to neutral `AgentEvent`s.
- keep explicit test env maps isolated from the default
  `~/.walkcode/telegram-claude.env`.
- restrict Telegram V3 E2E to the configured test chat by default.
- confirm Telegram long-poll offsets in `serve --once` and size HTTP timeouts above the Telegram long-poll timeout.

Non-goals:

- broad permission callback parity with all Claude SDK hooks.
- Lark live ingress.
- Codex app-server live wiring; handled separately in Slice 33.

Acceptance:

- focused contract tests cover launch, query submit, and SDK message conversion.
- `walkcode native doctor` reports `agent=claude` and `agent_status.available=true` when run with `claude-agent-sdk`.
- V3 install and upgrade paths inject `claude-agent-sdk` into the installed uv
  tool environment, so the normal `walkcode` CLI supports Claude headless after
  installation.
- real Telegram 1:1 smoke can attempt a safe prompt after SDK/auth is available.
- Telegram E2E does not consume messages outside the configured test chat.
- `serve --once` does not leave processed Telegram updates unconfirmed for the next process.

Design:

- ADR: `docs/adr/0026-claude-agent-sdk-runtime-adapter.md`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_telegram_claude.py -k real_sdk_shape
1 passed, 7 deselected

uv run --with pytest python -m pytest tests/test_channel_native_telegram_claude.py tests/test_channel_native_runtime.py tests/test_channel_native_config.py
26 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
150 passed

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python -m walkcode native doctor --json
channel.kind=telegram, agent=claude, agent_status.available=true
```

Live preflight:

- `getUpdates` returned no pending user inbound update.
- `sendMessage` to the configured E2E chat id returned Telegram `Bad Request: chat not found`.
- Real 1:1 turn execution therefore requires the user to first message the bot and provide or override the resulting real chat id.

## Slice 33: Real Codex App-server Stdio Client

Status: implemented and locally verified; real Codex turn E2E remains behind the explicit E2E gate.

Goal: make Codex a real channel-native agent adapter in the V3 runtime, without exposing app-server transport names in `.env`.

Scope:

- add a lazy `CodexStdioAppServerClient` that starts `codex app-server --stdio`.
- initialize the JSON-RPC session with WalkCode client metadata.
- wire `thread/start`, `thread/resume`, and `turn/start` through `CodexAppServerTransport`.
- support actual app-server response shapes with nested `thread.id`.
- convert JSON-RPC notifications such as `item/agentMessage/delta` and `turn/completed`.
- build the Codex transport automatically when the local `codex` CLI exists.

Non-goals:

- claiming full Codex permission callback parity.
- enabling AskUserQuestion, interrupt, model switching, permission-mode switching, or checkpoint rewind before protocol-specific verification.
- running a real model turn without `WALKCODE_E2E_CODEX_APP_SERVER=1`.

Acceptance:

- `native doctor` can report `agent=codex` and `agent_status.available=true` when the `codex` CLI is installed.
- users do not configure `WALKCODE_TRANSPORTS` or `WALKCODE_DEFAULT_TRANSPORT`.
- local protocol smoke can initialize the app-server and create an ephemeral thread without starting a model turn.
- the Codex live E2E gate requires only `WALKCODE_E2E_CODEX_APP_SERVER=1`
  plus `WALKCODE_E2E_CWD`; users do not configure an app-server URL.

Design:

- ADR: `docs/adr/0028-codex-app-server-stdio-client.md`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_codex.py tests/test_channel_native_runtime.py tests/test_channel_native_config.py tests/test_channel_native_cli.py
26 passed

Remove old pre-V3 variables from the runtime env file before running:
  WALKCODE_CHANNELS
  WALKCODE_PRIMARY_CHANNEL
  WALKCODE_TRANSPORTS
  WALKCODE_DEFAULT_TRANSPORT
  WALKCODE_DEFAULT_AGENT

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python -m walkcode native doctor --json
agent_status.available=true for the configured bot-bound agent

uv run --with claude-agent-sdk python - <<'PY'
...
{'thread_id_present': True, 'cwd_present': True}
PY
```

## Slice 34: Module-level Debug Gates

Status: implemented and locally verified.

Goal: make local E2E debugging repeatable by testing one module at a time before
consuming Telegram updates or starting an agent turn.

Scope:

- add non-IM diagnostics for config, runtime process conflicts, state
  persistence, durable outbox, and agent adapter smoke checks.
- add a read-only Telegram ingress diagnostic that checks bot identity,
  webhook state, pending update shape, allowlist match, and known session match.
- expose the diagnostic through `walkcode native debug telegram`.
- add `scripts/channel_native_debug.py` with module commands:
  `config`, `runtime`, `state`, `outbox`, `agent`, `agent-smoke`, `telegram`,
  and `tests`.
- ensure diagnostics do not print secrets and do not confirm Telegram offsets.

Non-goals:

- starting Claude/Codex turns unless `agent-smoke --live` is explicitly used.
- confirming Telegram offsets.
- replacing the full E2E gate; these diagnostics run before it.

Acceptance:

- config gate validates the single-channel config surface.
- runtime gate reports competing `walkcode serve` / `walkcode native serve`
  consumers before any Telegram update can be consumed.
- runtime gate treats launchd-managed per-agent native services
  (`com.walkcode.telegram-claude` and `com.walkcode.telegram-codex`) as valid
  runtime owners, not competing consumers. Unmanaged/manual native consumers
  remain unsafe unless a higher-level Telegram diagnostic explicitly allows a
  running native owner and relies on Telegram 409/pending-update checks.
- state gate can load existing state and prove atomic write/read with a
  temporary sibling file, without creating or rewriting the configured state.
- ~~state gate fails when active or waiting sessions have expired writer
  leases.~~ Withdrawn by ADR 0059: the count is informational and does not
  fail the gate.
- state repair can stop dead read-only external TUI observations after creating
  a state backup, so hook smoke leftovers do not block private-chat routing.
- outbox gate reports pending/sent/dead counts and verifies sent/permanent/
  transient dispatch behavior with synthetic channels only.
- polling runtimes flush persisted outbox entries before and after each
  polling iteration, so messages queued before a restart do not wait for a new
  inbound Telegram update.
- outbound transcript delivery has one runtime-owned dispatcher. Orchestrator
  event drains and runtime maintenance both call that dispatcher; they do not
  construct independent flushers. Each ready delivery is claimed with an owner
  lease before sending and then acknowledged through the same state callback.
- deferred TUI hook draining prioritizes hooks from the recent window before
  older backlog, so current TUI observation is not blocked behind historical
  queued hook files after a broken or interrupted service run. The runtime
  drains a bounded batch each polling iteration to catch up without moving TUI
  maintenance ahead of Telegram ingress.
- agent gate reports product-level Claude/Codex availability without starting a turn.
- agent-smoke dry run does not launch a transport; live mode is explicit and
  fails on `session.error`.
- Claude headless can receive a product-level settings profile through
  `WALKCODE_CLAUDE_SETTINGS`, so V3 does not depend on legacy `WALKCODE_EXTRA_ARGS`.
- Telegram gate returns `safe_to_run_serve_once=false` if pending updates would be
  rejected by the allowlist.
- Telegram gate returns `safe_to_run_serve_once=false` if competing local
  consumer processes are present.
- Telegram gate returns `safe_to_run_serve_once=false` if a pending update targets
  an existing session that would reject submit. (An expired writer lease is no
  longer such a reason — ADR 0059.)
- `poll_telegram_once` does not confirm the Telegram offset or complete inbound
  ledger for repairable submit failures. (`lease_expired` is no longer
  produced — ADR 0059.)
- focused tests prove no confirming offset is sent during Telegram diagnostics.

Design:

- ADR: `docs/adr/0029-channel-native-module-debug-gates.md`.

## Slice 30: Idle Structured Session Reacquire

Status: implemented and verified.

Goal: let Telegram follow-up messages continue a completed or recoverable-error
structured session across separate `serve --once` processes without weakening
single-writer safety.

Scope:

- `turn.completed` releases the active writer lease.
- Claude headless persists the SDK result `session_id` as
  `transport_ref.agent_session_id`.
- idle and `ERROR_RECOVERABLE` structured sessions reacquire a writer by
  calling `AgentTransport.resume(...)` before submitting the next IM input.
- diagnostics distinguish reusable idle sessions from stale active writers.

Non-goals:

- extending writer lease TTL as a substitute for durable resume.
- resuming active turns.
- changing external TUI takeover rules.

Acceptance:

- a completed idle session can receive a follow-up message after the old lease
  TTL has elapsed.
- an `ERROR_RECOVERABLE` session can receive a follow-up message after provider
  `session.error` when a durable resume reference exists.
- the follow-up path resumes the transport, writes a fresh handle reference, and
  creates a new writer lease before submit.
- a missing durable resume reference keeps Telegram `safe_to_run_serve_once`
  false and does not confirm the update offset.
- active/waiting sessions with expired leases still fail state and Telegram
  gates.

Design:

- ADR: `docs/adr/0030-idle-structured-session-reacquire.md`.

## Slice 31: Structured Session External TUI Handoff

Status: implemented and locally verified.

Goal: if a user resumes an IM-started headless session in a local TUI, make the
IM thread read-only instead of allowing two writers.

Scope:

- add an explicit external writer claim transition for structured sessions.
- preserve structured resume data for future IM takeover.
- preserve TUI termination data when available.
- invalidate stale IM callbacks by incrementing generation.
- reuse existing blocked-input and takeover UX when IM users send input after
  the TUI claim.

Non-goals:

- detecting raw TUI resumes that bypass WalkCode and do not emit hooks.
- injecting IM input into a live TUI.
- changing the observed takeover prompt flow.

Acceptance:

- a structured session can be marked externally owned by a WalkCode-aware TUI
  claim.
- after the claim, IM input is blocked and no transport submit occurs.
- stale claim generations are rejected.
- `walkcode native hook` can claim an existing structured session by Claude
  `agent_session_id` / `session_id` or Codex `thread_id`.

Design:

- ADR: `docs/adr/0031-structured-session-external-tui-handoff.md`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_runtime.py tests/test_channel_native_cli.py tests/test_channel_native_debug_script.py
45 passed
```

## Slice 35: Native TUI Hook Observation and Process Takeover

Status: implemented and locally verified.

Goal: observe TUI-owned Claude/Codex sessions in Telegram and let Telegram take
over only after an authorized local TUI process is terminated.

Scope:

- add `walkcode native hook <hook_type> --agent claude|codex` as the
  channel-native hook ingress.
- accept raw TUI hook names such as `Stop`, `UserPromptSubmit`, and
  `PostToolUse`, then normalize them inside the runtime.
- map Claude hooks to durable `agent_session_id` / `session_id` resume refs.
- map Codex hooks to durable `thread_id` resume refs.
- claim an existing IM-started structured session only from `sync` or
  `session-start` when the hook reports a matching durable resume id.
- create a Telegram observed session from `sync`, `session-start`,
  `UserPromptSubmit`, or first `PreToolUse` when the hook reports a TUI session
  that has no existing binding.
- mirror `UserPromptSubmit` prompt text as a read-only `TUI input` transcript
  message in the observed topic; this is not submitted back to the agent.
- claim an existing IM-started structured session only from `sync` or
  `session-start`, so fallback creation hooks do not steal IM-owned sessions.
- treat `MessageDisplay`, `stop`, `notification`, and `tui-output` with no
  existing observed session as accepted no-ops, so late output hooks do not
  create stale Telegram topics or claim IM-started sessions.
- preserve `terminate_ref` and use `LocalProcessController` only when
  `allow_terminate=true`.
- filter internal Codex status events and raw hook handler traces from Telegram.
- parse Claude `MessageDisplay` content blocks and Codex `event_msg` completion
  shapes as product events instead of leaking raw dictionaries or leaving turns
  active.
- clarify that Telegram placement is capability-driven: native
  topic-per-session is preferred where available, root reply-chain is the
  fallback, and group-per-session is not the default runtime model.
- real TUI hook configs may use `--defer` so the hook writes a local event and
  exits immediately while `walkcode native serve` performs Telegram side
  effects.
- observed TUI tool hooks update the same compact Telegram tool progress UI as
  structured transport tool events.

Non-goals:

- writing IM input into a live TUI.
- killing unrecognized or unauthorized processes.
- making Telegram groups per session.
- replacing legacy `walkcode hook` in this slice.

Acceptance:

- hook-created observed Telegram sessions send a root observation message on
  `sync`/`session-start`; subsequent user-visible TUI output is sent only after
  that observation exists.
- hook claim of existing Claude/Codex structured sessions increments generation,
  clears the writer lease, and blocks later IM input behind takeover.
- duplicate hook events are deduped by the inbound ledger without making the
  TUI hook command fail.
- internal `[thread/status/changed]` and hook handler debug output are not sent
  to Telegram.
- raw hook names, missing resume refs, stopped sessions, and non-session
  observation hooks do not make the TUI hook command fail.
- accepted hooks without `--json` write no stdout, because Claude Code Stop
  hooks parse non-empty stdout as hook decision JSON.
- Claude Code `Stop` is treated as turn completion, not process exit. It may
  forward final visible text but must not mark the observed session stopped or
  drop the TUI termination reference.
- deferred hooks are persisted locally, drained by the service, and do not block
  the TUI on Telegram network calls.
- deferred hook spool filenames use nanosecond timestamps so same-turn
  `PreToolUse`/`PostToolUse` files are drained in local creation order.
- `PreToolUse`, `PostToolUse`, failure, and permission hooks update one tool
  progress message for an existing observed session without rendering full
  tool output.
- TUI tool and permission observations keep the session in
  `EXTERNAL_OBSERVED_READONLY`; activity visibility must not imply that
  Telegram owns the writer before takeover.
- TUI user input is visible in Telegram as transcript context without changing
  the writer or causing a duplicate agent turn.
- Telegram takeover from a TUI-owned session validates structured resume,
  terminates an authorized live process, then submits the blocked input.
- late Stop hooks without an existing observed session do not create Telegram
  topics.
- late Stop hooks that only match an IM-started structured session do not claim
  or stop that session.
- claim-capable hooks may restore older external-TUI state that was
  incorrectly marked stopped, so takeover can still terminate the live TUI
  instead of creating a split writer.

Design:

- ADR: `docs/adr/0032-native-tui-hook-observation.md`.
- ADR: `docs/adr/0039-deferred-tui-hook-processing.md`.

Verification:

```text
uv run --with pytest python -m pytest tests/test_channel_native_cli.py tests/test_channel_native_runtime.py
51 passed

uv run --with pytest python -m pytest tests/test_channel_native_cli.py tests/test_channel_native_runtime.py tests/test_channel_native_takeover_process_control.py tests/test_channel_native_codex.py
63 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py
205 passed
```

## Slice 36: Telegram Session Placement and Bot Model

Status: partially implemented and locally verified.

Goal: replace the unclear "reply chain is the Telegram UX" framing with an
explicit placement strategy, and separate bot identity from Claude/Codex agent
selection.

Research findings:

- Telegram supports forum topics in supergroups and can address messages to a
  topic with `message_thread_id`.
- Telegram private bot chats can also support topics when the bot has private
  topic mode enabled.
- The current local Telegram target is a private chat, and the configured bot
  reports private topic mode disabled. That environment cannot show one native
  Telegram topic per session until the bot/chat setup changes.
- Lark/Feishu remains the clearest topic-native IM target; it should use one
  topic per session when live V3 Lark ingress is enabled.

Decision:

- Preferred placement is native topic-per-session:
  - Lark/Feishu topic-capable chats.
  - Telegram forum supergroup with the bot as an admin that can manage topics.
  - Telegram private chat with bot topic mode enabled.
- Fallback placement is root reply-chain:
  - one root message anchors the session.
  - replies to that root route to the session.
  - rootless input can continue a single active session in the same chat/thread.
  - rootless input with multiple active sessions must show an explicit chooser.
- Group-per-session is not a default runtime model.
- One V3 runtime instance selects one IM channel and one Coding Agent. One
  Telegram bot token or Lark app identity belongs to that agent.
- Claude Code and Codex run as separate WalkCode instances with separate env
  files, state paths, and bot/app identities.
- `/claude <task>` and `/codex <task>` are not routing commands inside a shared
  bot. They are rejected with guidance instead of launching another agent.

Local migration notes:

- Existing `claude` / `codex` tmux wrappers are not part of the V3 runtime.
  Keep them only as private local TUI shortcuts outside WalkCode's deploy path.
- IM-started V3 sessions do not depend on shell wrappers; they use the headless
  agent adapters directly.
- Old `walkcode serve/start` processes that use the same bot/webhook must be
  stopped before `walkcode native serve`.
- Comments, README sections, installer scripts, and upgrade scripts that
  describe Feishu-only or tmux/hook routing must point to V3 native commands or
  to legacy cleanup guidance.

Design:

- ADR: `docs/adr/0033-telegram-session-placement-and-bot-model.md`.

Implemented in this slice:

- Telegram debug gate reports bot private-topic flags and a sanitized target
  chat placement recommendation without exposing token or chat id.
- Telegram debug gate reports sanitized forum-supergroup bot administrator
  status, including whether the bot can manage topics. A forum group without
  `can_manage_topics` is reported as root reply-chain fallback.
- Rootless Telegram input with multiple active sessions now renders a
  `session_chooser` view instead of failing silently.
- Telegram debug treats ambiguous rootless input as safe to consume because it
  will render the chooser instead of starting an agent turn.
- Runtime config wires only the configured `WALKCODE_AGENT` transport for the
  current bot.
- Telegram root messages in a forum supergroup create a forum topic before the
  new session starts, then persist that `message_thread_id` as
  `ChannelBinding.thread_id`.
- Telegram forum topic creation follows the official Bot API contract:
  `createForumTopic` in a forum supergroup requires the bot to be an
  administrator with `can_manage_topics`, and the returned
  `ForumTopic.message_thread_id` is the channel thread id used for later
  `sendMessage` calls.
- Telegram empty system messages, such as group-to-supergroup migration updates,
  are confirmed without creating an agent session or a topic.
- Telegram TUI hook observation also creates a forum topic for the observed
  session when a `sync`/`session-start` hook arrives, the configured TUI chat is
  a topic-enabled supergroup, and the bot can manage topics.
- TUI `stop` hooks persist the final output and then mark an existing observed
  session stopped, so completed TUI sessions do not keep blocking root input in
  reply-chain fallback chats.
- TUI `stop`, `notification`, and `tui-output` hooks that do not map to an
  existing observed session are accepted as no-op observations. They do not
  create new topics and do not claim an IM-started structured session.
- Late TUI hooks whose durable resume id already maps to a stopped session are
  accepted as no-op observations. They do not hand off, revive, or write to the
  stopped session, and the hook process exits successfully.
- Accepted TUI hook commands are stdout-silent unless `--json` is requested, so
  Claude Code Stop hooks do not fail on non-decision WalkCode output.
- Old `/claude` and `/codex` agent-selector commands are rejected; they do not
  switch agents inside a shared bot.
- Accepted TUI hooks persist completed dedupe ledger state after saving hook
  output, so restarts do not leave accepted hook events in `in_progress`.
- The local Codex private-chat environment currently reports
  `has_private_topics_enabled=false`; it therefore uses root reply-chain
  fallback until the bot is moved to a forum supergroup or private topic mode is
  enabled.

Remaining implementation plan:

1. Extend Telegram capability diagnostics to include sanitized supergroup
   admin/topic-management status.
2. Add live Lark ingress wiring and map each Lark session to one topic/thread.
3. Add clickable session-list controls for reply-chain fallback environments.
4. Run real E2E gates for Telegram forum topic creation, Claude headless, and
   Codex app-server before marking V3 deployable.

Verification target:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_config.py \
  tests/test_channel_native_routing.py \
  tests/test_channel_native_telegram_claude.py \
  tests/test_channel_native_runtime.py

51 passed

uv run --with pytest python -m pytest tests/test_channel_native_*.py

183 passed
```

## Slice 36: V3 Release Surface Hard Cut

Status: implemented and locally verified.

Goal: make the release and local deploy surface match the clean-slate V3
architecture instead of leaving legacy runtime paths as working defaults.

Scope:

- hide and reject top-level legacy CLI commands:
  `serve/start/stop/restart/status/hook/install-hooks/clean-images/test-inject`;
- make `walkcode install-hooks`, `_install_claude_hooks`, and
  `_install_codex_hooks` refuse instead of writing `walkcode hook` config;
- keep `walkcode upgrade` V3-only: package upgrade, explicit
  `WALKCODE_V3_LAUNCHD_LABELS` restart, then `walkcode native doctor`;
- make install/upgrade block on legacy LaunchAgent, old hook, shell wrapper,
  and `FEISHU_*` remnants;
- require explicit `WALKCODE_CHANNEL=telegram|lark`;
- derive the default state path from `{channel}-{agent}` when
  `WALKCODE_STATE_PATH` is omitted;
- make the default generated env file `~/.walkcode/telegram-claude.env`;
- require explicit `allow_terminate=true` before IM takeover may kill a local
  TUI process.

Non-goals:

- providing a legacy compatibility mode;
- auto-cleaning user shell files or hook configs;
- claiming V3 live Lark ingress before its own E2E gate passes.

Acceptance:

- old CLI commands exit non-zero and are absent from help;
- release scripts no longer install hooks, tmux wrappers, or old daemons;
- `install.sh`, `upgrade.sh`, and `walkcode upgrade` install the CLI with
  `--with claude-agent-sdk`;
- pure agent-control-plane V3 pass-through helpers are allowed, while wrappers
  containing tmux, old `walkcode hook/serve/start/status/test-inject`, removed
  WalkCode env vars, legacy Codex env paths, or `FEISHU_*` are blocked;
- happy-path upgrade works only from a clean V3 environment;
- legacy remnants fail the runtime/debug/release gate;
- tests prove that inferred hook parent processes are not authorized for kill.

Design:

- ADR: `docs/adr/0024-channel-native-v3-runtime.md`.
- ADR: `docs/adr/0027-single-channel-agent-config.md`.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_*.py \
  tests/test_release_scripts.py \
  tests/test_upgrade.py \
  tests/test_codex_hooks_feature.py

226 passed

bash -n install.sh && bash -n upgrade.sh
passed

uv run python -m compileall -q \
  src/walkcode/channel_native \
  src/walkcode/channel_native_runtime.py \
  src/walkcode/__main__.py \
  scripts/channel_native_debug.py
passed

env -i HOME=<tmp> TMPDIR=<tmp> PATH="$PATH" LANG="$LANG" ./upgrade.sh --dry-run
passed

uv run --with pytest python -m pytest
665 passed, 4 warnings

uv build
Successfully built dist/walkcode-0.10.54.tar.gz
Successfully built dist/walkcode-0.10.54-py3-none-any.whl
```

## Slice 37: Telegram Topic Status Card and Read-only Takeover UX

Status: superseded in part by Slice 42. The original status-card placement
remains, but live edits, close/reopen topic hints, and blocked-input deletion are
no longer the default Telegram UX.

Goal: make each Telegram topic read like one WalkCode agent session, with a
stable root/status card and a clear read-only takeover path for TUI-observed
sessions.

Scope:

- treat Telegram General/root chat messages as a start inbox when native forum
  topics are available;
- create the session topic before the agent session starts;
- keep the original General launch message and reply with a short
  session-topic navigation notice after the topic is created;
- submit the original General launch text to the agent before status-card
  creation or pinning, so slow Telegram UI calls cannot expire the writer lease
  and leave the new topic idle;
- treat an agent event stream that ends without `turn.completed` as
  recoverable: release the writer lease and mark the session
  `ERROR_RECOVERABLE`, so the next user input can resume instead of being
  blocked by a stale `ACTIVE` lease;
- create one status card per topic-backed session and store its id in
  `ChannelBinding.health_message_id`;
- originally edited the status card on progress; Slice 42 changes Telegram
  topic cards to static informational anchors;
- keep product session identity as the Claude Code/Codex session, and route
  native Telegram topics by `message_thread_id` rather than status-card root
  message id;
- for TUI-observed read-only topics, show a `Take over` button on the status
  card; Slice 42 removes the close-topic hint;
- backfill empty status/read-only capabilities for older persisted
  TUI-observed topic bindings on hook update and Telegram polling startup;
- if Telegram users send text into a read-only topic, block it in WalkCode and
  open a takeover prompt bound to that exact input.

Non-goals:

- using chat-wide `setChatPermissions` or user restrictions to emulate a
  topic-only input lock;
- importing old Feishu/Lark card code;
- changing Lark's peer-channel target. Lark should map the same status-card
  contract to its own topic/thread card update API in a later live ingress
  slice.

Design:

- ADR: `docs/adr/0034-telegram-session-status-card-and-readonly-topic.md`.
- `ChannelBinding.health_message_id` is the editable status card pointer.
- `root_message_id` remains a reply-chain fallback field. In native topic mode,
  exact root matches still work, but a reply to any message in the topic falls
  back to the unique active session with the same
  `channel_kind/account_id/chat_id/thread_id`.
- Status card content follows the old root card's information structure:
  title, status, agent/transport, durable session id, lifecycle state, writer,
  duration, last progress event, event sequence, cwd, and read-only reason.
- Telegram uses `sendMessage` for first card creation and `pinChatMessage`
  best-effort. Slice 42 removes default card refreshes and topic close/reopen
  calls.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_routing.py \
  tests/test_channel_native_core.py \
  tests/test_channel_native_takeover_orchestrator.py \
  tests/test_channel_native_runtime.py

68 passed
```

## Slice 38: Telegram Agent Markdown Rendering

Status: implemented and module-verified.

Goal: make Claude Code and Codex Markdown-style answers render cleanly in
Telegram instead of exposing raw `##`, `**bold**`, tables, and code fences.

Scope:

- detect Markdown-like agent output for `turn_delta`, `turn_completed`, and
  explicit text agent views;
- default to conservative Telegram HTML conversion via
  `sendMessage(parse_mode=HTML)` for stable-client compatibility;
- keep `sendRichMessage` / `editMessageText.rich_message` behind explicit
  `WALKCODE_TELEGRAM_RICH_MESSAGES=1` opt-in;
- fall back to original plain text if HTML parsing fails;
- keep status cards and control cards as ordinary text unless they are an
  agent-output view.

Design:

- ADR: `docs/adr/0035-telegram-agent-markdown-rendering.md`.
- The Telegram adapter owns Markdown rendering. Core view models continue to
  carry plain semantic data.
- The HTML fallback supports headings, bold, underline, strike, inline code,
  fenced code blocks, table rows as preformatted blocks, and HTTP/HTTPS links.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_telegram_claude.py \
  tests/test_channel_native_config.py \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_core.py \
  tests/test_channel_native_views_auth_outbox.py

79 passed
```

## Slice 39: Telegram Polling Transient Retry

Status: implemented and module-verified.

Goal: keep local Telegram LaunchAgent services alive through temporary
`getUpdates` network/API failures and avoid losing Telegram output during
flood-limit windows.

Scope:

- make `serve_telegram_polling` catch non-configuration polling exceptions;
- record the latest transient failure in `last_telegram_poll_error`;
- log the transient error to stderr;
- sleep for a small backoff and continue polling;
- keep `poll_telegram_once` strict so one-shot debug commands still fail
  visibly;
- add bounded-loop test hooks (`retry_delay`, `max_iterations`) for deterministic
  module tests;
- treat Telegram HTTP 429/5xx delivery failures as transient;
- honor Telegram `parameters.retry_after` in durable outbox scheduling;
- avoid Markdown fallback retries on transient delivery failures so one failed
  HTML send does not immediately duplicate the message as plain text.

Design:

- ADR: `docs/adr/0036-telegram-polling-transient-retry.md`.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_telegram_claude.py

54 passed
```

## Slice 40: Headless Hook Self-observation Guard

Status: implemented and module-verified.

Goal: prevent WalkCode-owned headless Claude/Codex processes from being
misclassified as external TUI sessions when user-level agent hooks are present.

Scope:

- inspect the hook payload's explicit or inferred process reference;
- walk the short parent process chain for the hook origin;
- ignore Claude hooks from `claude_agent_sdk/_bundled/claude` or Claude
  stream-json headless commands;
- ignore Codex hooks from `codex app-server --stdio`;
- keep real external TUI hooks eligible for observed-read-only handoff and
  takeover;
- return accepted success for ignored self-observation hooks without changing
  writer ownership, session generation, or channel binding.

Design:

- ADR: `docs/adr/0037-headless-hook-self-observation-guard.md`.
- Durable agent session ids remain the product session identity. Telegram
  `message_thread_id` remains only the channel binding.
- Hook events are allowed to claim a structured session only when the process
  evidence points to a real external TUI, not a WalkCode-owned headless child.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_telegram_claude.py \
  tests/test_channel_native_persistence_reliability.py
```

## Slice 41: Telegram Native Command and Progress UX

Status: implemented and module-verified.

Goal: make Telegram feel like a native bot UI for commands, processing
acknowledgement, topic scanning, and tool activity.

Scope:

- install Telegram bot commands with `setMyCommands` on polling service start;
- intercept WalkCode-owned commands before agent submission:
  `/status`, `/sessions`, `/model`, `/skills`;
- make `/model` render local model inventory from Claude settings or Codex
  config/cache before attempting any transport model switch;
- keep `/claude` and `/codex` rejected as old selector commands in the
  one-agent-per-bot model;
- send `sendChatAction(typing)` after accepted input is placed into its target
  chat/topic and before agent submission;
- best-effort add a `✅` reaction to received user text as the WalkCode-owned
  receipt marker;
- randomize Telegram forum topic icons using `icon_custom_emoji_id` from
  `getForumTopicIconStickers`, falling back to random allowed `icon_color`;
- add neutral tool lifecycle events: `tool.started`, `tool.completed`,
  `tool.failed`;
- convert Claude SDK tool-use/tool-result blocks, direct SDK tool blocks, Codex
  app-server tool-like events, and Codex command-execution item events into
  those neutral events;
- convert observed TUI tool hooks into those neutral events;
- render one editable tool progress message per session instead of sending full
  tool output into the topic.

Non-goals:

- claiming Telegram double-check read receipts can be controlled by Bot API;
- forwarding every agent-native slash command blindly to Claude/Codex;
- showing complete tool stdout/stderr in Telegram progress cards;
- Lark live rendering of the same command/tool progress contract.

Design:

- ADR: `docs/adr/0038-telegram-native-command-and-progress-ux.md`.
- Telegram commands are bot/runtime controls first. Unknown slash commands are
  not automatically treated as WalkCode controls.
- Unknown slash commands are passed to the agent only when the message resolves
  to an existing session topic/reply chain. Unknown slash commands in
  General/root chat are rejected so they do not create accidental sessions.
- Model inventory is local and labeled as local: Claude reads
  `WALKCODE_CLAUDE_SETTINGS`; Codex reads `WALKCODE_CODEX_CONFIG` /
  `WALKCODE_CODEX_MODELS_CACHE` or the default `~/.codex` files.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_telegram_claude.py \
  tests/test_channel_native_codex.py
```

## Slice 42: Telegram Topic Identity and TUI Takeover UX

Status: implemented and module-verified.

Goal: align Telegram forum topics with the V3 product model: one agent session
per topic, Telegram-origin topics are writable, and TUI-origin topics are
observed until explicit takeover.

Scope:

- define Telegram topic identity as `chat_id + message_thread_id`; keep
  `root_message_id` only as the reply-chain fallback outside native topics;
- stop using Telegram `closeForumTopic` / `reopenForumTopic` as the default
  readonly indicator;
- mark Telegram topic status cards as `static_status_card`, so the first card is
  an informational anchor and is not continuously edited;
- preserve user messages in readonly TUI topics and render a takeover prompt
  bound to the exact blocked input;
- render a separate takeover completion message when takeover is started from
  the status card and no blocked user input should be submitted;
- require TUI process termination before takeover whenever the observed session
  still has a verified live external writer;
- confirm Telegram topic service messages without routing them to an agent;
- prevent stopped rootless sessions from capturing new General messages;
- render tool progress as a distinct `Agent activity` message instead of a
  plain text-looking tool note;
- install an agent-specific Telegram command menu from a command catalog, with
  WalkCode controls plus Claude/Codex native slash commands where known;
- make Telegram update polling higher priority than best-effort command-menu
  sync and TUI observed-session maintenance, so a transient `setMyCommands`
  failure or TUI hook burst cannot delay inbound messages;
- in live polling, confirm Telegram offsets after a user turn is submitted to
  the agent transport, while draining the long-running agent output stream in
  the background.
- capture native hook parent pid and process-tree snapshots before deferred
  hook files are queued, so delayed drain can still distinguish real external
  TUI hooks from WalkCode-owned headless self-observation.
- capture native hook process group before deferred hook files are queued, so
  local terminal foreground jobs can provide the real external TUI pid for
  automatic takeover termination.
- tighten external TUI process detection to the executable basename
  (`claude`, `claude-code`, or `codex`) instead of any shell command that merely
  contains those words.
- ignore claim-capable hooks that target an orchestrator-owned headless session
  when the hook has no verifiable external TUI process identity; this prevents a
  takeover-resumed Claude/Codex session from being marked `external_tui` again,
  so the following late Stop-hook path remains an unobserved no-op instead of
  sending a duplicate final reply.

Non-goals:

- claiming Telegram Bot API can force client-level double-check read receipts;
- recreating every TUI-only slash command screen one-for-one in Telegram;
- adding a `/progress` toggle for tool activity.

Design:

- ADR: `docs/adr/0040-telegram-topic-session-and-tui-takeover-ux.md`.
- A TUI-origin topic stores origin metadata through binding capabilities and
  remains open. Readonly state is a writer-ownership property, not a closed-topic
  property.
- Every inbound message that tries to write to a TUI-origin topic becomes a
  separate blocked input. The takeover card carries that blocked input id; after
  a successful takeover, generation checks invalidate older cards.
- Topic service updates such as create/close/reopen are treated as channel
  service events and acknowledged in the polling offset path.
- If an external TUI is still the writer, automatic takeover requires a
  termination boundary. Claude Code `Stop` is not enough to prove the TUI
  exited. Structured resume without termination is allowed only when startup or
  hook reconciliation has moved the topic into a detached observed state.
- Command-menu installation runs after polling work in each service iteration.
  It improves discoverability but is not part of the input delivery path.
- TUI observed-session refresh and deferred-hook drain also run after polling as
  bounded best-effort maintenance. They improve read-only mirror freshness but
  do not own the live Telegram input path.
- Live polling treats transport submission as the ingress acknowledgement
  boundary. Agent deltas, tool activity, permission prompts, and final replies
  are still delivered through the same outbox path, but they no longer hold the
  Telegram update offset hostage.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_routing.py \
  tests/test_channel_native_takeover_orchestrator.py \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_telegram_claude.py
```

## Slice 43: Observed TUI Lease Reconciliation and Idempotent Takeover

Status: implemented and module-verified.

Goal: make the observed-session model durable around restarts and failed
takeovers. The durable agent session remains the semantic session. A local TUI
process is only a temporary writer lease and must be revalidated before it can
block Telegram input or be terminated.

Scope:

- reconcile loaded Telegram TUI-observed sessions at runtime startup;
- treat stale `terminate_ref.process_ref.pid` values as detached writer leases,
  not successful termination;
- split observed TUI states into:
  - `EXTERNAL_OBSERVED_READONLY` for a verified live TUI writer;
  - `EXTERNAL_DETACHED_IMPORTABLE` for a gone TUI with durable resume data;
  - `EXTERNAL_DETACHED_UNIMPORTABLE` for a gone TUI without resume data;
- make status-card `Take over` idempotent per `session_id + generation`;
- validate structured resume before terminating the live TUI process;
- roll back a provisional resumed handle if process termination fails;
- include launchd service load state in `walkcode native doctor`, so a silent
  Telegram bot can be diagnosed as "runtime service not loaded" instead of a
  session-routing issue.

Non-goals:

- proving that every Claude/Codex TUI session id is resumable without calling
  the target transport;
- force-closing Telegram input boxes for readonly topics;
- using `closeForumTopic` as a state indicator.

Design:

- `resume_ref` is a candidate durable reference. It becomes trusted only after
  `AgentTransport.resume(...)` succeeds.
- `terminate_ref` is a local process lease hint. Startup checks the process id;
  if it is gone, WalkCode clears the external writer and increments generation
  to invalidate old takeover prompts.
- Live takeover flow is:
  1. authorize the takeover transaction;
  2. render `resuming_structured`;
  3. call `resume(...)`;
  4. if a live external writer remains, render `terminating_external_tui` and
     terminate it;
  5. commit writer ownership to the structured transport;
  6. submit the selected blocked input exactly once.
- Resume failure leaves the TUI writer untouched and does not submit the
  Telegram input.
- Termination failure leaves writer ownership unchanged, rolls back the
  provisional resumed handle through `shutdown(..., "takeover_rollback")` when
  the transport supports it, and does not submit the blocked input.
- A repeated status-card takeover click after failure returns the existing
  terminal result and does not create another `takeover-only` blocked input.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_takeover_process_control.py \
  tests/test_channel_native_takeover_orchestrator.py \
  tests/test_channel_native_runtime.py
```

## Slice 44: Codex Unified App-server Client Architecture

Status: partially implemented. Codex app-server server-request mapping,
JSON-RPC request answering, managed daemon control-socket client selection, and
local `codex --remote unix://...` attach smoke are implemented; long-lived
shared remote-thread runtime is pending.

Goal: replace hook-first Codex TUI observation with a shared Codex app-server
client architecture, so Telegram and the Codex TUI attach to the same
`threadId` and consume the same event/request stream.

Scope:

- generate Codex app-server TypeScript/JSON-schema fixtures for the installed
  Codex version used by tests;
- replace the current `events(thread_id) -> list` Codex client shape with a
  bidirectional JSON-RPC session client;
- correlate JSON-RPC responses by id and route server notifications by
  `threadId`;
- persist server-initiated requests so HITL can survive restarts and topic
  movement;
- support a shared local app-server endpoint for both WalkCode and
  `codex --remote`;
- keep current Codex hooks only as fallback for unmanaged local TUI launches.

Non-goals:

- removing Claude Code hook observation;
- claiming WebSocket transport stability before local auth and reconnect
  behavior are verified;
- allowing simultaneous TUI and Telegram writers before app-server write
  ownership is proven.

Design:

- ADR: `docs/adr/0041-codex-unified-app-server-client-architecture.md`.
- Product session identity is Codex `threadId`.
- Telegram topic identity remains a channel placement for that thread.
- App-server `ServerRequest` messages are first-class input to HITL, not
  best-effort text.
- Takeover of Codex TUI-origin topics resumes by `threadId`; process
  termination remains conservative until multi-client write safety is proven.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_codex.py \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_takeover_orchestrator.py
```

Live gates still required before marking the slice fully implemented:

- Telegram-origin Codex turn with streamed tool/status events;
- TUI-origin Codex transcript sync without `UserPromptSubmit`;
- takeover resume by `threadId`.

Local verification after partial implementation:

```text
uv run --with pytest python -m pytest tests/test_channel_native_*.py -q
297 passed
```

Local app-server smoke after partial implementation:

```text
CodexStdioAppServerClient.request("thread/start", ...)
returned a non-empty threadId from `codex app-server --stdio`.

CodexManagedAppServerClient now starts/uses the managed daemon and connects to
`~/.codex/app-server-control/app-server-control.sock` with WebSocket JSON-RPC.
The local machine is verified with `codex-cli 0.142.5` and the CLI-managed
standalone install at `~/.codex/packages/standalone/current/codex`.
```

Managed client validation:

```text
Unix control-socket fake-server test passed: WebSocket handshake, initialize,
initialized, and thread/start.

Live daemon smoke passed: codex app-server daemon version reported running
0.142.5, and CodexManagedAppServerClient.request("thread/start", ...)
returned a non-empty threadId.

Remote TUI attach smoke passed: `codex --remote unix://...` connected to the
same daemon and completed a minimal prompt. The scripted smoke uses a TTY
because Codex remote TUI refuses non-terminal stdin.
```

## Slice 45: HITL Full Capability for Telegram and Takeover

Status: partially implemented. Telegram-origin Codex approvals, tool
request-user-input, MCP form-mode elicitation, durable HITL storage, and
pre-takeover stale-HITL marking are implemented. Live shared-app-server HITL
rehydration after takeover is pending.

Goal: make HITL a durable transport request/response flow for Telegram-origin
sessions, and define exactly how pending HITL behaves when a TUI-origin session
is taken over.

Scope:

- add a durable `HitlRequest` / `HitlDecision` layer above callback tokens;
- map Codex app-server server requests to neutral permission or AskUserQuestion
  views:
  - `item/commandExecution/requestApproval`;
  - `item/fileChange/requestApproval`;
  - `item/permissions/requestApproval`;
  - `item/tool/requestUserInput`;
  - `mcpServer/elicitation/request`;
- answer the original app-server JSON-RPC request id exactly once after an
  authorized Telegram decision;
- preserve callback-token semantics: unauthorized or unsupported decisions do
  not consume tokens;
- support command/file/permission approvals, tool request-user-input, MCP
  elicitation, `Other`, multi-select, and secret answer prompts;
- after takeover, answer a pending TUI-origin HITL only if the structured
  transport is resumed and the native request is still pending.

Non-goals:

- answering a TUI-owned prompt while the TUI remains the writer;
- fabricating a HITL answer after the native request expired or disappeared;
- claiming Claude SDK parity without real SDK E2E.

Design:

- ADR: `docs/adr/0042-hitl-takeover-and-telegram-full-capability.md`.
- Telegram remains the first full HITL implementation target; Lark uses the
  same neutral request model later.
- TUI-origin read-only HITL cards are context until takeover succeeds.
- Telegram-origin HITL cards are actionable because WalkCode already owns the
  structured transport.
- When takeover succeeds, any pending HITL request from the old observed
  generation is marked `stale` and rendered in the topic, so old TUI-owned
  buttons are not silently treated as still answerable.

Verification:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_permission_roundtrip.py \
  tests/test_channel_native_ask_user_roundtrip.py \
  tests/test_channel_native_callback_ack.py \
  tests/test_channel_native_persistence_reliability.py \
  tests/test_channel_native_takeover_orchestrator.py
```

Live gates required before marking implemented:

- Codex command execution approval;
- Codex file change approval;
- Codex permission-profile approval;
- Codex tool request-user-input;
- MCP elicitation where the local app-server can trigger it;
- takeover-resume with a pending HITL request.

Local verification after partial implementation:

```text
uv run --with pytest python -m pytest \
  tests/test_channel_native_runtime.py \
  tests/test_channel_native_codex.py \
  tests/test_channel_native_permission_roundtrip.py \
  tests/test_channel_native_ask_user_roundtrip.py \
  tests/test_channel_native_callback_ack.py \
  tests/test_channel_native_persistence_reliability.py -q
121 passed

uv run --with pytest python -m pytest \
  tests/test_channel_native_codex.py \
  tests/test_channel_native_takeover_orchestrator.py -q
33 passed
```

## Current Local Verification

Date: 2026-07-02.

Status: passed for the currently implemented Telegram-first V3 path.

```text
uv run --with pytest python -m pytest tests/test_channel_native_*.py -q
297 passed

uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py
passed

codex app-server daemon version
status=running
managedCodexVersion=0.142.5
appServerVersion=0.142.5
socketPath=~/.codex/app-server-control/app-server-control.sock

CodexManagedAppServerClient.request("thread/start", ...)
returned a non-empty thread id from the managed daemon control socket.

uv run python scripts/channel_native_debug.py \
  --env-file ~/.walkcode/telegram-codex.env \
  agent-smoke --live --json --timeout 120
ok=true
event_types=[turn.delta, turn.completed]

uv run --with claude-agent-sdk python scripts/channel_native_debug.py \
  --env-file ~/.walkcode/telegram-claude.env \
  agent-smoke --live --json --timeout 120
ok=true
event_types included turn.delta, tool.started, tool.completed, turn.completed

codex --remote unix://~/.codex/app-server-control/app-server-control.sock ...
connected to the same daemon and returned walkcode-remote-smoke-ok
```

Local services were reinstalled/restarted through the uv tool environment so
the launchd services load the repository checkout and have `claude-agent-sdk`
available:

```text
uv tool install --force --editable /Users/alpha/Documents/workspace/walkcode --with claude-agent-sdk
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-claude
launchctl kickstart -k gui/$(id -u)/com.walkcode.telegram-codex

com.walkcode.telegram-claude loaded
com.walkcode.telegram-codex loaded
```

Runtime diagnostics after restart:

```text
telegram-claude doctor: agent_status.available=true
telegram-codex doctor: agent_status.available=true
runtime gate: competing_consumer_count=0, legacy_remnant_count=0
state gate: inbound_in_progress=0, pending_bindings=0
codex outbox: pending_count=0, dead_count=0
claude outbox: pending_count=0, dead_count=4
```

The four Claude dead outbox records are historical Telegram 429 failures from
old sessions. They are not pending work and do not block new sessions.

## Prior Local Verification

Status: historical baseline from an earlier slice. Kept as evidence for the
older package/build gate; current channel-native verification is the 2026-07-02
section above.

```text
uv run --with pytest python -m pytest tests/test_channel_native_*.py
268 passed

uv run python -m compileall -q src/walkcode/channel_native src/walkcode/channel_native_runtime.py src/walkcode/__main__.py
passed

uv run --with pytest python -m pytest
657 passed, 4 warnings

uv build
Successfully built dist/walkcode-0.10.54.tar.gz
Successfully built dist/walkcode-0.10.54-py3-none-any.whl

env WALKCODE_ENV_FILE=/tmp/walkcode-native-v3.env \
  WALKCODE_CHANNEL=telegram \
  TELEGRAM_BOT_TOKEN=123456:fake-token \
  WALKCODE_AGENT=claude \
  WALKCODE_CWD=/tmp \
  WALKCODE_STATE_PATH=/tmp/walkcode-native-state.json \
  uv run --no-project --no-cache --with ./dist/walkcode-0.10.54-py3-none-any.whl \
  walkcode native doctor --json
passed
```

The warnings are existing dependency deprecations from `lark_oapi` and `websockets`; they are not failures in the channel-native V3 contract tests.

## Prior Real Module Verification

Date: 2026-06-28.

Status: superseded by Slice 36. The result below remains historical evidence
for the earlier single-env smoke, but current release validation requires
dedicated per-agent env files such as `telegram-claude.env` and
`telegram-codex.env`, plus a real inbound Telegram turn.

```text
WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python -m walkcode native doctor --json
telegram.enabled=true
claude_headless.enabled=true
codex_app_server.enabled=true
lark.enabled=false

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py runtime --json
competing_consumer_count=0

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py state --json
expired_writer_leases=0

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py outbox --json
pending_count=0, dead_count=0

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py telegram --limit 10 --json
pending_updates.count=0
safe_to_run_serve_once=true

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py agent-smoke --agent claude --live --json
event_types=[turn.delta, turn.completed]

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-codex.env \
  uv run --with claude-agent-sdk python scripts/channel_native_debug.py agent-smoke --agent codex --live --json
event_types=[turn.delta, turn.completed]

WALKCODE_ENV_FILE=/Users/alpha/.walkcode/telegram-claude.env \
  uv run --with claude-agent-sdk python -m walkcode native serve --once --poll-timeout 0 --limit 5
processed 0 update(s)
```

Lark remains a peer `ChannelAdapter` design target, but live V3 Lark ingress is
not promoted without its own real E2E gate.
