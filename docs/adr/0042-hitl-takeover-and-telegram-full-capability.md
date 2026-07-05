# ADR 0042: HITL Takeover and Telegram Full Capability

Date: 2026-07-01

Status: Accepted as target behavior; Codex Telegram-origin server-request
roundtrip, durable HITL store, and takeover stale-HITL handling implemented;
live shared-app-server recovery pending.

## Context

V3 already has a neutral `InteractionStore`, Telegram inline-keyboard
rendering, callback acknowledgement, authorization checks, and transport
round-trip boundaries for:

- permission prompts;
- AskUserQuestion prompts;
- takeover prompts.

The implementation is still incomplete in two important ways:

- Codex app-server permission and AskUserQuestion callbacks now have a
  server-request round-trip foundation plus durable `HitlRequest` /
  `HitlDecision` storage, but live E2E validation is still pending.
- TUI-origin sessions are read-only observations. If a HITL prompt appears in
  the TUI and Telegram later takes over the session, WalkCode must still recover
  or explicitly stale-mark that pending prompt through the structured transport.

Codex 0.142.x's generated app-server protocol exposes real HITL server
requests. The relevant methods are:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`
- `item/tool/requestUserInput`
- `mcpServer/elicitation/request`

These are server-initiated JSON-RPC requests. The client must reply to the same
request id with the method-specific response shape.

## Decision

HITL is a first-class session event, not a channel-only card.

WalkCode must persist enough state to resume, re-render, authorize, and answer a
HITL prompt after restarts, topic moves, or takeover.

The core model becomes:

- `HitlRequest`: session id, generation, transport kind, transport request id,
  native method, native params, normalized prompt kind, created time, expires
  time, status, and channel view placement.
- `HitlDecision`: actor, action, native response payload, decided time, and
  delivery status.

`InteractionStore` can continue to provide short callback tokens and
multi-step question state, but the transport-native server request must be
persisted separately from the rendered Telegram message.

## 2-1: TUI Read-only Topic Takeover With Pending HITL

If Telegram takes over a TUI-observed session while a HITL prompt is pending,
the behavior depends on whether the prompt can be recovered through the
structured transport.

For Codex under the unified app-server architecture:

1. The TUI prompt is a server request on a Codex `threadId`.
2. WalkCode mirrors the request into the Telegram topic as a read-only HITL
   card if it is only observing.
3. If the user clicks a decision in Telegram before takeover, WalkCode first
   opens the takeover flow. It must not answer the server request while the TUI
   remains the writer unless app-server multi-client write ownership has been
   verified.
4. After takeover:
   - call `thread/resume` by `threadId`;
   - rehydrate active server requests from the live app-server stream when
     possible;
   - answer the original server request id if still pending;
   - then submit any blocked Telegram input.
5. If the original request id is no longer pending after resume, WalkCode must
   render a stale-HITL message and ask the user to continue the session with a
   fresh instruction.

Current implementation applies the safe half of that rule: after a successful
observed-session takeover, pending HITL requests from the pre-takeover
generation are marked `stale` and rendered as explicit stale-HITL context. It
does not fabricate an answer for an old TUI-owned request. The remaining
shared Codex app-server work is to prove whether a still-live native request id
can be rehydrated and answered after `thread/resume`.

For Claude Code hook-based TUI observation:

- a TUI-side prompt is only observable if the hook payload carries enough
  request identity and answer protocol data;
- if no answerable request id exists, Telegram may show the prompt as read-only
  context, but takeover cannot automatically answer it;
- after takeover/resume, the agent may re-ask or continue from its durable
  state. WalkCode should not fabricate a HITL response.

## 2-2: Telegram-origin Session Full HITL

For sessions created from Telegram, WalkCode owns the structured transport.
Telegram must support the full HITL loop.

Required behavior:

- render every transport-native HITL request as a Telegram message with inline
  buttons or a form-like multi-step interaction;
- call `answerCallbackQuery` immediately for callback UX;
- role-gate decisions before consuming callback tokens;
- keep tokens unconsumed when authorization or transport capability checks fail;
- persist pending HITL requests and decisions across restarts;
- answer the original transport request id exactly once;
- update or supersede the HITL card after decision;
- continue draining the agent event stream after the decision is delivered;
- avoid treating HITL text replies as new user turns when they belong to an
  `Other`/form answer.

Telegram rendering:

- command/file/permission approvals use concise cards with action buttons such
  as `Allow once`, `Allow for session`, `Deny`, and `Cancel turn` when the
  native protocol exposes those decisions;
- command approvals show command, cwd, reason, and additional permission or
  network context when present;
- file approvals show summarized changed files and grant root when present;
- permission-profile approvals show requested filesystem/network deltas;
- AskUserQuestion supports single-choice, multi-choice, `Other`, and secret
  answer prompts;
- MCP elicitation supports accept/decline/cancel plus generated form fields for
  supported schema shapes.

Codex app-server mapping:

- `item/commandExecution/requestApproval` answers with
  `CommandExecutionRequestApprovalResponse`;
- `item/fileChange/requestApproval` answers with
  `FileChangeRequestApprovalResponse`;
- `item/permissions/requestApproval` answers with
  `PermissionsRequestApprovalResponse`;
- `item/tool/requestUserInput` answers with `ToolRequestUserInputResponse`;
- `mcpServer/elicitation/request` answers with
  `McpServerElicitationRequestResponse`.

Claude headless mapping:

- keep the existing neutral `permission.requested` and `ask_user.requested`
  adapter surface;
- complete real Claude SDK E2E before claiming parity;
- if the SDK lacks an answer method, return `capability_disabled` without
  consuming the token.

## Consequences

- HITL cards are recoverable and auditable instead of being transient UI.
- Telegram-origin sessions can support Codex approvals and user-input requests
  through app-server JSON-RPC, not hooks.
- TUI-origin takeover has a clear rule: answer a pending HITL only after the
  structured transport is resumed and the native request is still pending.
- The same neutral UI model can still render to Lark later, but Telegram is the
  first complete implementation target.

## Implementation Progress

2026-07-01:

- Codex app-server server requests are now surfaced as neutral
  `permission.requested` or `ask_user.requested` events.
- Telegram callback decisions can now be sent back to the original Codex
  JSON-RPC request id via the transport callback path.
- `item/tool/requestUserInput` answers are converted back to Codex's
  question-id keyed response shape.
- Codex callback delivery can rebuild response shape from persisted interaction
  metadata if the transport-local pending request map is lost.
- Durable `HitlRequest` / `HitlDecision` storage is separate from rendered
  callback tokens and survives `JsonFileStateStore` round trips.
- Observed-session takeover marks pre-takeover pending HITL requests stale and
  renders a visible stale-HITL message.
- MCP elicitation form mode now generates form-field questions for simple
  schema properties and answers with structured `content`.
- Remaining work: live shared-app-server HITL rehydration after TUI takeover,
  richer MCP schema coverage, and live Codex E2E gates.

## Verification

Required tests:

- unit tests for mapping each Codex server request to a neutral HITL view;
- callback tests for accept/deny/cancel/session-wide approval decisions;
- restart tests proving pending HITL requests and tokens persist;
- stale generation and unauthorized actor tests that do not consume tokens;
- live Codex app-server E2E for command approval, file approval, permissions
  approval, tool request user input, and MCP elicitation where feasible;
- takeover E2E where a TUI-origin Codex pending HITL survives `thread/resume`
  and is either answered or explicitly marked stale.
